"""Resolve a GitHub repository link to an existing, verified local checkout.

This module never clones, fetches, checks out, or executes repository contents.
Local paths remain usable without a Git origin or Git history.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time
from urllib.parse import unquote, urlsplit


MAX_SEARCH_ENTRIES = 400
MAX_GIT_CANDIDATES = 100
MAX_REMOTE_BRANCHES = 1000
LOOKUP_SECONDS = 5
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")


def _validate_ref(ref):
    if (not isinstance(ref, str) or len(ref) > 200 or (ref and (
            ref.startswith("-") or ".." in ref or "@{" in ref
            or re.search(r"[\s\x00-\x1f\x7f~^:?*\[\\]", ref)))):
        raise ValueError("Choose a branch or revision, or leave it blank for current files")


def _resolve_reference(path, ref):
    """Prefer exact local revisions, then a unique fetched remote branch."""
    if not ref or not path.is_dir():
        return ref
    command = ["git", "-c", "core.fsmonitor=false", "-C", str(path)]
    try:
        exact = subprocess.run(command + ["rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"],
                               capture_output=True, timeout=2, check=False)
        if exact.returncode == 0:
            return ref
        result = subprocess.run(command + ["for-each-ref", "--format=%(refname)%00%(symref)",
            "--count=" + str(MAX_REMOTE_BRANCHES + 1), "refs/remotes"],
            capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Reading local Git branches failed or timed out. Enter the full branch reference.") from None
    if result.returncode:
        return ref
    if len(result.stdout) > 256 * 1024 or len(result.stdout.splitlines()) > MAX_REMOTE_BRANCHES:
        raise ValueError("Too many remote branches to match. Enter the full reference, such as origin/your-branch.")
    matches = set()
    for entry in result.stdout.splitlines():
        name, _, symbolic_target = entry.partition(b"\0")
        if symbolic_target:
            continue
        try:
            name = name.decode("utf-8")
        except UnicodeError:
            continue
        if name.startswith("refs/remotes/"):
            remote_ref = name[len("refs/remotes/"):]
            _, separator, branch = remote_ref.partition("/")
            if separator and branch == ref:
                matches.add(remote_ref)
    if len(matches) > 1:
        choices = ", ".join(sorted(matches))
        raise ValueError(f"Branch {ref} exists on multiple remotes. Select its full reference: {choices}.")
    return next(iter(matches)) if matches else ref


def _github_identity(value, *, remote=False):
    """Return owner/repository/ref for supported links, without credentials."""
    if remote and value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:"):]
    elif remote and value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value[len("ssh://git@github.com/"):]
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "github.com"
                or parsed.username or parsed.password or parsed.port
                or parsed.query or parsed.fragment):
            raise ValueError()
        parts = unquote(parsed.path).strip("/").split("/")
        if len(parts) < 2:
            raise ValueError()
        owner, name = parts[:2]
        if name.endswith(".git"):
            name = name[:-4]
        if not _NAME.fullmatch(owner) or not _NAME.fullmatch(name) or owner in {".", ".."} or name in {".", ".."}:
            raise ValueError()
        ref = ""
        if len(parts) > 2:
            if parts[2] != "tree" or remote:
                raise ValueError()
            ref = "/".join(parts[3:]).rstrip("/")
            if ref and (len(ref) > 200 or ref.startswith("-") or ".." in ref or "@{" in ref
                        or re.search(r"[\s\x00-\x1f\x7f~^:?*\[\\]", ref)):
                raise ValueError()
        return owner, name, ref
    except (TypeError, ValueError):
        raise ValueError("Use an HTTPS GitHub repository URL without credentials, query parameters or a fragment, or enter a local folder.") from None


def _origin_identity(path, timeout):
    try:
        process = subprocess.run([
            "git", "-c", "core.fsmonitor=false", "-C", str(path),
            "config", "--local", "--get", "remote.origin.url",
        ], capture_output=True, timeout=timeout, check=False)
        if process.returncode or len(process.stdout) > 8192:
            return None
        owner, name, _ = _github_identity(process.stdout.decode("utf-8").strip(), remote=True)
        return owner.casefold(), name.casefold()
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None


def _checkout_candidates(roots, name):
    found = {}
    for root in roots:
        try:
            root = Path(root).expanduser().resolve()
            # Check the expected name directly even in a directory with many files.
            entries = [root]
            if root.is_dir():
                for index, entry in enumerate(root.iterdir()):
                    if index >= MAX_SEARCH_ENTRIES:
                        break
                    entries.append(entry)
            entries.append(root / name)
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                    continue
                if (entry / ".git").exists():
                    metadata = entry.stat()
                    found.setdefault((metadata.st_dev, metadata.st_ino), entry.resolve())
        except OSError:
            continue
    return sorted(found.values(), key=lambda path: (path.name.casefold() != name.casefold(), str(path)))


def resolve_repository_selection(repo: str, ref: str = "", *, search_roots=None) -> dict:
    """Accept a local folder or match a GitHub link to a local Git origin.

    Explicit ``ref`` wins over a branch embedded in ``/tree/...``. A root GitHub
    link with a trailing ``/tree/`` also works when the branch is supplied in its
    separate field. A branch shorthand resolves to a unique fetched remote branch
    only when no exact local revision exists. Blank source selection is optional.
    """
    if (not isinstance(repo, str) or len(repo) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in repo)):
        raise ValueError("Enter a local repository folder or an HTTPS GitHub repository URL")
    _validate_ref(ref)
    original, ref = repo.strip(), ref.strip()
    if not original:
        return {"repo": "", "ref": ref, "repo_name": None,
                "input": original, "resolved_from_url": False}
    if "://" not in original and not original.startswith("git@"):
        path = Path(original).expanduser().resolve()
        return {"repo": str(path), "ref": _resolve_reference(path, ref), "repo_name": path.name,
                "input": original, "resolved_from_url": False}
    owner, name, embedded_ref = _github_identity(original)
    roots = search_roots if search_roots is not None else (Path.cwd().parent, Path.home())
    candidates = _checkout_candidates(roots, name)
    if len(candidates) > MAX_GIT_CANDIDATES:
        raise ValueError("Too many local repositories to search. Enter the exact local repository folder.")
    identity, matches = (owner.casefold(), name.casefold()), []
    deadline = time.monotonic() + LOOKUP_SECONDS
    for candidate in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Local repository lookup timed out. Enter the exact local repository folder.")
        if _origin_identity(candidate, min(1, remaining)) == identity:
            matches.append(candidate)
    if not matches:
        raise ValueError(f"No local checkout found for {owner}/{name}. Enter the local repository folder on the dashboard server; GitHub URLs are matched to existing local clones.")
    if len(matches) > 1:
        raise ValueError(f"More than one local checkout matches {owner}/{name}. Enter the exact local repository folder to choose one.")
    path = matches[0]
    return {"repo": str(path), "ref": _resolve_reference(path, ref or embedded_ref), "repo_name": path.name,
            "input": original, "resolved_from_url": True}
