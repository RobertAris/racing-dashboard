#!/usr/bin/env bash
# Independent simulator setup for RobertAris' dashboard; never changes Lab 6.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${RACING_SIM_ROOT:-}" ]]; then
    SIM_ROOT="$RACING_SIM_ROOT"
elif [[ -n "${LAB6_SIM_ROOT:-}" ]]; then
    SIM_ROOT="$LAB6_SIM_ROOT"
elif [[ -d /private/tmp/lab6_f1tenth_gym && -d /private/tmp/lab6_f1tenth_gym_ros/maps ]]; then
    SIM_ROOT=/private/tmp/lab6
else
    SIM_ROOT="${TMPDIR:-/tmp}/robertaris-racing"
fi
# Match pathlib.expanduser().resolve() in the bundled helper, including paths
# supplied with a trailing slash. All source directories derive from this prefix.
case "$SIM_ROOT" in
    '~/'*) SIM_ROOT="$HOME/${SIM_ROOT#\~/}" ;;
esac
SIM_ROOT="${SIM_ROOT%/}"
if [[ "$SIM_ROOT" != /* ]]; then
    SIM_ROOT="$PWD/$SIM_ROOT"
fi
mkdir -p "$(dirname "$SIM_ROOT")"
SIM_ROOT="$(cd "$(dirname "$SIM_ROOT")" && pwd -P)/$(basename "$SIM_ROOT")"
GYM_ROOT="${SIM_ROOT}_f1tenth_gym"
BRIDGE_ROOT="${SIM_ROOT}_f1tenth_gym_ros"
VENV="$PROJECT_DIR/.venv"
GYM_REVISION=ec3b1a4693ed20317e42e57a9454d94b824cec81
BRIDGE_REVISION=2c019810b4098ed82d44cad1d7d282a2dfc12421

command -v git >/dev/null 2>&1 || { printf 'Install Git, then run this script again.\n' >&2; exit 1; }

fetch_source() {
    local source_url="$1" revision="$2" destination="$3"
    if [[ ! -e "$destination/.git" ]]; then
        if [[ -e "$destination" ]]; then
            printf 'Path already exists: %s. Choose another RACING_SIM_ROOT.\n' "$destination" >&2
            exit 1
        fi
        mkdir -p "$(dirname "$destination")"
        git init --quiet "$destination"
        git -C "$destination" remote add origin "$source_url"
        git -C "$destination" fetch --quiet --depth 1 origin "$revision"
        git -C "$destination" checkout --quiet --detach FETCH_HEAD
    fi
    if [[ "$(git -C "$destination" rev-parse HEAD)" != "$revision" ]] ||
       [[ -n "$(git -C "$destination" status --porcelain --untracked-files=no)" ]]; then
        printf 'Expected clean revision %s in %s. Choose another RACING_SIM_ROOT.\n' "$revision" "$destination" >&2
        exit 1
    fi
}

fetch_source https://github.com/f1tenth/f1tenth_gym.git "$GYM_REVISION" "$GYM_ROOT"
fetch_source https://github.com/f1tenth/f1tenth_gym_ros.git "$BRIDGE_REVISION" "$BRIDGE_ROOT"

# The pinned upstream simulator requires Python >=3.12. uv can supply 3.13 when
# unavailable locally; without uv, use an existing suitable python3 interpreter.
if command -v uv >/dev/null 2>&1; then
    if [[ ! -x "$VENV/bin/python" ]]; then
        uv venv --python "${RACING_PYTHON:-3.13}" "$VENV"
    fi
    uv pip install --python "$VENV/bin/python" -e "$GYM_ROOT" -e "$PROJECT_DIR"
else
    SIM_PYTHON="${RACING_PYTHON:-python3}"
    command -v "$SIM_PYTHON" >/dev/null 2>&1 || { printf 'Install uv or Python 3.12+.\n' >&2; exit 1; }
    "$SIM_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Python 3.12+ required; install uv or set RACING_PYTHON")'
    if [[ ! -x "$VENV/bin/python" ]]; then
        "$SIM_PYTHON" -m venv "$VENV"
    fi
    "$VENV/bin/python" -m pip install -e "$GYM_ROOT" -e "$PROJECT_DIR"
fi

# Exercise source/revision checks, without triggering a lengthy JAX warmup.
RACING_SIM_ROOT="$SIM_ROOT" "$VENV/bin/python" -B -c 'from racing_dashboard.f1tenth_helper import SIMULATOR_ACTUAL_REVISIONS; print("Pinned simulator ready:", SIMULATOR_ACTUAL_REVISIONS)'
printf '\nInstalled dashboard and simulator in %s\n' "$VENV"
printf 'Run: RACING_SIM_ROOT=%q %q --repo /absolute/path/to/lab6\n' "$SIM_ROOT" "$VENV/bin/racing-dashboard"
printf 'First simulation start may take a minute while the simulator compiles.\n'
