#!/usr/bin/env bash
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(dirname "$SELF")"
LYR_PY="$HERE/lyr.py"

if [[ ! -f "$LYR_PY" ]]; then
    echo "lyr-launcher: cannot find lyr.py next to $SELF" >&2
    exit 1
fi

VENV_DIR="${LYR_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/lyr/venv}"
PY="${LYR_PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "lyr-launcher: $PY not found. Install Python 3.11+." >&2
    exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "lyr-launcher: lyr needs Python 3.11 or newer ($PY is $( "$PY" -V 2>&1 ))." >&2
    exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "lyr-launcher: creating venv in $VENV_DIR" >&2
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PY" -m venv --system-site-packages "$VENV_DIR"
fi

VPY="$VENV_DIR/bin/python"
REQ="$HERE/requirements.txt"
STAMP="$VENV_DIR/.requirements.stamp"

needs_install=0
if [[ -f "$REQ" ]]; then
    if [[ ! -f "$STAMP" || "$REQ" -nt "$STAMP" ]]; then
        needs_install=1
    fi
else
    if [[ ! -f "$STAMP" ]]; then
        needs_install=1
    fi
fi

if [[ "$needs_install" -eq 1 ]]; then
    "$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    if [[ -f "$REQ" ]]; then
        echo "lyr-launcher: installing requirements from $REQ" >&2
        "$VPY" -m pip install --quiet -r "$REQ"
    else
        echo "lyr-launcher: installing default requirements (Pillow, mutagen)" >&2
        "$VPY" -m pip install --quiet Pillow mutagen
    fi
    touch "$STAMP"
fi

exec "$VPY" "$LYR_PY" "$@"
