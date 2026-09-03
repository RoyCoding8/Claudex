#!/usr/bin/env bash
set -euo pipefail

source="${BASH_SOURCE[0]}"
while [ -L "$source" ]; do
    dir="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    [ "${source#/}" = "$source" ] && source="$dir/$source"
done
CX_DIR="$(cd -P "$(dirname "$source")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required: https://github.com/astral-sh/uv" >&2
    exit 127
fi

clear 2>/dev/null || true
exec uv run --project "$CX_DIR" python "$CX_DIR/cx.py" "$@"
