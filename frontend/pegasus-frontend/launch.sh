#!/usr/bin/env bash
set -euo pipefail

PEGASUS_PROGRAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PEGASUS_PROJECT_ROOT="$(cd -- "$PEGASUS_PROGRAM_ROOT/../.." && pwd -P)"
exec python3 -B "$PEGASUS_PROJECT_ROOT/tools/roms/materialize-collection-media-paths.py" launch-pegasus -- "$@"
