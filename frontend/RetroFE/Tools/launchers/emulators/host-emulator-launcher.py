#!/usr/bin/env python3
"""Dispatch a RetroFE item to the host emulator configured for its collection."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrofe_audio import run_with_music_paused


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (
            (candidate / "settings.conf").is_file()
            and (candidate / "Config/profiles/host-linux.json").is_file()
            and (candidate / "collections").is_dir()
        ):
            return candidate
    raise RuntimeError("Não encontrei a raiz portátil do RetroFE acima do launcher.")


ROOT = find_project_root()
PROFILE_PATH = ROOT / "Config/profiles/host-linux.json"
RUNTIME_OVERRIDE = None


def userdata_base() -> Path:
    base = ROOT.parent.parent / 'emulator/data'
    if base.parent.is_symlink() or base.is_symlink():
        raise ValueError('A raiz compartilhada emulator/data não pode ser symlink')
    return base


def archive_extractor() -> str | None:
    if os.name == 'nt':
        sys.path.insert(0, str(ROOT.parent.parent / 'tools/roms'))
        from portable_emulator import package_entry
        native = package_entry(ROOT.parent.parent, '7zip', 'windows')
        if native:
            return str(native)
    return shutil.which('7z') or shutil.which('7zz')


def set_toml_section(text: str, section: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    current = ""
    found = False
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            if current == section:
                result.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
            current = header.group(1)
            found = found or current == section
            result.append(line)
            continue
        if current == section:
            match = re.match(r"^\s*([^=]+?)\s*=", line)
            key = match.group(1).strip() if match else None
            if key in values:
                if key not in seen:
                    result.append(f"{key} = {values[key]}")
                    seen.add(key)
                continue
        result.append(line)
    if current == section:
        result.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
    elif not found:
        result.append("")
        result.append(f"[{section}]")
        result.extend(f"{key} = {value}" for key, value in values.items())
    return "\n".join(result) + "\n"


def set_conf_values(text: str, values: dict[str, str]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=", line)
        key = match.group(1) if match else None
        if key in values:
            if key not in seen:
                result.append(f"{key} = {values[key]}")
                seen.add(key)
        else:
            result.append(line)
    result.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
    return "\n".join(result) + "\n"


def set_ini_section(text: str, section: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    current = ""
    found = False
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            if current.casefold() == section.casefold():
                result.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
            current = header.group(1)
            found = found or current.casefold() == section.casefold()
            result.append(line)
            continue
        if current.casefold() == section.casefold():
            match = re.match(r"^\s*([^=]+?)\s*=", line)
            key = match.group(1).strip() if match else None
            if key in values:
                if key not in seen:
                    result.append(f"{key} = {values[key]}")
                    seen.add(key)
                continue
        result.append(line)
    if current.casefold() == section.casefold():
        result.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
    elif not found:
        result.extend(["", f"[{section}]"])
        result.extend(f"{key} = {value}" for key, value in values.items())
    return "\n".join(result) + "\n"


def resolve_host_path(value: str, variable: str) -> str:
    path = Path(os.environ.get(variable, value)).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return str(path.resolve())


def resolve_executable(value: str, variable: str) -> str:
    configured = os.environ.get(variable, value)
    path = Path(configured).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    if "/" in configured:
        return str((ROOT / path).resolve())
    return shutil.which(configured) or configured


def prepare_portable_config_home(app: dict, app_id: str, userdata: Path | None) -> Path | None:
    subdir = app.get("portable_xdg_config_home")
    if not subdir:
        return None
    if userdata is None:
        raise ValueError("O aplicativo declara XDG_CONFIG_HOME portátil, mas a coleção não declara userdata_subdir.")

    userdata_root = userdata.resolve()
    config_home = (userdata_root / subdir).resolve()
    if not config_home.is_relative_to(userdata_root):
        raise ValueError(f"XDG_CONFIG_HOME precisa permanecer dentro de {userdata_root}.")
    config_home.mkdir(parents=True, exist_ok=True)

    legacy_subdir = app.get("legacy_xdg_config_subdir")
    if legacy_subdir:
        legacy_root = Path.home() / ".var" / "app" / app_id / "config"
        legacy_config = (legacy_root / legacy_subdir).resolve()
        portable_config = (config_home / legacy_subdir).resolve()
        if not legacy_config.is_relative_to(legacy_root.resolve()):
            raise ValueError("A migração da configuração antiga precisa permanecer dentro do diretório Flatpak.")
        if not portable_config.is_relative_to(config_home):
            raise ValueError(f"A configuração antiga precisa permanecer dentro de {config_home}.")
        if legacy_config.is_dir() and not portable_config.exists():
            with tempfile.TemporaryDirectory(prefix=".flycast-config-migration-", dir=config_home) as staging:
                staged_config = Path(staging) / portable_config.name
                shutil.copytree(legacy_config, staged_config)
                try:
                    os.rename(staged_config, portable_config)
                except OSError:
                    if not portable_config.exists():
                        raise
    return config_home


def copy_legacy_flatpak_dir(legacy_root: Path, subdir: str, portable_home: Path) -> None:
    """Copy a Flatpak-private config/data folder into the project once; the original stays untouched."""
    legacy = (legacy_root / subdir).resolve()
    portable = (portable_home / subdir).resolve()
    if not legacy.is_relative_to(legacy_root.resolve()):
        raise ValueError("A migração antiga precisa permanecer dentro do diretório Flatpak.")
    if not portable.is_relative_to(portable_home):
        raise ValueError(f"A cópia migrada precisa permanecer dentro de {portable_home}.")
    if legacy.is_dir() and not portable.exists():
        with tempfile.TemporaryDirectory(prefix=".flatpak-migration-", dir=portable_home) as staging:
            staged = Path(staging) / portable.name
            shutil.copytree(legacy, staged, symlinks=True)
            try:
                os.rename(staged, portable)
            except OSError:
                if not portable.exists():
                    raise


def prepare_portable_data_home(app: dict, userdata: Path | None, app_id: str = "") -> Path | None:
    subdir = app.get("portable_xdg_data_home")
    if not subdir:
        return None
    if userdata is None:
        raise ValueError("O aplicativo declara XDG_DATA_HOME portátil, mas a coleção não declara userdata_subdir.")
    userdata_root = userdata.resolve()
    data_home = (userdata_root / subdir).resolve()
    if not data_home.is_relative_to(userdata_root):
        raise ValueError(f"XDG_DATA_HOME precisa permanecer dentro de {userdata_root}.")
    data_home.mkdir(parents=True, exist_ok=True)
    if app.get("legacy_xdg_data_subdir") and app_id:
        copy_legacy_flatpak_dir(Path.home() / ".var" / "app" / app_id / "data", app["legacy_xdg_data_subdir"], data_home)
    return data_home


def resolve_profile_paths(profile: dict) -> None:
    profile["rom_root"] = resolve_host_path(profile["rom_root"], "RETROFE_ROM_ROOT")
    profile["bios_root"] = resolve_host_path(profile["bios_root"], "RETROFE_BIOS_ROOT")
    profile["mame_executable"] = resolve_executable(profile["mame_executable"], "RETROFE_MAME")
    ra = profile["retroarch"]
    ra["core_root"] = resolve_host_path(ra["core_root"], "RETROFE_RETROARCH_CORE_ROOT")


def resolve_collection_for_item(
    profile: dict,
    requested_collection: str,
    item: Path,
) -> tuple[str | None, dict | None, str | None]:
    """Resolve a merged collection item to its source system without guessing."""
    collections = profile.get("collections", {})
    direct = collections.get(requested_collection)
    if isinstance(direct, dict):
        return requested_collection, direct, None

    overrides = profile.get("subcollection_overrides", {})
    for parent_name, parent in collections.items():
        if requested_collection in parent.get("subcollections", []):
            system = {**parent, **overrides.get(requested_collection, {})}
            return parent_name, system, None

    rom_root = Path(profile["rom_root"]).resolve()
    candidates: list[tuple[int, str, dict]] = []
    for source_name, source in collections.items():
        subdir = str(source.get("rom_subdir", "")).strip()
        if not subdir:
            continue
        source_root = (rom_root / subdir).resolve()
        try:
            item.relative_to(source_root)
        except ValueError:
            continue
        candidates.append((len(source_root.parts), source_name, source))

    if not candidates:
        return None, None, (
            f"A coleção {requested_collection!r} não está no perfil host-linux e "
            "o caminho do item não corresponde a uma plataforma configurada."
        )
    deepest = max(depth for depth, _name, _source in candidates)
    matches = [(name, source) for depth, name, source in candidates if depth == deepest]
    if len(matches) != 1:
        names = ", ".join(sorted(name for name, _source in matches))
        return None, None, (
            f"A origem do item da coleção {requested_collection!r} é ambígua: {names}. "
            "Nenhum emulador foi iniciado."
        )
    source_name, source = matches[0]
    return source_name, source, None


# "profile" keeps each profile's own arguments; the Preview may request an explicit mode.
DISPLAY_MODE = "profile"
FULLSCREEN_FLAGS = {"-fullscreen", "--fullscreen", "-full-screen"}


def apply_display_mode(arguments: list[str]) -> list[str]:
    """Remove declared fullscreen flags for windowed mode; never invent a flag for an emulator."""
    if DISPLAY_MODE != "windowed":
        if DISPLAY_MODE == "fullscreen" and not FULLSCREEN_FLAGS & set(arguments):
            print("O perfil não declara opção de tela cheia; o emulador usa a própria configuração.", flush=True)
        return arguments
    result: list[str] = []
    skip_value = False
    for index, value in enumerate(arguments):
        if skip_value:
            skip_value = False
            continue
        if value in FULLSCREEN_FLAGS:
            # `--fullscreen true` is a key/value pair in some emulator CLIs.
            skip_value = index + 1 < len(arguments) and arguments[index + 1] in {"true", "1"}
            continue
        result.append(value)
    return result


ARCHIVE_MAX_MEMBERS = 256
ARCHIVE_MAX_EXPANDED_BYTES = 8 * 1024**3
ARCHIVE_DISK_RESERVE_BYTES = 512 * 1024**2
ARCHIVE_LIST_TIMEOUT = 60
ARCHIVE_EXTRACT_TIMEOUT = 900


def list_archive_members(extractor: str, item: Path) -> tuple[list[dict], str]:
    """Read the archive table without writing; returns (members, refusal)."""
    try:
        listed = subprocess.run([extractor, "l", "-slt", "-ba", "-p", str(item)], capture_output=True, text=True,
                                errors="replace", check=False, timeout=ARCHIVE_LIST_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [], "listagem do arquivo excedeu o tempo limite"
    if listed.returncode:
        return [], "listagem falhou (arquivo corrompido ou protegido): " + (listed.stderr or listed.stdout)[-300:]
    members, current = [], {}
    for line in listed.stdout.splitlines() + [""]:
        if not line.strip():
            if "Path" in current:
                members.append(current)
            current = {}
            continue
        key, sep, value = line.partition(" = ")
        if sep:
            current[key.strip()] = value
    return members, ""


def vet_archive_members(members: list[dict], free_bytes: int) -> tuple[int, str]:
    if not members:
        return 0, "arquivo vazio ou formato não reconhecido"
    if len(members) > ARCHIVE_MAX_MEMBERS:
        return 0, f"{len(members)} membros excede o limite de {ARCHIVE_MAX_MEMBERS}"
    seen, total = set(), 0
    for member in members:
        name = member.get("Path", "")
        pure = Path(name.replace("\\", "/"))
        if not name or pure.is_absolute() or ".." in pure.parts or name.startswith("/"):
            return 0, f"caminho inseguro no arquivo: {name!r}"
        attributes = member.get("Attributes", "")
        if member.get("Encrypted") == "+":
            return 0, f"membro criptografado: {name}"
        if member.get("Symbolic Link") or any(re.fullmatch(r"l[rwxsStT-]{9}", token) for token in attributes.split()):
            return 0, f"link simbólico no arquivo: {name}"
        key = pure.as_posix().casefold()
        if key in seen:
            return 0, f"colisão de nomes no arquivo: {name}"
        seen.add(key)
        if member.get("Folder") != "+":
            try:
                total += int(member.get("Size") or 0)
            except ValueError:
                return 0, f"tamanho ilegível para {name}"
    if total > ARCHIVE_MAX_EXPANDED_BYTES:
        return total, f"conteúdo expandido de {total} bytes excede o orçamento"
    if total + ARCHIVE_DISK_RESERVE_BYTES > free_bytes:
        return total, f"espaço livre insuficiente: {total} bytes + reserva de {ARCHIVE_DISK_RESERVE_BYTES}"
    return total, ""


def reap_stale_extraction_caches(cache_root: Path, max_age_seconds: int = 12 * 3600) -> list[str]:
    """Crash recovery: remove only this launcher's own expired temporary trees (never other files)."""
    import time
    removed = []
    if cache_root.is_symlink() or not cache_root.is_dir():
        return removed
    for child in cache_root.iterdir():
        if (child.name.startswith(("retrofe-retroarch-", "retrofe-nds-")) and child.is_dir() and not child.is_symlink()
                and time.time() - child.lstat().st_mtime > max_age_seconds):
            shutil.rmtree(child)
            removed.append(child.name)
    return removed


