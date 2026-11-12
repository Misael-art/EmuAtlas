#!/usr/bin/env python3
"""Queue and resolve portable RetroFE theme choices."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


DEFAULT_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_REL = Path("Config/themes.json")
PREFERENCE_REL = Path("UserData/retrofe/theme-profile.json")
PENDING_REL = Path("UserData/retrofe/theme-switch-pending.json")
DISPLAY_REL = Path("UserData/retrofe/display-profile.json")
PROFILE_DIMENSIONS = {
    "4x3-classic": (1440, 1080),
    "16x9-hd": (1920, 1080),
    "16x10-steam-deck": (1280, 800),
    "64x27-ultra-wide": (2560, 1080),
    "21x9-ultra-wide": (3440, 1440),
    "32x9-super-wide": (5120, 1440),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"Arquivo de preferência é symlink e foi preservado: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, data)


def notify(message: str) -> None:
    command = shutil.which("notify-send")
    if command:
        subprocess.run([command, "RetroFE", message], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_registry(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = root / REGISTRY_REL
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("themes"), list):
        raise ValueError(f"Catálogo de temas inválido: {path}")
    themes: dict[str, dict[str, Any]] = {}
    for theme in data["themes"]:
        theme_id = str(theme.get("id", ""))
        label = str(theme.get("label", ""))
        layout = str(theme.get("layout", ""))
        if not theme_id or not label or not layout or theme_id in themes:
            raise ValueError(f"Entrada de tema incompleta ou duplicada em {path}")
        themes[theme_id] = theme
    default_id = str(data.get("default_theme_id", ""))
    if default_id not in themes:
        raise ValueError(f"Tema padrão ausente do catálogo: {default_id!r}")
    return data, themes


def safe_theme(root: Path, theme: dict[str, Any]) -> None:
    layout = str(theme["layout"])
    layout_dir = (root / "layouts" / layout).resolve()
    if not layout_dir.is_relative_to((root / "layouts").resolve()):
        raise ValueError(f"Caminho do tema sai de layouts/: {layout}")
    main_xml = layout_dir / "layout.xml"
    if not main_xml.is_file():
        raise ValueError(f"Layout principal ausente: {main_xml}")
    ET.parse(main_xml)
    variants = theme.get("variants", {})
    if not isinstance(variants, dict):
        raise ValueError(f"Variantes inválidas no tema {theme['id']}")
    for display_id, filename in variants.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError(f"Variante insegura no tema {theme['id']}: {filename!r}")
        variant = layout_dir / filename
        if not variant.is_file():
            raise ValueError(f"Variante {display_id} ausente: {variant}")
        ET.parse(variant)
    verifier = Path(__file__).with_name("navigation-panel.py")
    checked = subprocess.run([sys.executable, str(verifier), "--root", str(root),
                              "--check-contract", "--theme", theme["id"]],
                             capture_output=True, text=True, timeout=10)
    if checked.returncode:
        raise ValueError(f"O tema não preserva Opções/saída: {checked.stderr.strip() or checked.stdout.strip()}")


def display_profile_id(root: Path, explicit: str | None, width: int | None, height: int | None) -> str:
    if explicit:
        if explicit not in PROFILE_DIMENSIONS:
            raise ValueError(f"Perfil de tela desconhecido: {explicit}")
        return explicit
    saved_path = root / DISPLAY_REL
    if saved_path.is_file():
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        saved_id = str(saved.get("profile_id", ""))
        if saved_id in PROFILE_DIMENSIONS:
            return saved_id
    if width and height:
        for profile_id, dimensions in PROFILE_DIMENSIONS.items():
            if dimensions == (width, height):
                return profile_id
    return "16x9-hd"


def profile_for_layout(root: Path, themes: dict[str, dict[str, Any]]) -> str:
    preference = root / PREFERENCE_REL
    if preference.is_file():
        data = json.loads(preference.read_text(encoding="utf-8"))
        theme_id = str(data.get("theme_id", ""))
        if theme_id in themes:
            safe_theme(root, themes[theme_id])
            return theme_id
        raise ValueError(f"Tema salvo desconhecido: {theme_id!r}")
    settings = root / "settings.conf"
    if settings.is_file():
        content = settings.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?m)^\s*layout\s*=\s*([^#\r\n]+)", content)
        if match:
            name = match.group(1).strip()
            for theme_id, theme in themes.items():
                if theme["layout"] == name:
                    safe_theme(root, theme)
                    return theme_id
    default_id = load_registry(root)[0]["default_theme_id"]
    safe_theme(root, themes[default_id])
    return default_id


def resolve_theme(root: Path, selection: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    registry, themes = load_registry(root)
    folded = selection.strip().casefold()
    theme_id = next(
        (key for key, theme in themes.items() if folded in {key.casefold(), str(theme["label"]).casefold()}),
        None,
    )
    if theme_id is None:
        raise ValueError(f"Tema desconhecido: {selection!r}")
    theme = themes[theme_id]
    safe_theme(root, theme)
    return registry, theme, theme_id


def ensure_supported(theme: dict[str, Any], profile_id: str) -> None:
    supported = theme.get("supported_display_profiles", [])
    if profile_id not in supported:
        labels = ", ".join(supported) if supported else "nenhum perfil declarado"
        raise ValueError(f"{theme['label']} não está habilitado para {profile_id}; perfis do tema: {labels}")


def command_env(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    _, themes = load_registry(root)
    current_id = profile_for_layout(root, themes)
    theme = themes[current_id]
    profile_id = display_profile_id(root, args.display_profile_id, args.width, args.height)
    ensure_supported(theme, profile_id)
    variant = str(theme.get("variants", {}).get(profile_id, "layout.xml"))
    values = {
        "RETROFE_THEME_ID": current_id,
        "RETROFE_LAYOUT": str(theme["layout"]),
    }
    if variant != "layout.xml":
        values["RETROFE_LAYOUT_VARIANT"] = variant
    preference = root / PREFERENCE_REL
    if preference.is_file():
        saved = json.loads(preference.read_text(encoding="utf-8"))
        resume = str(saved.get("resume_collection", ""))
        if resume and (root / "collections" / resume).is_dir():
            values["RETROFE_FIRST_COLLECTION"] = resume
    for key, value in values.items():
        print(f"{key}={value}")
    return 0


def command_request(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    _, theme, theme_id = resolve_theme(root, args.theme)
    profile_id = display_profile_id(root, args.display_profile_id, args.width, args.height)
    ensure_supported(theme, profile_id)
    _, themes = load_registry(root)
    active_id = profile_for_layout(root, themes)
    if theme_id == active_id:
        print(f"{theme['label']} já está ativo.")
        return 0
    previous_theme = themes[active_id]
    record = {
        "schema_version": 1,
        "theme_id": theme_id,
        "theme_label": theme["label"],
        "previous_theme_id": active_id,
        "previous_theme_label": previous_theme["label"],
        "return_collection": "Main",
        "requested_at_utc": utc_now(),
        "apply_policy": "after-clean-frontend-exit",
        "transition_policy": "supervised-visual-transition-with-clean-q",
    }
    write_json(root / PENDING_REL, record)
    message = f"{theme['label']} selecionado; o launcher está preparando a transição visual e a reabertura em Main."
    print(message)
    return 0


def command_consume(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    pending_path = root / PENDING_REL
    if not pending_path.is_file():
        return 0
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        _, theme, theme_id = resolve_theme(root, str(pending.get("theme_id", "")))
        profile_id = display_profile_id(root, args.display_profile_id, args.width, args.height)
        ensure_supported(theme, profile_id)
        _, themes = load_registry(root)
        previous_theme_id = str(pending.get("previous_theme_id", ""))
        if previous_theme_id:
            if previous_theme_id not in themes:
                raise ValueError(f"Tema anterior para rollback não existe: {previous_theme_id!r}")
            safe_theme(root, themes[previous_theme_id])
            ensure_supported(themes[previous_theme_id], profile_id)
    except (OSError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        failed = root / "UserData/retrofe/theme-switch-failed.json"
        write_json(failed, {"schema_version": 1, "reason": str(exc), "failed_at_utc": utc_now(), "request": pending if "pending" in locals() else {}})
        pending_path.unlink(missing_ok=True)
        print(f"Solicitação de tema recusada; o tema atual foi mantido: {exc}", file=sys.stderr)
        return 2
    record = {
        "schema_version": 1,
        "theme_id": theme_id,
        "theme_label": theme["label"],
        "changed_at_utc": utc_now(),
        "resume_collection": str(pending.get("return_collection", "Main")),
    }
    if previous_theme_id:
        record["rollback_theme_id"] = previous_theme_id
        record["rollback_theme_label"] = str(pending.get("previous_theme_label", themes[previous_theme_id]["label"]))
    write_json(root / PREFERENCE_REL, record)
    pending_path.unlink(missing_ok=True)
    print(f"Tema ativo na próxima abertura: {theme['label']} ({theme['layout']}); retorno: {record['resume_collection']}.")
    return 0


def command_mark_started(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    path = root / PREFERENCE_REL
    if not path.is_file():
        return 0
    record = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    if "resume_collection" in record:
        record.pop("resume_collection", None)
        changed = True
    if "rollback_theme_id" in record:
        record.pop("rollback_theme_id", None)
        record.pop("rollback_theme_label", None)
        changed = True
    if not changed:
        return 0
    record["last_started_at_utc"] = utc_now()
    write_json(path, record)
    return 0


def command_rollback_failed(args: argparse.Namespace) -> int:
    """Restore the previous theme when a newly selected layout fails to start."""
    root = args.root.resolve()
    preference = root / PREFERENCE_REL
    if not preference.is_file():
        print("TEMA_ROLLBACK=none")
        return 0
    current = json.loads(preference.read_text(encoding="utf-8"))
    previous_id = str(current.get("rollback_theme_id", ""))
    if not previous_id:
        print("TEMA_ROLLBACK=none")
        return 0

    _, themes = load_registry(root)
    if previous_id not in themes:
        raise ValueError(f"Tema de rollback desconhecido: {previous_id!r}")
    previous = themes[previous_id]
    safe_theme(root, previous)
    profile_id = display_profile_id(root, args.display_profile_id, args.width, args.height)
    ensure_supported(previous, profile_id)

    failure = {
        "schema_version": 1,
        "failed_theme_id": str(current.get("theme_id", "")),
        "failed_theme_label": str(current.get("theme_label", "")),
        "rollback_theme_id": previous_id,
        "rollback_theme_label": str(previous["label"]),
        "failed_at_utc": utc_now(),
        "reason": "RetroFE encerrou com erro antes de confirmar a inicialização do novo tema.",
    }
    write_json(root / "UserData/retrofe/theme-switch-failed.json", failure)
    restored = {
        "schema_version": 1,
        "theme_id": previous_id,
        "theme_label": str(previous["label"]),
        "changed_at_utc": utc_now(),
        "resume_collection": "Main",
    }
    write_json(preference, restored)
    print(f"TEMA_ROLLBACK=restored:{previous_id}")
    notify(f"O tema não iniciou; voltei para {previous['label']}.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    registry, themes = load_registry(root)
    active_id = profile_for_layout(root, themes)
    profile_id = display_profile_id(root, args.display_profile_id, args.width, args.height)
    print(json.dumps({
        "active_theme": themes[active_id]["label"],
        "active_theme_id": active_id,
        "display_profile_id": profile_id,
        "compatible": profile_id in themes[active_id].get("supported_display_profiles", []),
        "pending": json.loads((root / PENDING_REL).read_text(encoding="utf-8")) if (root / PENDING_REL).is_file() else None,
        "selectable_themes": [
            {"id": key, "label": item["label"], "supported_display_profiles": item.get("supported_display_profiles", [])}
            for key, item in themes.items()
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--display-profile-id")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="lista temas e compatibilidade de tela")
    commands.add_parser("env", help="emite as configurações do tema ativo para o launcher")
    commands.add_parser("status", help="mostra tema, tela e solicitação pendente")
    request = commands.add_parser("request", help="agenda uma troca supervisionada após a saída limpa do RetroFE")
    request.add_argument("theme")
    commands.add_parser("consume-pending", help="aplica uma solicitação após a saída limpa")
    commands.add_parser("rollback-failed", help="restaura o tema anterior se o novo layout falhar ao iniciar")
    commands.add_parser("mark-started", help="encerra o retorno de navegação de uso único")
    args = parser.parse_args()
    try:
        if args.command == "list":
            root = args.root.resolve()
            _, themes = load_registry(root)
            for theme_id, theme in themes.items():
                safe_theme(root, theme)
                print(f"{theme['label']}\t{theme_id}\t{','.join(theme.get('supported_display_profiles', []))}")
            return 0
        if args.command == "env":
            return command_env(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "request":
            return command_request(args)
        if args.command == "consume-pending":
            return command_consume(args)
        if args.command == "rollback-failed":
            return command_rollback_failed(args)
        if args.command == "mark-started":
            return command_mark_started(args)
    except (OSError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.command == "request":
            notify(f"Não foi possível trocar o tema: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
