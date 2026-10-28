"""Bounded, read-only source context. Never imports or runs a controller checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


MAX_SOURCE_BYTES = 64 * 1024
MAX_CHAT_SOURCE_BYTES = 96 * 1024
MAX_INVENTORY_FILES = 1000
MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_CHAT_FILE_BYTES = 4 * 1024 * 1024
MAX_BRANCHES = 1000
SOURCE_EXTENSIONS = {
    ".py", ".cpp", ".cc", ".c", ".h", ".hpp", ".cu", ".cuh", ".rs", ".js", ".ts",
    ".yaml", ".yml", ".toml", ".json", ".xml", ".cfg", ".ini", ".launch",
    ".md", ".rst", ".txt", ".cmake", ".sh", ".bash", ".m", ".msg", ".srv", ".action",
}
EXCLUDED_DIRECTORIES = {
    "__pycache__", "node_modules", "build", "dist", "install", "log", "logs",
    "runs", "recordings", "vendor", "venv", "env", "site-packages",
}
SECRET_NAME = re.compile(
    r"(?:^|[_.-])(?:secrets?|credentials?|passwords?|tokens?|api[_-]?keys?|auth)(?:[_.-]|$)", re.I)
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"[\"']?\s*[=:]\s*[\"'][^\"'\r\n]{8,}[\"']|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"[\"']?\s*:\s*[^\s\"'#${\r\n][^\r\n#]{7,}", re.I)


def _source_path(relative):
    """Only include ordinary project text, never credential or generated paths."""
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return False
    if any(part.startswith(".") or part in EXCLUDED_DIRECTORIES or part.endswith(".egg-info")
           for part in candidate.parts):
        return False
    if any(SECRET_NAME.search(part) for part in candidate.parts):
        return False
    return candidate.suffix.lower() in SOURCE_EXTENSIONS or candidate.name in {"Dockerfile", "Makefile"}


def _git(path, *args, limit=2 * 1024 * 1024):
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
            capture_output=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Reading repository source timed out; choose a local Git checkout") from None
    if result.returncode:
        raise ValueError("Could not read selected repository Git revision")
    if len(result.stdout) > limit:
        raise ValueError("Repository source inventory exceeds the context size limit")
    return result.stdout


def _current_branch(path):
    """HEAD can be detached or a directory may not be a Git checkout."""
    try:
        return _git(path, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip() or None
    except (ValueError, UnicodeError):
        return None


def _validate_source_ref(ref):
    # Read a named revision, never a revision expression or a Git option. Git
    # accepts branch names beyond ASCII (including '+' and Unicode characters).
    if not isinstance(ref, str) or (ref and (
            len(ref) > 200 or ref.startswith("-") or ".." in ref or "@{" in ref
            or re.search(r"[\s\x00-\x1f\x7f~^:?*\[\\]", ref))):
        raise ValueError("Choose a Git branch, tag or commit, or leave the reference blank for local edits")


def list_source_branches(repo: str) -> dict:
    """List local and fetched remote branches without fetching or checking out."""
    if not isinstance(repo, str) or not repo.strip() or len(repo) > 4096:
        raise ValueError("Choose a local repository directory to list its branches")
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Repository directory does not exist")
    result = {"repo_name": path.name, "current_branch": None,
              "branches": [], "is_git": False, "truncated": False}
    try:
        _git(path, "rev-parse", "--git-dir")
    except ValueError as error:
        if "timed out" in str(error) or "size limit" in str(error):
            raise
        return result
    result["is_git"] = True
    result["current_branch"] = _current_branch(path)
    output = _git(path, "for-each-ref", "--format=%(refname)%00%(symref)",
                  "--count=" + str(MAX_BRANCHES + 1), "refs/heads", "refs/remotes")
    entries = output.splitlines()
    result["truncated"] = len(entries) > MAX_BRANCHES
    branches = []
    for entry in entries[:MAX_BRANCHES]:
        name, _, symbolic_target = entry.partition(b"\0")
        if symbolic_target:
            continue
        name = name.decode("utf-8")
        for prefix in ("refs/heads/", "refs/remotes/"):
            if name.startswith(prefix):
                branches.append(name[len(prefix):])
                break
    result["branches"] = sorted(set(branches), key=lambda name: (name != result["current_branch"], name))
    return result


def _read_local(path, relative, *, limit=MAX_SOURCE_BYTES):
    """Open every component without following symlinks, including during races."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Source is not an ordinary file")
            if metadata.st_size > limit:
                raise ValueError("File exceeds the per-file source limit")
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ValueError("File exceeds the per-file source limit")
            return content
    finally:
        os.close(descriptor)


