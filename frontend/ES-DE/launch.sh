#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_IMAGE="$SCRIPT_DIR/ES-DE_x64_SteamDeck.AppImage"
LAUNCHER="$SCRIPT_DIR/launch.sh"
ROM_DIRECTORY_CONFIGURATOR="$SCRIPT_DIR/configure-rom-directory.py"
FRONTEND_STRUCTURER="$SCRIPT_DIR/structure-frontend.py"
DESKTOP_ID='es-de-canonical.desktop'
MANAGED_MARKER='X-EmulationProject-Managed=ES-DE'
SHORTCUTS_CHANGED=0
DISPLAY_ARGUMENTS=()

report() {
    printf '%s\n' "$*" >&2
}

show_error() {
    local message="$1"
    # F03: diagnostic/CI callers (Preview, installer) set ESDE_NONINTERACTIVE=1 and read stderr.
    if [[ "${ESDE_NONINTERACTIVE:-0}" == 1 ]]; then
        report "$message"
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$message" --title 'ES-DE — biblioteca de ROMs'
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v zenity >/dev/null 2>&1; then
        zenity --error --text "$message" --title 'ES-DE — biblioteca de ROMs'
    else
        report "$message"
    fi
}

configure_library() {
    local mode="$1" output
    if ! command -v python3 >/dev/null 2>&1; then
        show_error 'Python 3 é necessário para atualizar es_settings.xml com segurança; o ES-DE não foi aberto para evitar a biblioteca padrão ~/ROMs.'
        return 1
    fi
    if ! output="$(python3 "$ROM_DIRECTORY_CONFIGURATOR" "$mode" 2>&1)"; then
        show_error "$output"
        return 1
    fi
    [[ -n "$output" ]] && report "$output"
    return 0
}

desktop_exec_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

check_desktop_destination() {
    local destination="$1"
    if [[ -L "$destination" ]]; then
        report "[AVISO] Atalho ES-DE ignorado: o destino é um symlink preexistente: $destination"
        return 1
    fi
    if [[ -e "$destination" ]] && ! grep -Fqx "$MANAGED_MARKER" "$destination"; then
        report "[AVISO] Atalho ES-DE ignorado para preservar um arquivo não gerenciado: $destination"
        return 1
    fi
    return 0
}

write_desktop_entry() {
    local destination="$1"
    local escaped_launcher temporary
    check_desktop_destination "$destination" || return 1
    escaped_launcher="$(desktop_exec_quote "$LAUNCHER")"
    temporary="$(mktemp "${destination}.XXXXXX")" || return 1
    if ! cat > "$temporary" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=ES-DE
Name[pt_BR]=ES-DE (Frontend de jogos)
GenericName=Emulation frontend
GenericName[pt_BR]=Frontend de emulação
Comment=Browse and launch games
Comment[pt_BR]=Navegue e inicie jogos
Exec=$escaped_launcher
Path=$SCRIPT_DIR
Icon=applications-games
Terminal=false
StartupNotify=true
Categories=Game;Emulator;
$MANAGED_MARKER
EOF
    then
        rm -f -- "$temporary"
        return 1
    fi
    if [[ -f "$destination" ]] && cmp -s "$temporary" "$destination"; then
        rm -f -- "$temporary"
    else
        mv -f -- "$temporary" "$destination" || return 1
        SHORTCUTS_CHANGED=1
    fi
    chmod 755 -- "$destination" || return 1
}

install_kde_shortcuts() {
    local desktop_dir='' data_home applications_dir changed_before="$SHORTCUTS_CHANGED"
    local destinations=()
    data_home="${XDG_DATA_HOME:-${HOME:-/tmp}/.local/share}"
    if command -v xdg-user-dir >/dev/null 2>&1; then
        desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    fi
    [[ -n "$desktop_dir" ]] || desktop_dir="${HOME:-/tmp}/Desktop"
    applications_dir="$data_home/applications"
    destinations=("$desktop_dir/$DESKTOP_ID" "$applications_dir/$DESKTOP_ID")

    if ! check_desktop_destination "${destinations[0]}" || ! check_desktop_destination "${destinations[1]}"; then
        return 1
    fi
    if ! mkdir -p -- "$desktop_dir" "$applications_dir"; then
        report '[AVISO] Não foi possível preparar as pastas de atalhos do KDE.'
        return 1
    fi
    if ! write_desktop_entry "${destinations[0]}" || ! write_desktop_entry "${destinations[1]}"; then
        report '[AVISO] Não foi possível criar ou atualizar os atalhos do ES-DE.'
        return 1
    fi

    if [[ "$SHORTCUTS_CHANGED" != "$changed_before" ]]; then
        if command -v kbuildsycoca6 >/dev/null 2>&1; then
            kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
        elif command -v kbuildsycoca5 >/dev/null 2>&1; then
            kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
        fi
        report '[OK] Atalho do Desktop e entrada do menu KDE instalados/atualizados.'
    else
        report '[OK] Atalhos do Desktop e do menu KDE já estavam atualizados.'
    fi
}

if [[ -z "${ESDE_APPDATA_DIR:-}" ]]; then
    ESDE_APPDATA_DIR="$SCRIPT_DIR/UserData"
elif [[ "$ESDE_APPDATA_DIR" != "$SCRIPT_DIR/UserData" ]]; then
    show_error "ESDE_APPDATA_DIR deve ser $SCRIPT_DIR/UserData para manter uma única estrutura portátil."
    exit 1
fi
export ESDE_APPDATA_DIR
for argument in "$@"; do
    case "$argument" in
        --home|--home=*|--create-system-dirs|--create-system-dirs=*)
            show_error 'Este argumento altera a raiz portátil ou cria pastas de ROMs fora do fluxo de organização; use as ferramentas do projeto.'
            exit 1
            ;;
    esac
