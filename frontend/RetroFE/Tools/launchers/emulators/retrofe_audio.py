"""Audio helpers for host emulators launched from the RetroFE frontend."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence


def set_background_music_paused(paused: bool) -> None:
    """Ask the optional mpv background stream to pause or resume."""
    socket_path = os.environ.get("RETROFE_MUSIC_SOCKET")
    if not socket_path:
        return
    request = {
        "command": ["set_property", "pause", paused],
        "request_id": 1,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.3)
            client.connect(socket_path)
            client.sendall((json.dumps(request) + "\n").encode("utf-8"))
            # Consume mpv's reply before closing the IPC connection.
            response = bytearray()
            while b"\n" not in response:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
    except OSError:
        # Music is optional; emulator launch must work without a player/socket.
        return


def run_with_music_paused(command: Sequence[str], cwd: Path, env: Mapping[str, str] | None = None,
                          system_id: str | None = None, journey: dict | None = None) -> int:
    """Run an emulator while muting the frontend's separate music stream."""
    set_background_music_paused(True)
    try:
        if journey and os.name == 'nt':
            sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "tools/roms"))
            from save_journey import run_unrouted
            return run_unrouted(Path(__file__).resolve().parents[5], command,
                                dict(env) if env is not None else dict(os.environ), journey)
        if system_id and os.name != 'nt':
            root = Path(__file__).resolve().parents[3]
            if env is not None:
                # Keep Steam/KDE helpers in the host XDG environment. Emulator
                # overrides apply only after the session has been prepared.
                differences = [f"{key}={value}" for key, value in env.items() if os.environ.get(key) != value]
                removed = [argument for key in os.environ if key not in env for argument in ("-u", key)]
                command = [shutil.which("env") or "env", *removed, *differences, *command]
            command = [sys.executable, str(root / "Tools/launchers/retrofe/steam-input.py"),
                       "--root", str(root), "session-run", "--system-id", system_id, "--", *command]
            env = {**os.environ, "EMUATLAS_RETROARCH_JOURNEY": json.dumps(journey)} if journey else None
        return subprocess.run(command, cwd=cwd, env=dict(env) if env is not None else None, check=False).returncode
    finally:
        set_background_music_paused(False)
