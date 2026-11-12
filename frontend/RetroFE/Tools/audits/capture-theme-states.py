#!/usr/bin/env python3
"""Capture named states of any RetroFE theme in an isolated virtual display.

Generic companion of validate-aeon-nox-navigation.py --composition: the project is
mounted read-only, settings/log/UserData/playlists are temporary, the game launcher
is replaced by a guard that refuses gameplay, and audio uses the dummy driver.
Steps are a comma-separated script, for example:

    cap:a1,key:Return,wait:1.5,cap:a2,key:Right,cap:a2-next,key:Escape,cap:return-a1

Each capture waits for a non-blank frame. Nothing outside Docs/TestResults is written.
This proves native rendering and keyboard routing only: no physical input, audible
sound, host monitor placement, FPS or gameplay.
"""
from pathlib import Path
from contextlib import ExitStack
import importlib.util
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
STEP = re.compile(r"(cap|key|wait):([A-Za-z0-9_.+-]+)")


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def parse_steps(text):
    steps = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        match = STEP.fullmatch(part)
        if not match:
            raise ValueError(f"Passo inválido: {part!r}")
        kind, value = match.groups()
        if kind == "wait" and not 0 < float(value) <= 30:
            raise ValueError("wait aceita de 0 a 30 segundos")
        steps.append((kind, value))
    if not any(kind == "cap" for kind, _ in steps):
        raise ValueError("O roteiro precisa de ao menos uma captura")
    return steps