done

if [[ "${1:-}" == '--diagnose-frontend' ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        show_error 'Python 3 é necessário para diagnosticar a estrutura do frontend.'
        exit 1
    fi
    python3 "$FRONTEND_STRUCTURER" --diagnose || exit $?
    configure_library --check || exit $?
    exit 0
fi

if [[ "${1:-}" == '--install-frontend' ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        show_error 'Python 3 é necessário para validar ou baixar o AppImage do ES-DE.'
        exit 1
    fi
    python3 "$FRONTEND_STRUCTURER" --ensure-installation
    exit $?
fi

if [[ "${1:-}" == '--check-library' ]]; then
    configure_library --check
    exit $?
fi

if [[ "${1:-}" == '--configure-library' ]]; then
    configure_library --apply
    exit $?
fi

if [[ "${1:-}" == '--install-shortcuts' ]]; then
    install_kde_shortcuts
    exit $?
fi

select_display() {
    local -a launch_args=("$@")
    local index=0 argument value
    for ((index = 0; index < ${#launch_args[@]}; index++)); do
        argument="${launch_args[index]}"
        if [[ "$argument" == '--display' ]]; then
            if ((index + 1 >= ${#launch_args[@]})); then
                show_error 'O argumento --display precisa de um índice entre 1 e 4.'
                return 1
            fi
            value="${launch_args[index + 1]}"
            if [[ ! "$value" =~ ^[1-4]$ ]]; then
                show_error "Índice de tela inválido para ES-DE: $value (use 1 a 4)."
                return 1
            fi
            report "[ES-DE] Usando o índice de tela informado: $value."
            return 0
        elif [[ "$argument" == --display=* ]]; then
            value="${argument#--display=}"
            if [[ ! "$value" =~ ^[1-4]$ ]]; then
                show_error "Índice de tela inválido para ES-DE: $value (use 1 a 4)."
                return 1
            fi
            report "[ES-DE] Usando o índice de tela informado: $value."
            return 0
        fi
    done

    if [[ -n "${ESDE_DISPLAY_INDEX:-}" ]]; then
        value="$ESDE_DISPLAY_INDEX"
        if [[ ! "$value" =~ ^[1-4]$ ]]; then
            show_error 'ESDE_DISPLAY_INDEX precisa ser um número entre 1 e 4.'
            return 1
        fi
    else
        if ! command -v python3 >/dev/null 2>&1; then
            show_error 'Python 3 é necessário para identificar a tela ativa do KDE.'
            return 1
        fi
        value="$(python3 "$FRONTEND_STRUCTURER" --display-index)" || return 1
        if [[ ! "$value" =~ ^[1-4]$ ]]; then
            show_error "O diagnóstico da tela retornou um índice inválido: $value."
            return 1
        fi
    fi
    DISPLAY_ARGUMENTS=(--display "$value")
    report "[ES-DE] Abrindo no índice de tela $value, detectado pela saída ativa do KDE."
}

if ! command -v python3 >/dev/null 2>&1; then
    show_error 'Python 3 é necessário para preparar o ES-DE com segurança.'
    exit 1
fi
if ! python3 "$FRONTEND_STRUCTURER" --ensure-installation; then
    show_error 'A estruturação ou validação do AppImage ES-DE falhou; consulte o diagnóstico no terminal.'
    exit 1
fi

# The frontend's utility flags do not open a window and should not rewrite its display preference.
case "${1:-}" in
    --version|-v|--help|-h)
        export APPIMAGELAUNCHER_DISABLE=1
        exec "$APP_IMAGE" "$@"
        ;;
    *)
        select_display "$@" || exit 1
        ;;
esac

# Restore the native command projection after moving the library between hosts.
if ! projection_output="$(python3 "$SCRIPT_DIR/../../tools/roms/portable_emulator.py" --prepare-frontends --apply 2>&1)"; then
    show_error "Comandos de emulação do ES-DE não puderam ser preparados: ${projection_output}"
    exit 1
fi

# F02: a new platform/category gets only its managed relative ES-DE aliases (idempotent, locked,
# frontends closed); an occupied or divergent alias blocks with the cause instead of being replaced.
if ! aliases_output="$(python3 "$SCRIPT_DIR/../../tools/roms/romtool.py" media esde aliases ensure 2>&1 >/dev/null)"; then
    show_error "Aliases de mídia ES-DE não puderam ser preparados: ${aliases_output}"
    exit 1
fi
if ! python3 "$ROM_DIRECTORY_CONFIGURATOR" --preflight; then
    show_error 'O preflight de saídas recusou a configuração. Um fluxo gerenciado não abre com destino fora da mídia canônica.'
    exit 1
fi
if ! recovery_output="$(python3 "$SCRIPT_DIR/../../tools/roms/romtool.py" media session recover 2>&1)"; then
    show_error "$recovery_output"
    exit 1
fi
install_kde_shortcuts || true
if ! theme_output="$(python3 "$FRONTEND_STRUCTURER" --prepare-theme 2>&1)"; then
    show_error "Não foi possível preparar o tema compartilhado do ES-DE: ${theme_output}"
    exit 1
fi
export APPIMAGELAUNCHER_DISABLE=1
exec python3 "$SCRIPT_DIR/../RetroFE/Tools/launchers/retrofe/steam-input.py" \
    --root "$SCRIPT_DIR/../RetroFE" session-run -- \
    python3 "$SCRIPT_DIR/../../tools/roms/romtool.py" media session run --client esde -- \
    "$APP_IMAGE" "${DISPLAY_ARGUMENTS[@]}" "$@"
