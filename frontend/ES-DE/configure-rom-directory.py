#!/usr/bin/env python3
"""Keep ES-DE ROMs and game media on the project's shared canonical roots."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
ROM_ROOT = PROJECT_ROOT / "roms"
MEDIA_ROOT = PROJECT_ROOT / "media"
ROMTOOL = PROJECT_ROOT / "tools" / "roms" / "romtool.py"
sys.path.insert(0, str(ROMTOOL.parent))
from esde_media import appdata_root
SETTING_NAME = "ROMDirectory"
MEDIA_SETTING_NAME = "MediaDirectory"
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
STRING_TAG_RE = re.compile(r"<string\b[^>]*>", re.DOTALL)
NAME_ATTR_RE = re.compile(r"\sname\s*=\s*([\"'])(.*?)\1", re.DOTALL)
VALUE_ATTR_RE = re.compile(r"\svalue\s*=\s*([\"'])(.*?)\1", re.DOTALL)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_rom_root() -> tuple[Path, list[Path], int]:
    if not (PROJECT_ROOT / "AGENTS.md").is_file():
        raise RuntimeError(f"Não encontrei a raiz oficial do projeto acima de {SCRIPT_DIR}.")
    if ROM_ROOT.is_symlink() or not ROM_ROOT.is_dir():
        raise RuntimeError(f"A raiz canônica de ROMs não existe como diretório real: {ROM_ROOT}")

    system_dirs: list[Path] = []
    non_empty = 0
    try:
        for child in sorted(ROM_ROOT.iterdir(), key=lambda item: item.name.casefold()):
            if child.name.startswith("."):
                continue
            if child.is_symlink():
                raise RuntimeError(
                    f"A raiz de ROMs contém uma entrada de plataforma symlink; revisão necessária: {child}"
                )
            if not child.is_dir():
                continue
            system_dirs.append(child)
            try:
                next(child.iterdir())
                non_empty += 1
            except StopIteration:
                pass
    except OSError as exc:
        raise RuntimeError(f"Não consegui inspecionar os diretórios de {ROM_ROOT}: {exc}") from exc

    if not system_dirs or not non_empty:
        raise RuntimeError(
            f"A raiz canônica {ROM_ROOT} não tem diretórios de plataforma preenchidos; "
            "não vou criar a árvore padrão do ES-DE."
        )
    return ROM_ROOT.resolve(), system_dirs, non_empty


def project_media_root() -> Path:
    if MEDIA_ROOT.is_symlink() or not MEDIA_ROOT.is_dir():
        raise RuntimeError(f"A raiz canônica de mídia não existe como diretório real: {MEDIA_ROOT}")
    if not any(child.is_dir() and not child.is_symlink() and child.name != "pending" for child in MEDIA_ROOT.iterdir()):
        raise RuntimeError(f"A raiz de mídia não tem plataformas preparadas: {MEDIA_ROOT}")
    result = subprocess.run([sys.executable, str(ROMTOOL), "media", "esde", "validate"],
                            cwd=PROJECT_ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"A validação da mídia compartilhada ES-DE falhou: {detail}")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("romtool retornou resultado inválido na validação da mídia ES-DE.") from exc
    if report.get("status") != "ready":
        raise RuntimeError("A raiz compartilhada de mídia não passou no preflight ES-DE: "
                           + "; ".join(report.get("issues", [])))
    return MEDIA_ROOT.resolve()


def app_data_dir() -> Path:
    return appdata_root(PROJECT_ROOT, os.environ.get("ESDE_APPDATA_DIR"))


def settings_file() -> Path:
    app_data = app_data_dir()
    legacy = Path.home() / "ES-DE"
    if os.path.lexists(legacy):
        raise RuntimeError(f"Dados ES-DE ainda presentes em {legacy}; execute romtool media esde userdata plan/apply/verify antes de iniciar. Não vou criar outra raiz.")
    settings_dir = app_data / "settings"
    for path in (app_data, settings_dir):
        if path.is_symlink():
            raise RuntimeError(f"Não vou alterar configuração através de symlink: {path}")
    config = settings_dir / "es_settings.xml"
    if config.is_symlink():
        raise RuntimeError(f"Não vou substituir um arquivo de configuração symlink: {config}")
    if config.exists() and not config.is_file():
        raise RuntimeError(f"O caminho de configuração não é um arquivo regular: {config}")
    return config


def setting_entries(text: str, setting_name: str = SETTING_NAME, tag_name: str = "string") -> list[tuple[re.Match[str], str | None]]:
    searchable = COMMENT_RE.sub(lambda match: " " * len(match.group(0)), text)
    entries: list[tuple[re.Match[str], str | None]] = []
    pattern=STRING_TAG_RE if tag_name=="string" else re.compile(r"<bool\b[^>]*>",re.DOTALL)
    for match in pattern.finditer(searchable):
        raw_tag = text[match.start() : match.end()]
        if not raw_tag.rstrip().endswith("/>"):
            name_match = NAME_ATTR_RE.search(raw_tag)
            if name_match and name_match.group(2) == setting_name:
                raise RuntimeError(f"A entrada {setting_name} não usa um elemento XML autocontido conhecido.")
            continue
        try:
            element = ET.fromstring(raw_tag)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"Encontrei uma entrada <string> inválida em es_settings.xml: {exc}"
            ) from exc
        if element.tag == tag_name and element.get("name") == setting_name:
            entries.append((match, element.get("value")))
    if len(entries) > 1:
        raise RuntimeError(f"es_settings.xml contém mais de uma entrada {setting_name}; não vou escolher por inferência.")
    return entries


def normalized_setting_path(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    return os.path.normpath(os.path.abspath(expanded))


def updated_setting_text(text: str, setting_name: str, target_value: str, tag_name: str = "string") -> tuple[str, str | None]:
    entries = setting_entries(text, setting_name, tag_name)
    previous_value = entries[0][1] if entries else None
    if entries and normalized_setting_path(previous_value) == target_value:
        return text, previous_value

    escaped = html.escape(target_value, quote=True)
    if entries:
        match = entries[0][0]
        raw_tag = text[match.start() : match.end()]
        value_matches = list(VALUE_ATTR_RE.finditer(raw_tag))
        if len(value_matches) > 1:
            raise RuntimeError(f"A entrada {setting_name} contém atributos value duplicados.")
        if value_matches:
            value_match = value_matches[0]
            quote = value_match.group(1)
            replacement = f' value={quote}{escaped}{quote}'
            new_tag = (
                raw_tag[: value_match.start()]
                + replacement
                + raw_tag[value_match.end() :]
            )
        else:
            close = raw_tag.rfind("/>")
            if close < 0:
                raise RuntimeError(f"A entrada {setting_name} não está fechada como esperado.")
            new_tag = raw_tag[:close].rstrip() + f' value="{escaped}"' + raw_tag[close:]
        result = text[: match.start()] + new_tag + text[match.end() :]
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        indent = ""
        for line in text.splitlines():
            if line.lstrip().startswith("<string"):
                indent = line[: len(line) - len(line.lstrip())]
                break
        separator = "" if not text or text.endswith(("\n", "\r")) else newline
        result = text + separator + f'{indent}<{tag_name} name="{setting_name}" value="{escaped}" />{newline}'

    verified = setting_entries(result, setting_name, tag_name)
    if len(verified) != 1 or verified[0][1] != target_value:
        raise RuntimeError(f"A pré-validação de {setting_name} resultou em configuração inesperada.")
    return result, previous_value


def updated_settings_text(text: str, rom_root: Path, media_root: Path) -> tuple[str, dict[str, str | None]]:
    updated, previous_rom = updated_setting_text(text, SETTING_NAME, str(rom_root))
    updated, previous_media = updated_setting_text(updated, MEDIA_SETTING_NAME, str(media_root))
    for name in ("ScraperOverwriteData","MiximageOverwrite"):
        updated,_=updated_setting_text(updated,name,"false",tag_name="bool")
    return updated, {SETTING_NAME: previous_rom, MEDIA_SETTING_NAME: previous_media}


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def preserve_backup(config: Path, original: bytes, mode: int) -> Path:
    digest = sha256_bytes(original)
    base = f"{config.name}.pre-esde-project-paths-{digest[:12]}"
    for suffix in range(1, 1000):
        name = f"{base}.bak" if suffix == 1 else f"{base}-{suffix}.bak"
        backup = config.with_name(name)
        if backup.is_symlink():
            continue
        if backup.exists():
            if backup.is_file() and sha256_bytes(backup.read_bytes()) == digest:
                return backup
            continue
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(backup, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(backup, min(mode, 0o600))
        except Exception:
            try:
                backup.unlink()
            except OSError:
                pass
            raise
        return backup
    raise RuntimeError("Não encontrei um nome livre e seguro para o backup do es_settings.xml.")


def running_appimage_pid() -> int | None:
    app_image = os.fsencode(str(SCRIPT_DIR / "ES-DE_x64_SteamDeck.AppImage"))
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit() or int(proc_dir.name) in {os.getpid(), os.getppid()}:
            continue
        try:
            command_line = (proc_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if app_image in command_line:
            return int(proc_dir.name)
    return None


def record_change(
    config: Path,
    original: bytes | None,
    updated: bytes,
    backup: Path | None,
    previous_values: dict[str, str | None],
    target_roots: dict[str, Path],
    system_dirs: list[Path],
    non_empty_dirs: int,
) -> str:
    details = {
        "product": "ES-DE",
        "configuration_path": str(config),
        "configuration_path_project_relative": config.relative_to(PROJECT_ROOT).as_posix(),
        "settings": list(target_roots),
        "project_boolean_defaults": {"ScraperOverwriteData": False, "MiximageOverwrite": False},
        "previous_values": previous_values,
        "new_values": {name: str(path) for name, path in target_roots.items()},
        "source_sha256": sha256_bytes(original) if original is not None else None,
        "source_size_bytes": len(original) if original is not None else None,
        "destination_sha256": sha256_bytes(updated),
        "destination_size_bytes": len(updated),
        "backup_path": str(backup) if backup is not None else None,
        "backup_sha256": sha256_bytes(original) if backup is not None and original is not None else None,
        "project_rom_root": "roms",
        "project_media_root": "media",
        "existing_platform_directory_count": len(system_dirs),
        "non_empty_platform_directory_count": non_empty_dirs,
        "rom_files_moved_or_created": False,
        "media_files_moved_or_created": False,
        "default_rom_tree_created": False,
    }
    command = [
        sys.executable,
        str(ROMTOOL),
        "record-change",
        "--change-type",
        "esde-shared-library-path-autoconfiguration",
        "--reason",
        "Configure ES-DE to read the existing canonical ROM and shared media roots after the ES-DE media migration and alias preflight.",
        "--details-json",
        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Não consegui registrar a alteração no histórico do projeto: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        return str(json.loads(result.stdout).get("operation_id", "recorded"))
    except (json.JSONDecodeError, AttributeError):
        return "recorded"


def apply_configuration(check_only: bool) -> int:
    announce_stray(PROJECT_ROOT)
    target_root, system_dirs, non_empty_dirs = project_rom_root()
    target_media_root = project_media_root()
    config = settings_file()
    original: bytes | None = None
    mode = 0o644
    if config.exists():
        original = config.read_bytes()
        mode = stat.S_IMODE(config.stat().st_mode)
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"es_settings.xml não é UTF-8; mantive o arquivo intacto: {config}") from exc
    else:
        text = '<?xml version="1.0"?>\n'

    new_text, previous_values = updated_settings_text(text, target_root, target_media_root)
    updated = new_text.encode("utf-8")
    if updated == original:
        print(
            f"[ES-DE] ROMDirectory e MediaDirectory já apontam para as raízes compartilhadas "
            f"({target_root}; {target_media_root}); "
            f"encontrei {len(system_dirs)} diretórios de plataforma existentes "
            f"({non_empty_dirs} não vazios). Nenhuma pasta, ROM ou mídia foi criada."
        )
        return 0

    state = {name: ("ausente" if value is None else (value or "vazio; padrão do ES-DE"))
             for name, value in previous_values.items()}
    if check_only:
        print(
            f"[ES-DE] Precisa configurar ROMDirectory/MediaDirectory: {state} → "
            f"{{'ROMDirectory': '{target_root}', 'MediaDirectory': '{target_media_root}'}}. "
            f"A árvore já tem {len(system_dirs)} diretórios de plataforma "
            f"({non_empty_dirs} não vazios); nenhuma pasta será criada."
        )
        return 0

    active_pid = running_appimage_pid()
    if active_pid is not None:
        raise RuntimeError(
            f"O ES-DE já está aberto (PID {active_pid}); feche-o antes de atualizar es_settings.xml."
        )
    if not ROMTOOL.is_file():
        raise RuntimeError(f"Não encontrei romtool.py para registrar a mudança: {ROMTOOL}")

    backup = preserve_backup(config, original, mode) if original is not None else None
    write_atomic(config, updated, mode)
    try:
        operation_id = record_change(
            config,
            original,
            updated,
            backup,
            previous_values,
            {SETTING_NAME: target_root, MEDIA_SETTING_NAME: target_media_root},
            system_dirs,
            non_empty_dirs,
        )
    except Exception:
        current = config.read_bytes() if config.exists() else None
        if current is not None and sha256_bytes(current) == sha256_bytes(updated):
            if original is None:
                config.unlink()
            else:
                write_atomic(config, original, mode)
        raise

    backup_note = f"; backup {backup}" if backup is not None else "; configuração inicial criada"
    print(
        f"[ES-DE] ROMDirectory/MediaDirectory configurados para as raízes oficiais "
        f"{target_root} e {target_media_root}{backup_note}. "
        f"Diretórios existentes: {len(system_dirs)} ({non_empty_dirs} não vazios); "
        f"nenhuma ROM/mídia foi movida nem pasta de sistema criada. Registro: {operation_id}."
    )
    return 0


def _system_id_from_rom_path(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _single_segment(value: str) -> str | None:
    text = value.strip("/")
    if not text or "/" in text or text in {".", ".."} or text.startswith("."):
        return None
    return text


def media_category_from_output(raw: str, system_id: str, root: Path) -> str:
    """Classify one media output by its resolved path, not by a literal prefix alone.

    `%ROMROOTFOLDER%` is the system's ROM directory (`roms/<id>`), so `../media`
    lands in `roms/media`. The portable form is `../../media/<id>/<category>`.
    A leaked staging path or the stray tree is recognizable and rewritten; any
    other destination is refused.
    """
    normalized = (raw or "").strip().replace("\\", "/")
    if not normalized or normalized.count("%ROMROOTFOLDER%") > 1:
        raise RuntimeError(f"Saída de mídia Skraper ambígua: {system_id}")
    if len(normalized) > 1 and normalized[1] == ":" and not normalized.startswith("Z:"):
        raise RuntimeError(f"Saída de mídia Skraper em unidade não gerenciada: {system_id}")
    body = normalized[2:] if normalized.startswith("Z:") else normalized
    for prefix in (f"%ROMROOTFOLDER%/../../media/{system_id}/", f"%ROMROOTFOLDER%/../media/{system_id}/"):
        if body.startswith(prefix):
            category = _single_segment(body[len(prefix):])
            if category:
                return category
            raise RuntimeError(f"Saída de mídia Skraper fora da regra canônica: {system_id}")
    if "%ROMROOTFOLDER%" in body or not body.startswith("/"):
        raise RuntimeError(f"Saída de mídia Skraper fora da regra canônica: {system_id}")
    path = Path(body)
    parts = path.as_posix().rstrip("/").split("/")
    if len(parts) >= 4 and parts[-4] == "staging" and parts[-3] == "media" and parts[-2] == system_id:
        category = _single_segment(parts[-1])
        if category and "media-session-" in path.as_posix():
            return category
    try:
        resolved = path.resolve()
        project = root.resolve()
        resolved.relative_to(project)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Saída de mídia Skraper fora da raiz do projeto: {system_id}") from exc
    for base in (root / "media" / system_id, root / "roms" / "media" / system_id):
        try:
            relative = resolved.relative_to(base.resolve())
        except (OSError, ValueError):
            continue
        if len(relative.parts) == 1:
            category = _single_segment(relative.parts[0])
            if category:
                return category
    raise RuntimeError(f"Saída de mídia Skraper fora da regra canônica: {system_id}")


def portable_media_literal(system_id: str, category: str, separator: str) -> str:
    return separator.join(["%ROMROOTFOLDER%", "..", "..", "media", system_id, category])


def portable_gamelist_literal(system_id: str, separator: str) -> str:
    return separator.join(["%ROMROOTFOLDER%", "..", "..", "frontend", "ES-DE", "UserData", "gamelists", system_id, "gamelist.xml"])


def stray_media_fingerprint(root: Path) -> dict:
    """Cheap presence fingerprint. It never hashes payloads and never migrates them."""
    stray = root / "roms" / "media"
    if stray.is_symlink() or not stray.is_dir():
        return {"present": False, "files": 0, "bytes": 0, "max_mtime_ns": 0}
    files = 0
    size = 0
    max_mtime = 0
    for current, dirs, names in os.walk(stray, followlinks=False):
        parent = Path(current)
        dirs[:] = [name for name in dirs if not (parent / name).is_symlink()]
        for name in names:
            path = parent / name
            if path.is_symlink() or not path.is_file():
                continue
            files += 1
            info = path.stat()
            size += info.st_size
            if info.st_mtime_ns > max_mtime:
                max_mtime = info.st_mtime_ns
    return {"present": True, "files": files, "bytes": size, "max_mtime_ns": max_mtime}


def announce_stray(root: Path) -> dict:
    fingerprint = stray_media_fingerprint(root)
    if fingerprint["present"]:
        print(
            f"[Skraper] Diagnóstico: roms/media contém {fingerprint['files']} arquivos "
            f"({fingerprint['bytes']} bytes). A presença não migra nem bloqueia a abertura; "
            "um fluxo gerenciado não pode ampliar essa árvore."
        )
    return fingerprint


def skraper_settings(data: dict, runtime: str, root: Path) -> tuple[dict, dict]:
    """Rebase existing configured systems; never discover/create a second ROM tree.

    Steady state keeps every GameList export disabled. A managed session may
    point Type 1 at its own staging gamelists after this function returns.
    """
    canonical_data = appdata_root(root)
    catalog = json.loads((root / "frontend/RetroFE/Config/platform-catalog.json").read_text())
    system_ids = {row["id"] for row in catalog["systems"]}
    separator = "\\" if runtime == "wine" else "/"
    changed_roots = 0
    protected = []
    media_rewritten = 0
    for row in data["Systems"]:
        previous = row["GamesAndRoms"]["Path"]
        sid = _system_id_from_rom_path(previous)
        rom = root / "roms" / sid
        if sid not in system_ids or rom.is_symlink() or not rom.is_dir():
            raise RuntimeError(f"Sistema Skraper sem diretório canônico comprovado: {sid}")
        new = "Z:" + str(rom).replace("/", "\\") if runtime == "wine" else str(rom)
        changed_roots += new != previous
        row["GamesAndRoms"]["Path"] = new
        for media in row.get("Medias", []):
            category = media_category_from_output(media.get("OutputFolder", ""), sid, root)
            literal = portable_media_literal(sid, category, separator)
            media_rewritten += media.get("OutputFolder") != literal
            media["OutputFolder"] = literal
        game_list = row["GameList"]
        if game_list.get("Type") not in {0, 1}:
            raise RuntimeError(f"Tipo de gamelist Skraper não revisado: {sid}")
        game_list["OutputFile"] = portable_gamelist_literal(sid, separator)
        game_list["Type"] = 0
        if (canonical_data / "gamelists" / sid / "gamelist.xml").exists():
            protected.append(sid)
    data["AllSystems"]["GameList"]["Type"] = 0
    data["AllSystems"]["GameList"]["OutputFile"] = ""
    # Existing systems have explicit outputs. No legacy global output under ROMs
    # may silently become active when a user adds/inherits a new system.
    cleared_global = len(data["AllSystems"].get("Medias") or [])
    data["AllSystems"]["Medias"] = []
    global_root = str(root / "roms")
    if runtime == "wine":
        global_root = "Z:" + global_root.replace("/", "\\")
    global_settings = data["AllSystems"].setdefault("GamesAndRoms", {})
    global_root_changed = global_settings.get("Path") != global_root
    global_settings["Path"] = global_root

    def normalize(value):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str) and value.startswith("%ROMROOTFOLDER%"):
            return separator.join(part for part in re.split(r"[/\\]+", value) if part)
        return value

    return normalize(data), {"systems": len(data["Systems"]), "rom_roots_updated": changed_roots,
                             "global_rom_root_updated": global_root_changed,
                             "media_outputs_rewritten": media_rewritten,
                             "cleared_global_medias": cleared_global,
                             "protected_existing_gamelists": protected, "runtime": runtime}


def prepare_skraper(runtime: str) -> int:
    settings_file()  # same appdata/legacy-root safety contract as ES-DE
    project_rom_root()
    project_media_root()
    stray = announce_stray(PROJECT_ROOT)
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(Path(os.fsdecode(arg)).name.casefold() == "skraperui.exe" for arg in argv if arg):
            raise RuntimeError("Feche o Skraper antes de atualizar sua configuração de caminhos.")
    config = PROJECT_ROOT / "tools/roms/skraper/skraper.config.mono.json"
    if config.is_symlink() or not config.is_file():
        raise RuntimeError("Configuração Skraper ausente ou symlink; preserve para revisão.")
    original = config.read_bytes()
    data, summary = skraper_settings(json.loads(original), runtime, PROJECT_ROOT)
    updated = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    if original == updated:
        print(f"[Skraper] Caminhos portáveis conferidos; {len(summary['protected_existing_gamelists'])} gamelists existentes protegidas.")
        return 0
    mode = stat.S_IMODE(config.stat().st_mode)
    backup = preserve_backup(config, original, mode)
    write_atomic(config, updated, mode)
    details = {**summary, "configuration": str(config.relative_to(PROJECT_ROOT)),
               "sha256_before": sha256_bytes(original), "sha256_after": sha256_bytes(updated),
               "backup": str(backup.relative_to(PROJECT_ROOT)), "existing_gamelists_modified": False,
               "stray_media_files": stray["files"], "stray_media_bytes": stray["bytes"]}
    result = subprocess.run([sys.executable, str(ROMTOOL), "record-change", "--change-type", "skraper-canonical-path-preflight",
                             "--reason", "Rebase ROM/media/gamelist paths for the current portable root and protect existing ES-DE metadata before opening Skraper.",
                             "--details-json", json.dumps(details)], cwd=PROJECT_ROOT, text=True, capture_output=True)
    if result.returncode:
        if config.read_bytes() == updated:
            write_atomic(config, original, mode)
        raise RuntimeError("Falha ao registrar configuração Skraper; estado anterior restaurado.")
    operation_id = json.loads(result.stdout)["operation_id"]
    print(f"[Skraper] {summary['systems']} sistemas preparados; {len(summary['protected_existing_gamelists'])} gamelists preservadas; registro {operation_id}.")
    return 0


def preflight(runtime: str = "mono") -> int:
    """Idempotent resolved-path check. Cache misses rewrite known-bad literals only."""
    config = PROJECT_ROOT / "tools/roms/skraper/skraper.config.mono.json"
    if config.is_symlink() or not config.is_file():
        raise RuntimeError("Configuração Skraper ausente ou symlink; preserve para revisão.")
    fingerprint = stray_media_fingerprint(PROJECT_ROOT)
    original = config.read_bytes()
    digest = sha256_bytes(original)
    cache_path = PROJECT_ROOT / "outputs/roms-consolidacao-2026-10-24/operations/output-root-preflight.json"
    cache = {}
    if cache_path.is_file() and not cache_path.is_symlink():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    if cache.get("config_sha256") == digest and cache.get("stray") == fingerprint and cache.get("status") == "canonical":
        print(
            f"[Skraper] preflight em cache: sistemas={cache.get('systems')} "
            f"roms/media_arquivos={fingerprint['files']} bytes={fingerprint['bytes']}"
        )
        return 0
    data, summary = skraper_settings(json.loads(original), runtime, PROJECT_ROOT)
    updated = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    for row in data["Systems"]:
        for media in row.get("Medias", []):
            folder = str(media.get("OutputFolder", "")).replace("\\", "/")
            if "/../media/" in folder.replace("/../../media/", "/CANONICAL/"):
                raise RuntimeError(f"Preflight ainda resolveria mídia fora de media/: {row['GamesAndRoms']['Path'].replace(chr(92), '/').rstrip('/').rsplit('/', 1)[-1]}")
            if "staging" in folder or folder.startswith("Z:") or folder.startswith("/"):
                raise RuntimeError("Preflight deixaria uma saída absoluta ou de staging na configuração estável")
        game_list = row["GameList"]
        output = str(game_list.get("OutputFile", "")).replace("\\", "/")
        if game_list.get("Type") != 0 or "staging" in output or "/../frontend/" in output.replace("/../../frontend/", "/CANONICAL/"):
            raise RuntimeError("Preflight deixaria exportação de gamelist fora do literal portátil desativado")
    if updated != original:
        code = prepare_skraper(runtime)
        if code != 0:
            return code
        updated = config.read_bytes()
        digest = sha256_bytes(updated)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "canonical", "runtime": runtime, "config_sha256": digest,
        "systems": summary["systems"], "protected_gamelists": len(summary["protected_existing_gamelists"]),
        "media_outputs_rewritten": summary["media_outputs_rewritten"],
        "stray": fingerprint, "credentials_recorded": False,
    }
    temporary = cache_path.with_name(f".{cache_path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, cache_path)
    print(
        f"[Skraper] preflight: sistemas={summary['systems']} "
        f"saidas_reescritas={summary['media_outputs_rewritten']} "
        f"gamelists_protegidas={len(summary['protected_existing_gamelists'])} "
        f"roms/media_arquivos={fingerprint['files']} bytes={fingerprint['bytes']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="inspeciona sem escrever")
    mode.add_argument("--apply", action="store_true", help="configura ROMDirectory e MediaDirectory canônicos")
    mode.add_argument("--prepare-skraper", choices=["mono", "wine"], help="rebaseia caminhos Skraper e protege gamelists existentes antes de abrir")
    mode.add_argument("--preflight", nargs="?", const="mono", choices=["mono", "wine"],
                      help="confere saídas resolvidas e reescreve literais conhecidos; padrão mono")
    args = parser.parse_args()
    try:
        if args.preflight:
            return preflight(args.preflight)
        if args.prepare_skraper:
            return prepare_skraper(args.prepare_skraper)
        return apply_configuration(check_only=args.check)
    except Exception as exc:
        print(f"[ES-DE] ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