def safe_extract_archive(extractor: str, item: Path, staging: Path) -> str:
    """F08: list and vet before writing; bounded extraction; only our own process group is killed."""
    members, refusal = list_archive_members(extractor, item)
    if refusal:
        return refusal
    _total, refusal = vet_archive_members(members, shutil.disk_usage(staging).free)
    if refusal:
        return refusal
    process = subprocess.Popen([extractor, "x", "-y", "-p", f"-o{staging}", str(item)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=ARCHIVE_EXTRACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, 9)
        process.wait()
        return "extração excedeu o tempo limite; temporário próprio descartado"
    if process.returncode:
        return "falha ao extrair: " + (output or "")[-400:]
    root = staging.resolve()
    for path in root.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return f"conteúdo extraído contém link ou caminho externo: {path.name}"
    return ""


def notify(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    executable = shutil.which("notify-send")
    if executable:
        subprocess.run([executable, "RetroFE", message], check=False)


def show_onboarding_guide(collection: str, system: dict, guide: dict) -> bool:
    display = str(system.get("display_name", collection))
    title = str(guide.get("title", f"Primeiro uso: {display}"))
    intro = str(guide.get("intro", ""))
    steps = guide.get("steps", [])
    lines = [intro, "", "Etapas do assistente:"]
    if isinstance(steps, list):
        for index, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                step_title = str(step.get("title", f"Etapa {index}"))
                instruction = str(step.get("instruction", ""))
                lines.append(f"{index}. {step_title}: {instruction}")
    completion = str(guide.get("completion", ""))
    if completion:
        lines.extend(["", f"Conclusão: {completion}"])
    message = "\n".join(lines)

    kdialog = shutil.which("kdialog")
    zenity = shutil.which("zenity")
    if kdialog:
        command = [
            kdialog, "--title", title, "--yes-label", "Abrir assistente",
            "--no-label", "Agora não", "--yesno", message,
        ]
    elif zenity:
        command = [
            zenity, "--question", f"--title={title}", f"--text={message}",
            "--width=760", "--ok-label=Abrir assistente", "--cancel-label=Agora não",
        ]
    else:
        print(f"\n{title}\n{message}\n", file=sys.stderr, flush=True)
        notify(f"{display}: o assistente de primeiro uso será aberto; consulte as etapas impressas no terminal.")
        return True

    try:
        result = subprocess.run(command, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        notify(f"Não consegui abrir o guia de {display}: {error}. O assistente nativo será aberto.")
        return True
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        notify(f"Configuração inicial de {display} adiada; nenhum jogo foi iniciado.")
        return False
    notify(f"O guia de {display} terminou com código {result.returncode}; o assistente nativo será aberto.")
    return True


def prepare_onboarding_defaults(collection: str, system: dict) -> tuple[int, bool]:
    if not system.get("onboarding_defaults") and not system.get("onboarding_manual_requirements"):
        return 0, True
    project_root = ROOT.parents[1]
    emutool = project_root / "tools/roms/emutool.py"
    if not emutool.is_file():
        notify(f"emutool.py não está disponível para preparar o primeiro uso de {collection}.")
        return 2, False
    try:
        result = subprocess.run(
            [sys.executable, str(emutool), "--apply-onboarding", collection],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        notify(f"Falha ao preparar o primeiro uso de {collection}: {error}")
        return 2, False
    if result.stdout.strip():
        print(result.stdout.rstrip(), flush=True)
    if result.returncode:
        message = result.stderr.strip() or f"emutool.py retornou {result.returncode}."
        notify(f"Onboarding de {collection} bloqueado: {message}")
        return result.returncode, False
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr, flush=True)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    onboarding = payload.get("onboarding", {}) if isinstance(payload, dict) else {}
    if isinstance(onboarding, dict) and onboarding.get("status") == "manual_required":
        guide = onboarding.get("guide")
        if isinstance(guide, dict):
            if not show_onboarding_guide(collection, system, guide):
                return 0, False
        else:
            display = system.get("display_name", collection)
            notify(f"{display}: conclua o assistente de primeiro uso. As escolhas manuais continuam com você.")
    return 0, True


def disk_order(path: Path) -> tuple:
    """Boot disk first (Program/System/Boot, then Disk A/1), then natural order by name."""
    name = path.name.casefold()
    boot = 0 if re.search(r"(program|system|boot) disk", name) else 1
    natural = [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]
    return (boot, natural)


def launch_retroarch(profile: dict, system: dict, item: Path) -> int:
    ra = profile["retroarch"]
    native = ra.get('executable')
    flatpak = shutil.which("flatpak")
    if not native and not flatpak:
        notify("Flatpak não está disponível para iniciar o núcleo do RetroArch.")
        return 2
    bios_root = Path(profile["bios_root"]).resolve()
    missing_bios = [name for name in system.get("bios_files", []) if not (bios_root / name).is_file()]
    if missing_bios:
        notify(f"BIOS ausentes para {system.get('display_name', system.get('rom_subdir', 'este sistema'))}: {', '.join(missing_bios)}")
        return 2
    # A collection may use a project-local core folder (installed by emutool) instead of the host RetroArch cores.
    core_root = (ROOT / system["core_root"]).resolve() if system.get("core_root") else Path(ra["core_root"]).resolve()
    core = core_root / system["core"]
    if not core.is_file():
        notify(f"Núcleo do RetroArch não encontrado: {core}")
        return 2
    writable = (ROOT / ra["userdata_root"]).resolve()
    writable.mkdir(parents=True, exist_ok=True)
    for subdirectory in ("config", "playlists", "autoconfig", "assets", "cache", "cheats", "database", "info",
                         "overlays", "shaders", "screenshots", "recordings", "remaps", "logs"):
        (writable / subdirectory).mkdir(parents=True, exist_ok=True)
    config_path = ROOT / ra["config_path"]
    if not config_path.is_file():
        notify(f"Configuração do RetroArch não encontrada: {config_path}")
        return 2
    config_text = config_path.read_text(encoding="utf-8", errors="replace")
    config_text = re.sub(r"(?m)^config_directory = " + re.escape(json.dumps(str(writable / "config"))) + r"\n", "", config_text)
    config_text = set_conf_values(config_text, {
        "system_directory": json.dumps(profile["bios_root"]),
        "libretro_directory": json.dumps(str(core_root)),
        "savefile_directory": json.dumps(str((ROOT / ra["savefile_directory"]).resolve())),
        "savestate_directory": json.dumps(str((ROOT / ra["savestate_directory"]).resolve())),
        "config_save_on_exit": "false",
        "rgui_config_directory": json.dumps(str(writable / "config")),
        "core_options_path": json.dumps(str(writable / "retroarch-core-options.cfg")),
        "core_info_cache_enable": "false",
        **{key: json.dumps(str(writable / folder)) for key, folder in {
            'assets_directory': 'assets', 'cache_directory': 'cache', 'cheat_database_path': 'cheats',
            'cheat_settings_path': 'cheats', 'content_database_path': 'database/rdb',
            'cursor_directory': 'database/cursors', 'libretro_info_path': 'info',
            'overlay_directory': 'overlays', 'osk_overlay_directory': 'overlays/keyboards',
            'video_shader_dir': 'shaders', 'screenshot_directory': 'screenshots',
            'recording_output_directory': 'recordings', 'recording_config_directory': 'config/record',
            'input_remapping_directory': 'remaps', 'joypad_autoconfig_dir': 'autoconfig',
            'log_dir': 'logs'}.items()},
        "playlist_directory": json.dumps(str(writable / "playlists")),
        **{key: json.dumps(str(writable / "playlists")) for key in (
            "content_favorites_directory", "content_history_directory", "content_image_history_directory",
            "content_music_history_directory", "content_video_directory")},
        **{key: json.dumps(str(writable / "playlists" / filename)) for key, filename in {
            "content_history_path": "content_history.lpl", "content_favorites_path": "content_favorites.lpl",
            "content_music_history_path": "content_music_history.lpl", "content_video_history_path": "content_video_history.lpl",
            "content_image_history_path": "content_image_history.lpl"}.items()},
    })
    config_path.write_text(config_text, encoding="utf-8")
    extract_extensions = {str(ext).casefold() if str(ext).startswith(".") else f".{str(ext).casefold()}" for ext in system.get("extract_extensions", [])}
    extracted_extensions = {str(ext).casefold() if str(ext).startswith(".") else f".{str(ext).casefold()}" for ext in system.get("extracted_extensions", [])}
    cache_root = (ROOT / "../../emulator/data/ROMCache").resolve()
    if item.suffix.casefold() in extract_extensions:
        cache_root.mkdir(parents=True, exist_ok=True)
        reap_stale_extraction_caches(cache_root)
        temp_context = tempfile.TemporaryDirectory(prefix="retrofe-retroarch-", dir=cache_root)
    else:
        temp_context = nullcontext(None)

    with temp_context as temp_dir:
        launched_item = item
        if temp_dir is not None:
            extractor = archive_extractor()
            if not extractor:
                notify("O comando 7z é necessário para abrir este jogo compactado no RetroArch.")
                return 2
            refusal = safe_extract_archive(extractor, item, Path(temp_dir))
            if refusal:
                notify(f"{item.name}: {refusal}")
                return 2
            staging = Path(temp_dir).resolve()
            entries = list(staging.rglob("*"))
            if any(path.is_symlink() or not path.resolve().is_relative_to(staging) for path in entries):
                notify(f"O arquivo {item.name} contém caminho simbólico ou externo; a extração foi bloqueada.")
                return 2
            files = sorted(path for path in entries if path.is_file())
            if (system.get("extract_playlist") and len(files) > 1
                    and all(path.suffix.casefold() in extracted_extensions for path in files)):
                # Multi-disk archive: a temporary M3U in the cache, boot/program/system disk first.
                files.sort(key=disk_order)
                playlist = staging / (item.stem + ".m3u")
                playlist.write_text("".join(f"{path.relative_to(staging)}\n" for path in files), encoding="utf-8")
                files = [playlist]
                extracted_extensions = extracted_extensions | {".m3u"}
            if len(files) != 1 or files[0].suffix.casefold() not in extracted_extensions:
                notify(f"O arquivo {item.name} contém {len(files)} arquivos; esperado exatamente um payload entre {', '.join(sorted(extracted_extensions))}.")
                return 2
            launched_item = files[0]

        command = [
            flatpak, "run",
            f"--filesystem={profile['rom_root']}:ro",
            f"--filesystem={profile['bios_root']}:ro",
            f"--filesystem={core_root}:ro",
            f"--filesystem={writable}:rw",
        ]
        if temp_dir is not None:
            command.append(f"--filesystem={cache_root}:ro")
        command.extend([
            "--command=retroarch", ra["app_id"],
            "--config", str(config_path.resolve()),
            *(["--verbose"] if os.environ.get("RETROFE_EMULATOR_DIAGNOSTICS") == "1" else []),
            "-L", str(core), *([] if DISPLAY_MODE == "windowed" else ["--fullscreen"]), str(launched_item),
        ])
        if native:
            command = [str(native), '--config', str(config_path.resolve()), '-L', str(core),
                       *([] if DISPLAY_MODE == 'windowed' else ['--fullscreen']), str(launched_item)]
        project = ROOT.parent.parent
        journey = {"system_id": system["rom_subdir"], "rom": item.relative_to(project).as_posix(),
                   "core": core.relative_to(project).as_posix(),
                   "content": launched_item.relative_to(project).as_posix(),
                   "config": config_path.resolve().relative_to(project).as_posix()}
        launch_env = dict(os.environ, APPIMAGELAUNCHER_DISABLE="1") if native and str(native).endswith(".AppImage") else None
        return run_with_music_paused(command, cwd=ROOT, env=launch_env,
                                     system_id=system["rom_subdir"], journey=journey)


def launch_flatpak(profile: dict, system: dict, item: Path) -> int:
    app_id = system["app_id"]
    app = profile["flatpak_apps"].get(app_id)
    native = app.get('executable') if app else None
    if native and os.name == 'nt':
        try:
            from portable_emulator import assert_native_ready
            assert_native_ready(ROOT.parent.parent, system.get('native_runtime_id', ''))
        except (OSError, ValueError) as error:
            notify(str(error))
            return 2
    flatpak = shutil.which("flatpak")
    if not native and not flatpak:
        notify("Flatpak não está disponível neste host.")
        return 2
    if not app:
        notify(f"O perfil não declara o comando do Flatpak {app_id}.")
        return 2

    rom_root = Path(profile["rom_root"]).resolve() / system["rom_subdir"]
    bios_root = Path(profile["bios_root"]).resolve()
    missing_bios = [filename for filename in system.get("bios_files", []) if not (bios_root / filename).is_file()]
    if missing_bios:
        notify(f"BIOS ausentes para {system.get('display_name', system['rom_subdir'])}: {', '.join(missing_bios)}")
        return 2
    args_map = {
        "item": str(item),
        "bios_dc": str((bios_root / system.get("bios_subdir", "")).resolve()),
        "bios_root": str(bios_root),
    }
    userdata = None
    env_overrides: list[str] = []
    if system.get("userdata_subdir"):
        userdata = safe_userdata_path(str(system["userdata_subdir"]))
        userdata.mkdir(parents=True, exist_ok=True)
        args_map["userdata"] = str(userdata)
    try:
        portable_config_home = prepare_portable_config_home(app, app_id, userdata)
        portable_data_home = prepare_portable_data_home(app, userdata, app_id)
    except (OSError, ValueError) as error:
        notify(f"Não consegui preparar a configuração portátil de {app_id}: {error}")
        return 2
    if portable_config_home is not None:
        env_overrides.append(f"--env=XDG_CONFIG_HOME={portable_config_home}")
    if portable_data_home is not None:
        env_overrides.append(f"--env=XDG_DATA_HOME={portable_data_home}")
    if userdata is not None:
        cache_home = userdata / 'cache'
        cache_home.mkdir(parents=True, exist_ok=True)
        env_overrides.append(f'--env=XDG_CACHE_HOME={cache_home}')

    with (tempfile.TemporaryDirectory(prefix="retrofe-nds-", dir=(ROOT / "../../emulator/data/ROMCache")) if system.get("extract_extensions") else _null_context()) as temp_dir:
        launched_item = item
        if system.get("extract_extensions") and item.suffix.lower() in system["extract_extensions"]:
            if not temp_dir:
                notify("Não foi possível criar a pasta temporária para extrair o jogo de Nintendo DS.")
                return 2
            extractor = archive_extractor()
            if not extractor:
                notify("O comando 7z é necessário para abrir jogos de Nintendo DS compactados.")
                return 2
            refusal = safe_extract_archive(extractor, item, Path(temp_dir))
            if refusal:
                notify(f"{item.name}: {refusal}")
                return 2
            matches = sorted(p for p in Path(temp_dir).rglob("*") if p.is_file() and p.suffix.lower() in system.get("extracted_extensions", []))
            if len(matches) != 1:
                notify(f"O arquivo {item.name} contém {len(matches)} jogos .nds; esperado exatamente um.")
                return 2
            launched_item = matches[0]
            args_map["item"] = str(launched_item)

        if app_id == "net.kuribo64.melonDS":
            melonds = profile["melonDS"]
            config_path = ROOT / melonds["config_path"]
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if not config_path.is_file():
                notify(f"Configuração do melonDS ausente: {config_path}")
                return 2
            stem = launched_item.stem
            current = config_path.read_text(encoding="utf-8", errors="replace")
            current = set_toml_section(current, "DS", {
                key: json.dumps(str(Path(profile["bios_root"]) / filename))
                for key, filename in melonds["bios_files"].items()
            })
            current = set_toml_section(current, "Instance0", {
                "SaveFilePath": json.dumps(str((ROOT / melonds["userdata_root"] / "saves" / stem).resolve())),
                "SavestatePath": json.dumps(str((ROOT / melonds["userdata_root"] / "states" / stem).resolve())),
                "CheatFilePath": json.dumps(str((ROOT / melonds["userdata_root"] / "cheats" / stem).resolve())),
            })
            config_path.write_text(current, encoding="utf-8")
            env_overrides.append(f"--env={melonds['config_environment']}={(ROOT / melonds['userdata_root'] / 'config').resolve()}")

        if app_id == "net.pcsx2.PCSX2":
            # PCSX2 appends a `PCSX2` directory to the custom -datapath.
            # Its active INI is therefore nested below ../../emulator/data/PCSX2.
            pcsx2_config = ROOT / "../../emulator/data/PCSX2/PCSX2/inis/PCSX2.ini"
            if pcsx2_config.is_file():
                current = pcsx2_config.read_text(encoding="utf-8", errors="replace")
                current = set_ini_section(current, "Folders", {"Bios": profile["bios_root"]})
                current = set_ini_section(current, "GameList", {
                    "RecursivePaths": str(Path(profile["rom_root"]) / "ps2")
                })
                pcsx2_config.write_text(current, encoding="utf-8")

        if app_id == "app.xemu.xemu":
            # xemu.toml needs absolute paths; rewrite them from the current project root on every launch.
            if userdata is None:
                notify("O perfil do xemu precisa declarar userdata_subdir.")
                return 2
            hdd = userdata / "xbox_hdd.qcow2"
            if not hdd.is_file() or hdd.is_symlink():
                notify("Imagem de HD do Xbox ausente. Instale com: tools/roms/emutool.py install asset --asset-id xemu-hdd --apply")
                return 2
            xemu_config = userdata / "xemu.toml"
            current = xemu_config.read_text(encoding="utf-8") if xemu_config.is_file() else ""
            current = set_toml_section(current, "sys.files", {
                "bootrom_path": json.dumps(str(bios_root / "mcpx_1.0.bin")),
                "flashrom_path": json.dumps(str(bios_root / "Complex_4627.bin")),
                "hdd_path": json.dumps(str(hdd)),
                "eeprom_path": json.dumps(str(userdata / "eeprom.bin")),
            })
            # The first-run welcome skips the flash ROM; the launcher already supplies every machine file.
            current = set_toml_section(current, "general", {"show_welcome": "false"})
            xemu_config.write_text(current.rstrip("\n") + "\n", encoding="utf-8")

        command = [
            flatpak, "run",
            f"--filesystem={rom_root}:ro",
            f"--filesystem={bios_root}:ro",
        ]
        if userdata:
            command.append(f"--filesystem={userdata}:rw")
        if system.get("extract_extensions"):
            command.append(f"--filesystem={(ROOT / '../../emulator/data/ROMCache').resolve()}:ro")
        if env_overrides:
            # Flatpak forces XDG_* inside the sandbox and ignores --env for them; `env` sets them for the app itself.
            command.extend(["--command=env", app_id, *(value.removeprefix("--env=") for value in env_overrides), app["command"]])
        else:
            command.extend([f"--command={app['command']}", app_id])
        command.extend(apply_display_mode([part.format_map(args_map) for part in system.get("args", ["{item}"])]))
        if native:
            launch_env = os.environ.copy()
            launch_env.update(dict(value.removeprefix('--env=').split('=', 1) for value in env_overrides))
            launch_env['APPIMAGELAUNCHER_DISABLE'] = '1'
            command = [str(native), *apply_display_mode([part.format_map(args_map) for part in system.get('args', ['{item}'])])]
            working_directory = ROOT
            if system.get('working_directory') and userdata:
                working_directory = ensure_userdata_directory(system['working_directory'].format_map(args_map), userdata)
            return run_with_music_paused(command, cwd=working_directory, env=launch_env, system_id=system['rom_subdir'])
        return run_with_music_paused(command, cwd=ROOT, system_id=system["rom_subdir"])


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        return False


def launch_named_mame(profile: dict, item: Path) -> int:
    mame = Path(profile["mame_executable"])
    if not mame.is_file() or not os.access(mame, os.X_OK):
        notify(f"MAME não está disponível: {mame}")
        return 2
    user_root = (ROOT / profile["mame_user_data"] / "named").resolve()
    config_dirs = {name: user_root / (profile.get('mame_config_subdir', 'cfg') if name == 'cfg' else name)
                   for name in ("cfg", "nvram", "input", "state", "snap", "diff")}
    for directory in config_dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    rompath = os.pathsep.join([str(Path(profile['rom_root']).resolve() / 'mame'), str(Path(profile['bios_root']).resolve())])
    command = [str(mame), item.stem, "-rompath", rompath]
    for option, name in (("-cfg_directory", "cfg"), ("-nvram_directory", "nvram"), ("-input_directory", "input"), ("-state_directory", "state"), ("-snapshot_directory", "snap"), ("-diff_directory", "diff")):
        command.extend([option, str(config_dirs[name])])
    return run_with_music_paused(command, cwd=ROOT, system_id="mame")


def launch_command(profile: dict, system: dict, item: Path) -> int:
    selection_info = resolve_runtime_choice(system, item, forced_runtime=RUNTIME_OVERRIDE)
    if isinstance(selection_info, str):
        notify(selection_info)
        return 2
    system, runtime_id, title_id, eboot = selection_info
    if os.name == 'nt':
        try:
            from portable_emulator import assert_native_ready
            assert_native_ready(ROOT.parent.parent, system.get('native_runtime_id', ''))
        except (OSError, ValueError) as error:
            notify(str(error))
            return 2
    command_value = str(system.get("command", ""))
    executable = resolve_executable(command_value, str(system.get("command_env", "")))
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        notify(f"Executável do emulador não encontrado ou não executável: {command_value}")
        return 2

    rom_root = Path(profile["rom_root"]).resolve() / str(system.get("rom_subdir", ""))
    rom_root = rom_root.resolve()
    if not item.resolve().is_relative_to(rom_root):
        notify(f"O item precisa permanecer na pasta da plataforma: {rom_root}")
        return 2
    if item.is_dir() and not system.get("accepts_directories"):
        notify(f"O perfil não permite iniciar diretórios de jogo: {item}")
        return 2

    bios_root = Path(profile["bios_root"]).resolve()
    missing_bios = [name for name in system.get("bios_files", []) if not (bios_root / name).is_file()]
    if missing_bios:
        notify(f"BIOS ausentes para {system.get('display_name', system['rom_subdir'])}: {', '.join(missing_bios)}")
        return 2

    args_map = {
        "item": str(item.resolve()),
        "rom_root": str(rom_root),
        "bios_root": str(bios_root),
    }
    model2_native = (os.name == 'nt' and system.get('native_runtime_id') == 'model2'
                     and system.get('rom_subdir') == 'model2')
    if model2_native:
        if item.suffix.lower() != '.zip' or not re.fullmatch(r'[A-Za-z0-9_-]+', item.stem):
            notify('Model 2 exige ZIP com driver explícito na biblioteca canônica.')
            return 2
        args_map['item_stem'] = item.stem
    userdata_subdir = system.get("userdata_subdir")
    if userdata_subdir:
        try:
            userdata = safe_userdata_path(str(userdata_subdir))
        except ValueError as error:
            notify(str(error))
            return 2
        userdata.mkdir(parents=True, exist_ok=True)
        args_map["userdata"] = str(userdata)
    if title_id:
        args_map["title_id"] = title_id
    if eboot:
        args_map["eboot"] = str(eboot)
        args_map['game_dir']=str(eboot.parent)

    arguments = system.get("args", ["{item}"])
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        notify("O perfil command precisa declarar args como uma lista de textos.")
        return 2
    has_item = any("{item}" in value for value in arguments)
    has_eboot = any("{eboot}" in value for value in arguments)
    has_game_dir=any('{game_dir}' in value for value in arguments)
    if not has_item and not ((has_eboot or has_game_dir) and eboot is not None) and not (model2_native and arguments == ['{item_stem}']):
        notify("O perfil command precisa encaminhar {item}, {eboot} ou {game_dir} validado ao emulador.")
        return 2
    try:
        command = [executable, *apply_display_mode([value.format_map(args_map) for value in arguments])]
    except KeyError as error:
        notify(f"Placeholder não reconhecido no perfil command: {error}")
        return 2

    environment = system.get("environment", {})
    if not isinstance(environment, dict):
        notify("O perfil command precisa declarar environment como um objeto de variáveis.")
        return 2
    launch_env = os.environ.copy()
    for name, template in environment.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            notify(f"Nome inválido de variável de ambiente no perfil command: {name!r}")
            return 2
        if not isinstance(template, str):
            notify(f"Valor de ambiente precisa ser texto: {name}")
            return 2
        try:
            value = template.format_map(args_map)
        except KeyError as error:
            notify(f"Placeholder não reconhecido no ambiente do perfil command: {error}")
            return 2
        if name in {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"}:
            configured_path = Path(value).expanduser().resolve()
            userdata_root = Path(args_map.get("userdata", "")).resolve()
            if not args_map.get("userdata") or not configured_path.is_relative_to(userdata_root):
                notify(f"{name} precisa permanecer dentro do userdata_subdir declarado.")
                return 2
            configured_path.mkdir(parents=True, exist_ok=True)
            value = str(configured_path)
        launch_env[name] = value

    working_directory = ROOT
    working_template = system.get("working_directory")
    if model2_native and working_template == '{program_dir}':
        working_directory = Path(executable).parent
        expected = ROOT.parent.parent / 'emulator/windows/model2'
        if working_directory.resolve() != expected.resolve() or expected.is_symlink():
            notify('Diretório de execução Model 2 saiu do pacote canônico.')
            return 2
        working_template = None
    if working_template is not None:
        if not isinstance(working_template, str) or not args_map.get("userdata"):
            notify("working_directory exige texto e userdata_subdir no perfil command.")
            return 2
        try:
            relative_working = working_template.format_map(args_map)
        except KeyError as error:
            notify(f"Placeholder não reconhecido em working_directory: {error}")
            return 2
        try:
            working_directory = ensure_userdata_directory(relative_working, Path(args_map["userdata"]))
        except (OSError, ValueError) as error:
            notify(f"Diretório de execução fora do userdata declarado: {error}")
            return 2

    if runtime_id:
        print(f"Runtime selecionado ({system['rom_subdir']}): {runtime_id}" + (f" ({title_id})" if title_id else ""), flush=True)
    return run_with_music_paused(command, cwd=working_directory, env=launch_env if environment else None,
                                 system_id=system["rom_subdir"])


def safe_userdata_path(relative: str) -> Path:
    base = userdata_base()
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or any(part in {".", ".."} for part in relative_path.parts):
        raise ValueError("userdata_subdir precisa ser relativo a UserData/ e sem traversal.")
    if base.is_symlink():
        raise ValueError("A raiz UserData não pode ser symlink.")
    cursor = base
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink em UserData: {cursor}")
    resolved = cursor.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError("userdata_subdir saiu de UserData/.")
    return cursor


def ensure_userdata_directory(value: str, userdata: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    base = userdata.resolve()
    if userdata.is_symlink() or not path.resolve().is_relative_to(base):
        raise ValueError(f"{path} não está abaixo de {userdata}.")
    try:
        relative = path.relative_to(userdata)
    except ValueError as error:
        raise ValueError(f"{path} não está abaixo do userdata declarado.") from error
    cursor = userdata
    for part in relative.parts:
        if part in {".", ".."}:
            raise ValueError("working_directory contém traversal.")
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink no diretório de execução: {cursor}")
        cursor.mkdir(exist_ok=True)
        if not cursor.is_dir():
            raise ValueError(f"Componente de working_directory não é diretório: {cursor}")
    return cursor.resolve()


def _runtime_item_identity(system: dict, item: Path) -> tuple[str, Path | None, str]:
    if system.get('rom_subdir')=='switch2':
        try:
            sys.path.insert(0,str(ROOT.parents[1]/'tools/roms'))
            import eden_runtime
            directory,reason,tid=eden_runtime.resolve_dump(item,ROOT.parents[1]/'roms/switch2')
            if any((directory/name).is_symlink() for name in ['main','main.npdm']):
                return '',None,'Dependência ExeFS por symlink recusada'
            return f'{tid:016x}',directory/'main',''
        except (OSError,ValueError) as error:return '',None,str(error)
    identity = system.get("runtime_identity")
    if not item.is_dir() or not isinstance(identity, dict):
        return "", None, ""
    metadata_relative = Path(str(identity.get("metadata_path", "")))
    executable_relative = Path(str(identity.get("executable_path", "")))
    for relative in (metadata_relative, executable_relative):
        if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
            return "", None, "runtime_identity contém caminho inválido."
        cursor = item
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return "", None, f"Componente symlink em runtime_identity: {relative}"
    metadata = item / metadata_relative
    if metadata.is_symlink() or not metadata.is_file():
        return "", None, f"Metadado de identidade ausente ou symlink: {metadata_relative}"
    try:
        if metadata.stat().st_size > 1024 * 1024:
            return "", None, f"Metadado de identidade excede 1 MiB: {metadata_relative}"
        document = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return "", None, f"Não consegui ler {metadata_relative}: {error}"
    if not isinstance(document, dict):
        return "", None, f"Metadado de identidade não é um objeto JSON: {metadata_relative}"
    title_id = str(document.get(str(identity.get("metadata_key", "TITLE_ID")), "")).strip()
    pattern = str(identity.get("title_id_pattern", ""))
    if not title_id or not pattern or not re.fullmatch(pattern, title_id):
        return "", None, f"TITLE_ID inválido em {metadata_relative}: {title_id!r}"
    eboot = item / executable_relative
    if eboot.is_symlink() or not eboot.is_file():
        return title_id, None, f"Executável PS5 ausente ou symlink: {executable_relative}"
    if not eboot.resolve().is_relative_to(item.resolve()):
        return title_id, None, "eboot.bin sai do diretório canônico do jogo."
    return title_id, eboot.resolve(), ""


def resolve_runtime_choice(system: dict, item: Path, *, forced_runtime=None):
    options = system.get("runtime_options", {})
    if not options:
        return system, "", "", None
    if not isinstance(options, dict):
        return "runtime_options precisa ser um objeto."
    default_id = str(system.get("runtime_id", "")).strip()
    if not default_id:
        return "O perfil com runtime_options precisa declarar runtime_id."
    title_id, eboot, identity_error = _runtime_item_identity(system, item)
    selected_id = default_id
    selection_path_value = str(system.get("runtime_selection_path", ""))
    if selection_path_value:
        selection_path = Path(selection_path_value)
        if selection_path.is_absolute():
            return "runtime_selection_path precisa ser relativo e sem traversal."
        selection_path = Path(os.path.abspath(ROOT / selection_path))
        userdata_root = userdata_base()
        if not selection_path.resolve().is_relative_to(userdata_root.resolve()):
            return "runtime_selection_path precisa permanecer dentro de UserData/."
        cursor = userdata_root
        try:
            relative = selection_path.relative_to(userdata_root)
        except ValueError:
            return "runtime_selection_path precisa permanecer dentro de UserData/."
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return f"Componente symlink no arquivo de seleção de runtime: {cursor}"
        if selection_path.exists():
            try:
                if selection_path.stat().st_size > 1024 * 1024:
                    return "Arquivo de seleção de runtime excede 1 MiB; nenhum emulador foi iniciado."
                payload = json.loads(selection_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                return f"Arquivo de seleção de runtime inválido: {error}"
            if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("selections"), dict):
                return "Arquivo de seleção de runtime tem schema inválido; nenhum emulador foi iniciado."
            selections = payload["selections"]
            if payload.get("system_id") != str(system.get("rom_subdir", "")):
                return "Arquivo de seleção de runtime pertence a outra plataforma."
            allowed_ids = {default_id, *options.keys()}
            preferred=payload.get('preferred_runtime_id',default_id)
            if not isinstance(preferred,str) or preferred not in allowed_ids:return 'Runtime preferido não declarado; seleção preservada.'
            selected_id=preferred
            identity = system.get("runtime_identity", {})
            title_id_pattern = str(identity.get("title_id_pattern", "")) if isinstance(identity, dict) else ""
            for saved_title_id, saved_runtime_id in selections.items():
                if (not isinstance(saved_title_id, str) or not title_id_pattern
                        or not re.fullmatch(title_id_pattern, saved_title_id)
                        or not isinstance(saved_runtime_id, str) or saved_runtime_id not in allowed_ids):
                    return "Arquivo de seleção contém Title ID ou runtime desconhecido; nenhum emulador foi iniciado."
            if title_id:
                selected_id = str(selections.get(title_id, selected_id))
    choices = {default_id: system, **options}
    if forced_runtime:
        if forced_runtime not in choices:return 'Runtime explícito não declarado para esta plataforma.'
        selected_id=forced_runtime
    selected = choices.get(selected_id)
    if not isinstance(selected, dict):
        return f"Runtime {selected_id!r} não existe no perfil da plataforma."
    input_type = "directory" if item.is_dir() else item.suffix.casefold().lstrip(".")
    if system.get('rom_subdir')=='switch2' and eboot is not None:input_type='exefs'
    supported = selected.get("supported_inputs")
    if selected_id == default_id:
        supported = ["directory" if value == "game_directory" else str(value).casefold().lstrip(".") for value in system.get("launch_inputs", [])]
    if not isinstance(supported, list) or input_type not in {str(value).casefold().lstrip(".") for value in supported}:
        name = selected.get("display_name", selected_id)
        return f"{name} não aceita entrada {input_type!r}; o arquivo foi preservado e nenhum emulador foi iniciado."
    if selected_id != default_id:
        if not title_id and input_type!='zar':
            return f"Não consegui ler um TITLE_ID único no diretório PS5: {identity_error or 'identidade ausente'}."
        if eboot is None and input_type!='zar':
            return identity_error or "eboot.bin não foi validado no diretório PS5."
        selected = {**system, **selected}
    return selected, selected_id, title_id, eboot


MANAGED_LAUNCH_ALIASES = {
    # runtime_id -> (alias suffix, required leaf name of the target). F04: only these leaf aliases are
    # followed; every other symlink in a collection item stays rejected.
    "eden-nx2": (".exefs", "main"),
}


def validate_managed_launch_alias(raw_item: Path, platform_root: Path, system: dict) -> tuple[Path | None, str]:
    """Return the vetted target of a managed relative launch alias, or an explicit refusal."""
    contract = MANAGED_LAUNCH_ALIASES.get(str(system.get("runtime_id", "")))
    if not contract or contract[0].lstrip(".") not in system.get("launch_inputs", []):
        return None, "O perfil desta plataforma não declara aliases de lançamento gerenciados."
    suffix, leaf = contract
    if raw_item.suffix != suffix or raw_item.parent != platform_root:
        return None, f"Alias gerenciado precisa ser {suffix} diretamente em {platform_root}."
    if platform_root.is_symlink() or any(parent.is_symlink() for parent in platform_root.parents):
        return None, "A raiz da plataforma ou um ancestral é symlink; alias recusado."
    literal = os.readlink(raw_item)
    literal_path = Path(literal)
    if literal_path.is_absolute() or ".." in literal_path.parts or not literal_path.parts:
        return None, f"Alias precisa ser relativo e interno à plataforma: {literal}"
    component = platform_root
    for part in literal_path.parts:
        component = component / part
        if component.is_symlink():
            return None, f"Alvo do alias contém outro symlink (loop/redirecionamento recusado): {component}"
    try:
        target = (platform_root / literal_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"Alvo do alias ausente ou em loop: {literal}"
    if not target.is_relative_to(platform_root.resolve()) or target.name != leaf or not target.is_file():
        return None, f"Alvo do alias precisa ser {leaf} regular dentro da plataforma: {target}"
    try:
        with target.open("rb") as stream:
            magic = stream.read(4)
        npdm = (target.parent / "main.npdm").read_bytes()
    except OSError as exc:
        return None, f"ExeFS ilegível: {exc}"
    if magic != b"NSO0":
        return None, "main do alias não é NSO0."
    if len(npdm) < 0x80 or npdm[:4] != b"META":
        return None, "main.npdm ausente ou sem META."
    offset = int.from_bytes(npdm[0x70:0x74], "little")
    if offset + 24 > len(npdm) or npdm[offset:offset + 4] != b"ACI0":
        return None, "main.npdm sem ACI0 válido."
    return target, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", required=True)
    parser.add_argument("--item", required=True)
    parser.add_argument('--runtime',help='alternativa declarada no perfil; não altera jogos nem a preferência persistente')
    parser.add_argument("--display", choices=["profile", "fullscreen", "windowed"], default="profile",
                        help="profile mantém os argumentos do perfil (padrão do RetroFE)")
    args = parser.parse_args()
    global RUNTIME_OVERRIDE
    RUNTIME_OVERRIDE=args.runtime
    global DISPLAY_MODE
    DISPLAY_MODE = args.display
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    sys.path.insert(0, str(ROOT.parent.parent / 'tools/roms'))
    from portable_emulator import native_profile
    profile = native_profile(ROOT.parent.parent, profile)
    resolve_profile_paths(profile)
    raw_item = Path(args.item).expanduser()
    if not raw_item.is_absolute():
        raw_item = ROOT / raw_item
    raw_item = Path(os.path.abspath(raw_item))
    try:
        item_stat = raw_item.lstat()
    except OSError:
        notify(f"Arquivo/diretório da coleção não encontrado: {raw_item}")
        return 2
    managed_alias = stat.S_ISLNK(item_stat.st_mode)
    if managed_alias:
        # Resolved only for collection lookup; the alias contract is enforced below, before launch.
        try:
            raw_item.resolve(strict=True)
        except (OSError, RuntimeError):
            notify(f"O item da coleção é symlink quebrado ou em loop: {raw_item}")
            return 2
    elif not (stat.S_ISREG(item_stat.st_mode) or stat.S_ISDIR(item_stat.st_mode)):
        notify(f"O item da coleção precisa ser arquivo regular ou diretório de jogo: {raw_item}")
        return 2
    item = raw_item.resolve(strict=True)
    source_collection, system, resolution_error = resolve_collection_for_item(
        profile,
        args.collection,
        item,
    )
    if resolution_error or not system or not source_collection:
        notify(resolution_error or f"A coleção {args.collection!r} não está no perfil host-linux.")
        return 2
    if source_collection != args.collection:
        print(
            f"Coleção mesclada {args.collection!r}: item pertence a {source_collection!r}.",
            flush=True,
        )

    # A profile may explicitly allow game directories. Check the original path
    # components below that platform root before resolving them, so a symlink
    # cannot redirect a launch outside the canonical ROM tree.
    platform_root = Path(os.path.abspath(Path(profile["rom_root"]) / str(system.get("rom_subdir", ""))))
    try:
        relative_item = raw_item.relative_to(platform_root)
    except ValueError:
        notify(f"O item precisa permanecer na pasta canônica da plataforma: {platform_root}")
        return 2
    if platform_root.is_symlink():
        notify(f"A raiz da plataforma não pode ser symlink: {platform_root}")
        return 2
    if managed_alias:
        target, refusal = validate_managed_launch_alias(raw_item, platform_root, system)
        if target is None:
            notify(f"O item da coleção é symlink não gerenciado e não será seguido: {raw_item}. {refusal}")
            return 2
        print(f"Alias gerenciado {raw_item.name} → {target.relative_to(platform_root.resolve())}", flush=True)
        item = target
        relative_item = Path(raw_item.name)
    component = platform_root
    for part in ([] if managed_alias else relative_item.parts):
        component = component / part
        if component.is_symlink():
            notify(f"O caminho do item contém symlink e não será seguido: {component}")
            return 2
    if item.is_dir() and not system.get("accepts_directories"):
        notify(f"O perfil não permite iniciar diretórios de jogo: {item}")
        return 2

    adapter = system["adapter"]
    if adapter == "unavailable":
        notify(system.get("reason", f"Não há emulador configurado para {source_collection}."))
        return 2
    onboarding_result, should_launch = ((0, True) if os.name == 'nt' else
                                       prepare_onboarding_defaults(source_collection, system))
    if onboarding_result:
        return onboarding_result
    if not should_launch:
        return 0
    if adapter in {"mame", "mame_named"} and DISPLAY_MODE != "profile":
        print("O modo de tela do MAME segue a configuração do perfil MAME.", flush=True)
    if adapter == "mame":
        launcher = ROOT / "Tools/launchers/emulators/mame-profile-launcher.py"
        return subprocess.run([sys.executable, str(launcher), "--collection", source_collection, "--item", str(item)], cwd=ROOT, check=False).returncode
    if adapter == "mame_named":
        return launch_named_mame(profile, item)
    if adapter == "retroarch":
        return launch_retroarch(profile, system, item)
    if adapter in ("flatpak", "native_app"):
        return launch_flatpak(profile, system, item)
    if adapter == "command":
        return launch_command(profile, system, item)
    notify(f"Adaptador não reconhecido para {args.collection}: {adapter}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
