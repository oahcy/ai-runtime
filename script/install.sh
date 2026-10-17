#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${AI_RUNTIME_PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: Install Python 3.11+ or set AI_RUNTIME_PYTHON to its executable path.' >&2
    exit 1
fi
exec "$python_bin" "$script_dir/install.py" "$@"