def inner(root, scratch, layout, width, height, stamp, steps, prefix):
    from PIL import Image, ImageStat
    out = root / "Docs/TestResults"
    env = {**os.environ, "SDL_AUDIODRIVER": "dummy", "SDL_RENDER_DRIVER": "software", "SDL_VIDEODRIVER": "x11",
           "LIBGL_ALWAYS_SOFTWARE": "1", "LD_LIBRARY_PATH": str(root / "core/linux/lib")}
    log = (scratch / "retrofe-stdout.log").open("w")
    app = subprocess.Popen([str(root / "RetroFE")], env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    captures, sequence = {}, []

    def run(command):
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError(f"{command}: {result.stderr.strip()}")
        return result.stdout.strip()

    try:
        deadline = time.monotonic() + 40
        while "[Layout] Initialized" not in (root / "log.txt").read_text(errors="replace"):
            if app.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("RetroFE não inicializou o layout")
            time.sleep(.2)
        time.sleep(1)
        wid = run(["xdotool", "search", "--pid", str(app.pid), "--onlyvisible"]).splitlines()[0]
        run(["xdotool", "windowmove", wid, "0", "0"])
        run(["xdotool", "windowsize", wid, str(width), str(height)])
        run(["xdotool", "windowfocus", "--sync", wid])
        time.sleep(2.5)
        for kind, value in steps:
            if app.poll() is not None:
                raise RuntimeError(f"RetroFE encerrou antes de {kind}:{value}")
            if kind == "wait":
                time.sleep(float(value))
            elif kind == "key":
                run(["xdotool", "windowfocus", "--sync", wid])
                run(["xdotool", "key", "--clearmodifiers", value])
                time.sleep(.6)
            else:
                name = f"theme-review-{prefix}-{value}-{width}x{height}-{stamp}.png"
                destination = out / name
                deadline = time.monotonic() + 12
                while True:
                    run(["import", "-display", env["DISPLAY"], "-window", wid, str(destination)])
                    with Image.open(destination) as frame:
                        size = frame.size
                        if max(ImageStat.Stat(frame.convert("RGB")).stddev) > 3:
                            break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"Quadro vazio em {value}")
                    time.sleep(.3)
                captures[value] = {"path": f"Docs/TestResults/{name}", "pixels": list(size)}
            sequence.append(f"{kind}:{value}")
        engine_log = (root / "log.txt").read_text(errors="replace")
        print(json.dumps({"status": "passed", "layout": layout, "requested_size": [width, height], "sequence": sequence,
                          "captures": captures,
                          "engine_errors": [l.strip()[-240:] for l in engine_log.splitlines() if "[ERROR]" in l][:20],
                          "launcher_activity": "[Launcher]" in engine_log}, ensure_ascii=False), flush=True)
    finally:
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=4)
            except subprocess.TimeoutExpired:
                app.kill()
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--layout", required=True, help="Diretório em layouts/")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--steps", required=True)
    parser.add_argument("--stamp", default=time.strftime("%Y%m%d"), help="AAAAMMDD[-revisao]")
    parser.add_argument("--first-collection", default="Main")
    parser.add_argument("--inner", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = ROOT
    steps = parse_steps(args.steps)
    if not (root / "layouts" / args.layout / "layout.xml").is_file() or "/" in args.layout:
        parser.error("--layout precisa ser um tema existente em layouts/")
    if not re.fullmatch(r"\d{8}(?:-[a-z0-9]+)*", args.stamp):
        parser.error("--stamp deve usar AAAAMMDD[-revisao]")
    if not (320 <= args.width <= 7680 and 240 <= args.height <= 4320):
        parser.error("Resolução fora do intervalo suportado")
    prefix = slug(args.layout)
    if args.inner:
        inner(root, args.inner, args.layout, args.width, args.height, args.stamp, steps, prefix)
        return 0
    for command in ("bwrap", "xvfb-run", "Xvfb", "xdotool", "import"):
        if not shutil.which(command):
            raise RuntimeError(f"Dependência ausente: {command}")
    scratch = Path(tempfile.mkdtemp(prefix="retrofe-theme-capture-"))
    out = root / "Docs/TestResults"
    leases = ExitStack()
    process = None
    try:
        project = root.parents[1]
        sys.path.insert(0, str(project / 'tools/roms'))
        import romtool, media_session
        spec = importlib.util.spec_from_file_location('media_paths', project / 'tools/roms/materialize-collection-media-paths.py')
        paths = importlib.util.module_from_spec(spec); spec.loader.exec_module(paths)
        leases.enter_context(paths.frontend_instance_lock())
        leases.enter_context(media_session.write_lock(vars(romtool)))
        if any(romtool.frontend_processes_running().values()):
            raise RuntimeError('Feche os frontends normalmente antes de capturar o tema')
        (scratch / "UserData/retrofe").mkdir(parents=True, mode=0o700)
        settings = (root / "settings.conf").read_text()
        for key, value in {"layout": args.layout, "horizontal": args.width, "vertical": args.height,
                           "fullscreen": "no", "fullscreen0": "no", "fullscreen1": "no",
                           "horizontal0": args.width, "vertical0": args.height, "screenNum0": 0,
                           "firstCollection": args.first_collection, "firstPlaylist": "all", "attractModeTime": 0}.items():
            settings = re.sub(r"(?m)^(" + re.escape(key) + r"\s*=\s*)[^\r\n#]*", lambda m: m[1] + str(value), settings)
        (scratch / "settings.conf").write_text(settings)
        (scratch / "log.txt").touch()
        # A routed Enter in A2 must never become gameplay during a visual audit.
        guard = scratch / "blocked-game-launch.py"
        guard.write_text("#!/usr/bin/env python3\nraise SystemExit('Gameplay is disabled in the visual audit')\n")
        guard.chmod(0o555)
        shutil.copytree(root / "collections/Main/playlists", scratch / "playlists")
        command = ["bwrap", "--unshare-pid", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                   "--bind", str(scratch), str(scratch), "--bind", str(scratch / "UserData"), str(root / "UserData"),
                   "--bind", str(scratch / "settings.conf"), str(root / "settings.conf"),
                   "--bind", str(scratch / "log.txt"), str(root / "log.txt"),
                   "--ro-bind", str(guard), str(root / "Tools/launchers/emulators/host-emulator-launcher.py"),
                   "--bind", str(scratch / "playlists"), str(root / "collections/Main/playlists"),
                   "--bind", str(out), str(out),
                   sys.executable, str(Path(__file__).resolve()), "--layout", args.layout, "--width", str(args.width),
                   "--height", str(args.height), "--steps", args.steps, "--stamp", args.stamp,
                   "--first-collection", args.first_collection, "--inner", str(scratch)]
        command = ["xvfb-run", "--auto-servernum", "--server-args",
                   f"-screen 0 {args.width + 100}x{args.height + 100}x24 -nolisten tcp", *command]
        env = {**os.environ, "XDG_RUNTIME_DIR": str(scratch), "XDG_CACHE_HOME": str(scratch / "cache")}
        env.pop("WAYLAND_DISPLAY", None)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                   start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=8)
        print(stdout.strip())
        if process.returncode:
            print(stderr[-2000:], file=sys.stderr)
            engine = scratch / "log.txt"
            if engine.exists():
                print(engine.read_text(errors="replace")[-2000:], file=sys.stderr)
        return process.returncode
    finally:
        if process is not None:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=5)
        leases.close()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
