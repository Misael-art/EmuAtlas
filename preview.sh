#!/usr/bin/env bash
set -euo pipefail
project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -eq 0 ]]; then
    exec python3 -B "$project_dir/tools/roms/preview.py" serve --open
fi
exec python3 -B "$project_dir/tools/roms/preview.py" "$@"
