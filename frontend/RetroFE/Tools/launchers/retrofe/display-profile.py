#!/usr/bin/env python3
"""Choose and read portable RetroFE display preferences for the next launch."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[3]
PREFERENCE_REL = Path("UserData/retrofe/display-profile.json")
PROFILES = {
    "4:3 Classic 1440x1080": {
        "id": "4x3-classic", "width": 1440, "height": 1080,
    },
    "16:9 HD 1920x1080": {
        "id": "16x9-hd", "width": 1920, "height": 1080,
    },
    "16:10 Steam Deck 1280x800": {
        "id": "16x10-steam-deck", "width": 1280, "height": 800,
    },
    "21:9 Ultra-wide 2560x1080": {
        "id": "64x27-ultra-wide", "width": 2560, "height": 1080,
    },
    "21:9 Ultra-wide 3440x1440": {
        "id": "21x9-ultra-wide", "width": 3440, "height": 1440,
    },
    "32:9 Super-wide 5120x1440": {
        "id": "32x9-super-wide", "width": 5120, "height": 1440,
    },
}


def preference_path(root: Path) -> Path:
    return root / PREFERENCE_REL


def read_preference(root: Path) -> dict[str, object] | None:
    path = preference_path(root)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    profile_id = str(value.get("profile_id", ""))
    profile = next((item for item in PROFILES.values() if item["id"] == profile_id), None)
    if profile is None:
        raise ValueError(f"Perfil salvo desconhecido: {profile_id!r}")
    return {**profile, **value}


def find_profile(selection: str) -> tuple[str, dict[str, object]]:
    folded = selection.strip().casefold()
    for label, profile in PROFILES.items():
        if folded in {label.casefold(), str(profile["id"]).casefold()}:
            return label, profile
    raise ValueError(f"Perfil desconhecido: {selection!r}. Use 'list' para ver as opções.")


def atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"A preferência é um symlink e foi preservada: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def notify(message: str) -> None:
    if os.environ.get("RETROFE_SILENT_OPTIONS") == "1":
        return
    notify_send = shutil.which("notify-send")
    if notify_send:
        subprocess.run([notify_send, "RetroFE", message], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="raiz RetroFE; padrão relativo à ferramenta")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="lista perfis de tela disponíveis")
    subparsers.add_parser("env", help="emite variáveis para o launcher")
    subparsers.add_parser("show", help="mostra a preferência atual")
    set_parser = subparsers.add_parser("set", help="salva o perfil para a próxima abertura")
    set_parser.add_argument("profile", help="nome ou id exibido por list")
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        if args.command == "list":
            for label, profile in PROFILES.items():
                print(f"{label}\t{profile['id']}\t{profile['width']}x{profile['height']}")
            return 0

        if args.command == "env":
            saved = read_preference(root)
            if saved:
                print(f"RETROFE_DISPLAY_PROFILE_ID={saved['id']}")
                print(f"RETROFE_WINDOW_WIDTH={saved['width']}")
                print(f"RETROFE_WINDOW_HEIGHT={saved['height']}")
            return 0

        if args.command == "show":
            saved = read_preference(root)
            if saved:
                print(json.dumps(saved, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print("Nenhum perfil salvo; o launcher usa as configurações atuais do host.")
            return 0

        label, profile = find_profile(args.profile)
        theme_tool = root / "Tools/launchers/retrofe/theme-profile.py"
        if theme_tool.is_file():
            checked = subprocess.run(
                [sys.executable, str(theme_tool), "--root", str(root), "--display-profile-id", str(profile["id"]), "env"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if checked.returncode != 0:
                raise ValueError(checked.stderr.strip() or "O tema ativo não suporta esse perfil de tela.")
        record = {
            "schema_version": 2,
            "profile_id": profile["id"],
            "profile_label": label,
            "width": profile["width"],
            "height": profile["height"],
            "selected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        data = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        atomic_write(preference_path(root), data)
        message = f"{label} será aplicado na próxima abertura do RetroFE. Feche a sessão normalmente para alternar."
        print(message)
        notify(message)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
