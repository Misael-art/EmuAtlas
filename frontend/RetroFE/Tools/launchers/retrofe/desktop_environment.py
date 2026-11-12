"""Desktop input environment captured before a game and restored after it.

The Steam profile swap and the KDE shortcut guard cover what RetroFE itself
changes. A game session can still leave the desktop half-working through the
host pieces around them: the KWin touch keyboard switched off, or the
touch/terminal keyboard watcher stopped. This module records how the desktop
looked when the first session started and puts back whatever went missing,
so the Deck always returns with trackpad, keyboard and button shortcuts.

Only restores, never removes: a piece that was off before the game stays off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

SNAPSHOT_NAME = "desktop-environment.json"
# User units whose loss leaves the desktop without an input path. Missing
# units are simply not recorded.
WATCHED_UNITS = (
    "phasezero-terminal-keyboard.service",
    "phasezero-controller-apps.service",
)
NOTIFY_INTERVAL = 300.0


def _run(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "")


def _unit_active(unit: str) -> bool | None:
    if not shutil.which("systemctl"):
        return None
    if _run(["systemctl", "--user", "cat", unit]).returncode != 0:
        return None
    return _run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def _kwin_keyboard_enabled() -> str | None:
    if not shutil.which("kreadconfig6"):
        return None
    value = _run(["kreadconfig6", "--file", "kwinrc", "--group", "Wayland",
                  "--key", "VirtualKeyboardEnabled"]).stdout.strip()
    return value or None


def phasezero_reconcile(action: str = "reconcile") -> list[str]:
    """Use the registered PhaseZero backend; never maintain a second audio policy.

    Registration is opt-in through `pz steamdeck input install`. A standalone
    RetroFE retains its existing snapshot recovery when PhaseZero is absent.
    """
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "phasezero/desktop-session.json"
    if config.is_symlink():
        raise RuntimeError("Registro PhaseZero symlink; preservado.")
    if not config.exists():
        return []
    value = json.loads(config.read_text())
    command = value.get("command")
    if (value.get("schema_version") != 1 or value.get("managed_by") != "phasezero-input"
            or not isinstance(command, list) or len(command) != 2
            or not all(isinstance(v, str) and Path(v).is_absolute() for v in command)):
        raise RuntimeError("Registro de recuperação PhaseZero inválido; preservado.")
    result = _run([*command, action], timeout=30)
    if result.returncode:
        raise RuntimeError("Recuperação PhaseZero pendente: " + (result.stderr or result.stdout)[-300:])
    return ["phasezero-desktop-policy"]


def capture() -> dict:
    # Capture immediately before the first profile swap, rather than depending
    # on the background guardian's last polling interval.
    phasezero_reconcile("capture")
    return {
        "schema_version": 1,
        "captured_at": time.time(),
        "kwin_virtual_keyboard": _kwin_keyboard_enabled(),
        "units": {unit: state for unit in WATCHED_UNITS
                  if (state := _unit_active(unit)) is not None},
    }


def save_once(state_dir: Path) -> None:
    """Record the desktop before the first concurrent session; keep the
    oldest snapshot so a second game never captures a degraded desktop."""
    path = state_dir / SNAPSHOT_NAME
    if path.is_symlink():
        raise RuntimeError("Snapshot do desktop é symlink; preservado.")
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("schema_version") != 1:
            raise RuntimeError("Snapshot do desktop desconhecido; preservado.")
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(capture(), indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def restore(state_dir: Path) -> list[str]:
    """Bring back what the desktop had. Returns what was repaired; raises
    RuntimeError (snapshot kept) when something could not be repaired."""
    path = state_dir / SNAPSHOT_NAME
    if path.is_symlink():
        raise RuntimeError("Snapshot do desktop é symlink; preservado.")
    if not path.exists():
        return []
    saved = json.loads(path.read_text(encoding="utf-8"))
    repaired, failed = [], []
    if saved.get("kwin_virtual_keyboard") == "true" and _kwin_keyboard_enabled() != "true":
        ok = _run(["kwriteconfig6", "--notify", "--file", "kwinrc", "--group", "Wayland",
                   "--key", "VirtualKeyboardEnabled", "true"]).returncode == 0
        if ok:
            _run(["qdbus6", "org.kde.KWin", "/KWin", "reconfigure"])
            repaired.append("kwin-virtual-keyboard")
        else:
            failed.append("kwin-virtual-keyboard")
    for unit, was_active in saved.get("units", {}).items():
        if was_active and _unit_active(unit) is False:
            if _run(["systemctl", "--user", "restart", unit], timeout=15).returncode == 0:
                repaired.append(unit)
            else:
                failed.append(unit)
    if failed:
        raise RuntimeError("Desktop parcialmente restaurado; pendente: " + ", ".join(failed))
    # Do not discard the original snapshot while shared restoration is pending.
    repaired.extend(phasezero_reconcile())
    path.unlink(missing_ok=True)
    return repaired


_last_notice = 0.0


def notify_stuck(message: str) -> None:
    """Tell the user, at most every few minutes, how to get the controls
    back by touch: the touchscreen works even without any controller map."""
    global _last_notice
    if time.monotonic() - _last_notice < NOTIFY_INTERVAL or not shutil.which("notify-send"):
        return
    _last_notice = time.monotonic()
    _run(["notify-send", "--app-name=RetroFE", "--urgency=critical", "--expire-time=30000",
          "Controles do Desktop ainda não voltaram",
          "Toque em: menu > Restaurar controles, ou abra o Steam. Detalhe: " + message[:160]])