def _working_inventory(path):
    try:
        names = _git(path, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        return sorted({name.decode("utf-8") for name in names.split(b"\0") if name}), False
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ValueError) and ("timed out" in str(exc) or "size limit" in str(exc)):
            raise
        # Working directories need not have a Git history. Hidden/generated
        # directories are pruned before walking; symlinks are never traversed.
        names = []
        scanned = 0
        for directory, subdirectories, files in os.walk(path, followlinks=False):
            scanned += 1
            subdirectories[:] = sorted(
                part for part in subdirectories
                if not part.startswith(".") and part not in EXCLUDED_DIRECTORIES
                and not part.endswith(".egg-info") and not (Path(directory) / part).is_symlink())
            for name in sorted(files):
                relative = (Path(directory) / name).relative_to(path).as_posix()
                if _source_path(relative):
                    names.append(relative)
                    if len(names) >= MAX_INVENTORY_FILES:
                        return names, True
            if scanned >= MAX_INVENTORY_FILES:
                return names, True
        return names, False


def read_chat_source_context(repo: str, ref: str = "", query: str = "", *,
                             max_bytes: int = MAX_CHAT_SOURCE_BYTES) -> dict:
    """Refresh bounded source evidence for a chat turn, without executing code.

    A blank reference reads current local edits and untracked, nonignored source.
    An explicit reference always reads immutable Git blobs. ``inventory`` lists
    eligible files so the assistant can explain gaps; ``omitted_files`` explains
    files that did not fit. A query prioritizes matching paths and symbols.
    """
    if not repo:
        return {"status": "unavailable", "deployment_verified": False, "files": [],
                "inventory": [], "note": "Choose the local repository directory in chat settings."}
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Repository directory does not exist")
    _validate_source_ref(ref)
    if not isinstance(query, str) or len(query) > 24000:
        raise ValueError("Source search text must be at most 24000 characters")
    if not isinstance(max_bytes, int) or not 1024 <= max_bytes <= 512 * 1024:
        raise ValueError("Source context size must be between 1 and 512 KiB")
    commit = None
    inventory_truncated = False
    if ref:
        commit = _git(path, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
        entries = _git(path, "ls-tree", "-r", "-l", "-z", commit).split(b"\0")
        names = []
        blob_sizes = {}
        for entry in entries:
            if not entry:
                continue
            metadata, separator, name = entry.partition(b"\t")
            fields = metadata.split()
            if separator and len(fields) == 4 and fields[0] in {b"100644", b"100755"}:
                try:
                    relative = name.decode("utf-8")
                    names.append(relative)
                    blob_sizes[relative] = int(fields[3])
                except UnicodeError:
                    continue
    else:
        names, inventory_truncated = _working_inventory(path)
        try:
            commit = _git(path, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        except ValueError:
            pass
    names = sorted({name for name in names if _source_path(name)})
    if len(names) > MAX_INVENTORY_FILES:
        inventory_truncated = True
        names = names[:MAX_INVENTORY_FILES]
    tokens = {token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", query)} - {
        "the", "and", "for", "with", "this", "that", "what", "how", "can", "you", "from",
        "have", "should", "would", "could", "please", "code", "change", "recommend", "lap", "laps",
    }

    def priority(name, content=""):
        lower = name.lower()
        score = sum(40 for token in tokens if token in lower)
        score += 10000 if name in query or query.strip() == name else 0
        score += sum(min(content.lower().count(token), 8) for token in tokens)
        score += 12 if any(part in lower for part in ("controller", "planner", "config", "launch", "param")) else 0
        score += 4 if lower.endswith((".py", ".yaml", ".yml")) else 0
        score -= 8 if any(part in Path(lower).parts for part in ("tests", "test", "docs")) else 0
        return -score, name

    candidates, omissions, scanned_bytes = [], [], 0
    for name in sorted(names, key=priority):
        if scanned_bytes >= MAX_SCAN_BYTES:
            omissions.append({"path": name, "reason": "source search size limit"})
            continue
        try:
            if ref and blob_sizes[name] > MAX_SOURCE_BYTES:
                raise ValueError("File exceeds the per-file source limit")
            raw = (_git(path, "show", f"{commit}:{name}", limit=MAX_SOURCE_BYTES)
                   if ref else _read_local(path, name))
            scanned_bytes += len(raw)
            if b"\0" in raw:
                raise ValueError("Binary file")
            content = raw.decode("utf-8")
            if SECRET_CONTENT.search(content):
                # Do not include even the contents or digest of credential files.
                omissions.append({"path": name, "reason": "possible credentials"})
                continue
            candidates.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
                               "content": content, "line_count": len(content.splitlines()),
                               "start_line": 1, "size_bytes": len(raw)})
        except (OSError, ValueError, UnicodeError):
            omissions.append({"path": name, "reason": "unreadable, unsafe, binary or over 64 KiB"})
    files, total = [], 0
    for item in sorted(candidates, key=lambda item: priority(item["path"], item["content"])):
        if total + item["size_bytes"] <= max_bytes:
            files.append(item)
            total += item["size_bytes"]
        else:
            omissions.append({"path": item["path"], "reason": "context size limit"})
    if not files:
        raise ValueError("No readable source or configuration files are available in the selected directory")
    snapshot = hashlib.sha256(json.dumps(
        sorted((item["path"], item["sha256"]) for item in files), separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "status": "local_source", "mode": "git_revision" if ref else "working_tree",
        "ref": ref, "commit": commit, "deployment_verified": False,
        "repo_name": path.name, "branch": _current_branch(path),
        "files": files, "inventory": names, "inventory_truncated": inventory_truncated,
        "omitted_files": omissions, "context_bytes": total, "snapshot_sha256": snapshot,
        "note": ("Read-only pinned Git revision." if ref else
                 "Current local files, including uncommitted edits; the commit is only the working tree's base.")
                + " File contents begin at line 1. Source defaults are not verified runtime settings. "
                  "Only supplied files may be cited as read; omitted files remain unavailable.",
    }


def read_chat_source_file(repo: str, ref: str, relative: str, *, start_line: int = 1,
                          max_lines: int = 250) -> dict:
    """Retrieve an exact eligible file in bounded chunks, including large files.

    This is the escape hatch for files omitted from the initial chat context.
    ``next_line`` allows subsequent retrieval without claiming omitted code was
    read. The complete file is scanned for common credentials before disclosure.
    """
    if not isinstance(relative, str) or not _source_path(relative) or "\\" in relative:
        raise ValueError("Choose a source file from the repository inventory")
    if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
        raise ValueError("Source line numbers start at one")
    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or not 1 <= max_lines <= 500:
        raise ValueError("Retrieve between 1 and 500 source lines at a time")
    _validate_source_ref(ref)
    if not repo:
        raise ValueError("Choose the local repository directory in chat settings")
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Repository directory does not exist")
    commit = None
    if ref:
        commit = _git(path, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
        entry = _git(path, "ls-tree", "-l", "-z", commit, "--", relative)
        metadata, separator, name = entry.rstrip(b"\0").partition(b"\t")
        fields = metadata.split()
        if (not separator or len(fields) != 4 or fields[0] not in {b"100644", b"100755"}
                or name.decode("utf-8", errors="replace") != relative):
            raise ValueError("Choose an ordinary source file at the selected Git revision")
        if int(fields[3]) > MAX_CHAT_FILE_BYTES:
            raise ValueError("Source file exceeds the 4 MiB retrieval limit")
        raw = _git(path, "show", f"{commit}:{relative}", limit=MAX_CHAT_FILE_BYTES)
    else:
        inventory, _ = _working_inventory(path)
        if relative not in inventory:
            raise ValueError("Choose a nonignored source file from the repository inventory")
        try:
            commit = _git(path, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        except ValueError:
            pass
        try:
            raw = _read_local(path, relative, limit=MAX_CHAT_FILE_BYTES)
        except (OSError, ValueError) as exc:
            raise ValueError("Source file is unreadable, unsafe or exceeds the 4 MiB retrieval limit") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError("Source file must be UTF-8 text") from None
    if "\0" in content or SECRET_CONTENT.search(content):
        raise ValueError("Source file is binary or may contain credentials")
    lines = content.splitlines(keepends=True)
    if start_line > max(1, len(lines)):
        raise ValueError("Requested line is beyond the end of this source file")
    selected, size = [], 0
    for line in lines[start_line - 1:start_line - 1 + max_lines]:
        encoded_size = len(line.encode("utf-8"))
        if size + encoded_size > 32 * 1024:
            if not selected:
                raise ValueError("This source line exceeds the 32 KiB retrieval limit")
            break
        selected.append(line)
        size += encoded_size
    end_line = start_line + len(selected) - 1
    return {
        "status": "local_source", "mode": "git_revision" if ref else "working_tree",
        "ref": ref, "commit": commit, "deployment_verified": False,
        "repo_name": path.name, "branch": _current_branch(path),
        "path": relative, "content": "".join(selected), "sha256": hashlib.sha256(raw).hexdigest(),
        "start_line": start_line, "end_line": end_line, "line_count": len(lines),
        "next_line": end_line + 1 if end_line < len(lines) else None,
        "note": "Only the supplied line range was retrieved. Source defaults are not verified runtime settings.",
    }


def read_source_context(repo: str, ref: str = "HEAD") -> dict:
    """Read a bounded source snapshot from any repository without checking it out."""
    return read_chat_source_context(repo, ref, max_bytes=MAX_SOURCE_BYTES)
