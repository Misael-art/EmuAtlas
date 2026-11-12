#!/usr/bin/env bash
# One portable entry point; preparation and supervision stay in the existing tool.
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
    printf '[Quiver] Python 3 é necessário para o preflight.\n' >&2
    exit 1
fi
case "${1:-}" in
    --diagnose|--diagnose-json|--check-library)
        exec python3 -B "$SCRIPT_DIR/structure-frontend.py" "$1" ;;
    --install)
        exec python3 -B "$SCRIPT_DIR/structure-frontend.py" --ensure-installation ;;
    --check)
        exec python3 -B "$SCRIPT_DIR/structure-frontend.py" --check-startup ;;
    --prepare)
        exec python3 -B "$SCRIPT_DIR/structure-frontend.py" --prepare-startup ;;
    --install-shortcuts)
        exec python3 -B "$SCRIPT_DIR/structure-frontend.py" --prepare-shortcuts ;;
    --rollback-startup)
        exec python3 -B "$SCRIPT_DIR/structure-frontend.py" --rollback-startup "${2:?Informe a operação de preparação}" ;;
esac
exec python3 -B "$SCRIPT_DIR/structure-frontend.py" --launch "$@"
