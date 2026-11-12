#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP="$SCRIPT_DIR/SkraperUI.exe"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd -P)"
CONFIGURATOR="$PROJECT_ROOT/frontend/ES-DE/configure-rom-directory.py"
if ! command -v python3 >/dev/null 2>&1; then
    printf '[ERRO] Python 3 é necessário para validar os caminhos portáveis do Skraper.\n' >&2
    exit 1
fi

if [[ ! -f "$APP" ]]; then
    printf '[ERRO] Executável do Skraper não encontrado: %s\n' "$APP" >&2
    exit 1
fi

if command -v mono >/dev/null 2>&1; then
    if ! python3 "$CONFIGURATOR" --preflight mono; then
        printf '[ERRO] O preflight de saídas recusou a configuração do Skraper.\n' >&2
        exit 1
    fi
    cd -- "$SCRIPT_DIR"
    exec python3 "$PROJECT_ROOT/tools/roms/romtool.py" media session run --client skraper --runtime mono -- mono "$APP" "$@"
fi

if ! command -v wine >/dev/null 2>&1; then
    printf '[ERRO] Skraper exige Mono ou Wine; nenhum runtime está disponível.\n' >&2
    exit 1
fi

if [[ -n "${WINEPREFIX:-}" && "$WINEPREFIX" != "$SCRIPT_DIR/UserData/WinePrefix" ]]; then
    printf '[ERRO] WINEPREFIX externo recusado; use o prefixo local do projeto.\n' >&2
    exit 1
fi
for directory in "$SCRIPT_DIR/UserData" "$SCRIPT_DIR/UserData/WinePrefix"; do
    if [[ -L "$directory" ]]; then
        printf '[ERRO] Prefixo Skraper por symlink recusado: %s\n' "$directory" >&2
        exit 1
    fi
done
export WINEPREFIX="$SCRIPT_DIR/UserData/WinePrefix"
mkdir -p -- "$WINEPREFIX"
if ! python3 "$CONFIGURATOR" --preflight wine; then
    printf '[ERRO] O preflight de saídas recusou a configuração do Skraper.\n' >&2
    exit 1
fi
cd -- "$SCRIPT_DIR"
exec python3 "$PROJECT_ROOT/tools/roms/romtool.py" media session run --client skraper --runtime wine -- wine "$APP" "$@"
