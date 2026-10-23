"""Bounded, read-only source context. Never imports or runs a controller checkout."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess


MPC_FILES = (
    "mpc/scripts/mpc_node.py",
    "mpc/launch/levine_launch.py",
    "mpc/launch/spielberg_launch.py",
)
MAX_SOURCE_BYTES = 64 * 1024


def read_source_context(repo: str, ref: str = "HEAD") -> dict:
    if not repo:
        return {"status": "unavailable", "deployment_verified": False,
                "note": "No controller source selected. Active controller configuration is unknown."}
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Controller source directory does not exist")
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", ref) or ref.startswith("-"):
        raise ValueError("Choose a Git branch, tag or commit for source context")

    def git(*args):
        try:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
                capture_output=True, timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Reading controller source timed out; choose a local Git checkout") from None
        if result.returncode:
            raise ValueError("Could not read selected controller Git revision")
        if len(result.stdout) > MAX_SOURCE_BYTES:
            raise ValueError("Controller source exceeds the context size limit")
        return result.stdout

    commit = git("rev-parse", "--verify", ref + "^{commit}").decode().strip()
    files = []
    total = 0
    for relative in MPC_FILES:
        try:
            content = git("show", f"{commit}:{relative}")
        except ValueError:
            continue
        if total + len(content) > MAX_SOURCE_BYTES:
            break
        total += len(content)
        files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest(),
                      "content": content.decode("utf-8", errors="replace")})
    if not files:
        raise ValueError("No supported Lab 7 MPC source files exist at that revision")
    return {
        "status": "local_source", "ref": ref, "commit": commit,
        "deployment_verified": False, "files": files,
        "note": "Read-only Git source context, not proof of the code or effective settings deployed on the car. "
                "ROS launch overrides and hardcoded constants must be verified on the deployed controller before exact tuning.",
    }
