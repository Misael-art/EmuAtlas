"""Load host-emulator-launcher.py from a minimal fixture RetroFE root (no personal profile needed)."""
import atexit
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EMULATORS = ROOT / "frontend/RetroFE/Tools/launchers/emulators"


def load_launcher(name):
    base = Path(tempfile.mkdtemp(prefix="launcher-fixture-"))
    atexit.register(shutil.rmtree, base, True)
    retrofe = base / "frontend/RetroFE"
    target = retrofe / "Tools/launchers/emulators"
    target.mkdir(parents=True)
    (retrofe / "collections").mkdir()
    (retrofe / "settings.conf").write_text("")
    (retrofe / "Config/profiles").mkdir(parents=True)
    (retrofe / "Config/profiles/host-linux.json").write_text(json.dumps({"collections": {}}))
    for source in EMULATORS.glob("*.py"):
        shutil.copy2(source, target / source.name)
    tools = base / 'tools/roms'
    tools.mkdir(parents=True)
    shutil.copy2(ROOT / 'tools/roms/portable_emulator.py', tools / 'portable_emulator.py')
    sys.path.insert(0, str(target))
    spec = importlib.util.spec_from_file_location(name, target / "host-emulator-launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_emutool():
    """emutool needs a project root with catalog/profile; build a minimal one with copied tool sources."""
    base = Path(tempfile.mkdtemp(prefix="emutool-fixture-"))
    atexit.register(shutil.rmtree, base, True)
    config = base / "frontend/RetroFE/Config"
    (config / "profiles").mkdir(parents=True)
    (config / "platform-catalog.json").write_text(json.dumps({"systems": []}))
    (config / "profiles/host-linux.json").write_text(json.dumps({"collections": {}}))
    shutil.copytree(ROOT / "tools/roms", base / "tools/roms",
                    ignore=shutil.ignore_patterns("tests", "runtimes", "skraper", "docs", "__pycache__", "*.log"))
    sys.path.insert(0, str(base / "tools/roms"))
    spec = importlib.util.spec_from_file_location("emutool_fixture", base / "tools/roms/emutool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
