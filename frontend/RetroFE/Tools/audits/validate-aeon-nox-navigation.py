#!/usr/bin/env python3
"""Validate Options/return/jump HUD on host KDE with a protected test instance.

The project is mounted read-only; only a temporary settings/log/UserData view,
Main playlists and the existing TestResults output are writable. Input devices
are hidden; the host window manager is used to check the real focus/placement
contract. This runs a short visible keyboard sequence and does not launch games.
"""
from pathlib import Path
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


def inner(root, scratch, width, height, stamp, composition=False, virtual_display=False, ambient_cycle=False):
    captures = {}
    out = root / "Docs/TestResults"
    env = {**os.environ, "SDL_AUDIODRIVER": "dummy", "SDL_RENDER_DRIVER": "software",
           "QT_QPA_PLATFORM": "xcb", "SDL_VIDEODRIVER": "x11", "LIBGL_ALWAYS_SOFTWARE": "1", "LD_LIBRARY_PATH": str(root / "core/linux/lib"),
           "XDG_RUNTIME_DIR": str(scratch / "runtime"), "XDG_CACHE_HOME": str(scratch / "cache"),
           "XDG_CURRENT_DESKTOP": "", "QT_QPA_PLATFORMTHEME": "", "QT_NO_XDG_DESKTOP_PORTAL": "1"}
    processes, opened_logs = [], []
    def launch(command, name):
        log = (scratch / (name + ".log")).open("w")
        opened_logs.append(log)
        process = subprocess.Popen(command, env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    def run(command):
        result = subprocess.run(command, env=env, cwd=root, capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError(f"{command}: {result.stderr.strip()}")
        return result.stdout.strip()
    def key(keys, wait=.35):
        if composition:
            run(["xdotool", "windowfocus", "--sync", wid])
            run(["xdotool", "keydown", "--clearmodifiers", keys])
            time.sleep(.035 if keys in ('Right','Left','Up','Down') else .15)
            run(["xdotool", "keyup", keys])
        else:
            run(["xdotool", "key", "--clearmodifiers", keys])
        time.sleep(wait)
    def capture(name):
        capture_name = f"virtual-{name}" if virtual_display and not composition else name
        import sys
        sys.path.insert(0, str(root / "Tools/launchers/retrofe"))
        import window_placement
        focus = wid if composition else run(["xdotool", "getwindowfocus"])
        from PIL import Image, ImageStat
        destination = out / f"aeon-nox-{capture_name}-{width}x{height}-{stamp}.png"
        deadline = time.monotonic()+12
        while True:
            run(["import", "-display", env["DISPLAY"], "-window", focus, str(destination)])
            with Image.open(destination) as ready:
                if max(ImageStat.Stat(ready.convert("RGB")).stddev)>3: break
            if time.monotonic() >= deadline: raise RuntimeError(f"Blank native frame: {name}")
            time.sleep(.3)
        from PIL import Image
        with Image.open(out / f"aeon-nox-{capture_name}-{width}x{height}-{stamp}.png") as captured:
            from PIL import ImageStat
            with Image.open(out / f"aeon-nox-{capture_name}-{width}x{height}-{stamp}.png") as captured:
                if composition or name in {"options-a1", "options-a2-before-jump"}:
                    assert captured.size == (width,height), f"Wrong native canvas: {captured.size}"
                assert max(ImageStat.Stat(captured.convert("RGB")).stddev)>3, f"Blank capture: {name}"
            captures[name] = {"path": f"Docs/TestResults/aeon-nox-{capture_name}-{width}x{height}-{stamp}.png", "pixels": list(captured.size)}
    def wait_text(path, needle, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and needle in path.read_text(errors="replace"):
                return
            time.sleep(.1)
        raise RuntimeError(f"Timeout waiting for {needle} in {path}")
    def assert_panel_focus():
        active = run(["xdotool", "getactivewindow"])
        actual = run(["xprop", "-id", active, "WM_CLASS"])
        if "retrofeoptions" not in actual.lower():
            raise RuntimeError(f"Options did not receive focus; refusing to send menu keys: {actual}")
    try:
        sys.path.insert(0, str(root / "Tools/launchers/retrofe"))
        import window_placement
        if virtual_display and not composition:
            env["KWIN_COMPOSE"]="N"
            env["XDG_CONFIG_HOME"]=str(scratch / "config")
            launch(["kwin_x11", "--no-kactivities"], "kwin")
            time.sleep(2)
        monitors = window_placement.xrandr_monitors()
        target = next((m for m in monitors if m.rectangle.width >= width and m.rectangle.height >= height+40), None)
        if target:
            env["SDL_VIDEO_WINDOW_POS"] = f"{target.rectangle.x+40},{target.rectangle.y+60}"
        app = launch([str(root / "RetroFE")], "retrofe-stdout")
        wait_text(root / "log.txt", "[Layout] Initialized"); time.sleep(1)
        wid = run(["xdotool", "search", "--pid", str(app.pid), "--onlyvisible"]).splitlines()[0]
        run(["xprop", "-id", wid, "-f", "_MOTIF_WM_HINTS", "32c", "-set", "_MOTIF_WM_HINTS", "2, 0, 0, 0, 0"])
        if target:
            run(["xdotool", "windowmove", wid, str(target.rectangle.x+40), str(target.rectangle.y+60)])
            time.sleep(.4)
        run(["xdotool", "windowsize", wid, str(width), str(height)])
        run(["xdotool", "windowfocus" if composition else "windowactivate", "--sync", wid]); time.sleep(.4)
        if composition:
            import importlib.util
            spec=importlib.util.spec_from_file_location('aeon_fixture_geometry',root / "Tools/materializers/materialize-aeon-nox-layouts.py")
            fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
            geometry=fixture.composition_geometry(width,height)
            hx,hy,hw,hh=geometry['hero'];unit=geometry['unit']
            title_box=tuple(round(v) for v in (hx+30*unit,hy+hh*.43-30*unit,hx+hw-30*unit,hy+hh*.43+30*unit))
            def title_mask(name):
                from PIL import Image
                with Image.open(root/captures[name]['path']) as frame:
                    raw=frame.convert('RGB').crop(title_box).tobytes()
                return bytes(max(abs(raw[i+j]-v) for j,v in enumerate((180,198,209)))<=5 for i in range(0,len(raw),3))
            def initial_selection(name):
                candidate=title_mask(name)
                intersection=sum(a and b for a,b in zip(initial_title,candidate))
                union=sum(a or b for a,b in zip(initial_title,candidate))
                return intersection/max(1,union)>.8
            time.sleep(4)
            capture("composition-a1")
            initial_title=title_mask('composition-a1')
            assert sum(initial_title)>20, 'Initial platform title not ready'
            time.sleep(4); capture("composition-a1-idle")
            if ambient_cycle:
                cycle_start=time.monotonic()
                for seconds in (0,9,18,27,36,45):
                    pause=cycle_start+seconds-time.monotonic()
                    if pause>0: time.sleep(pause)
                    capture(f"composition-echo-cycle-{seconds}")
            key("Right", 2); capture('composition-a1-away')
            assert not initial_selection('composition-a1-away'), 'Fixture did not leave initial selection'
            key("Left", 2); time.sleep(2)
            capture("composition-a1-reselected")
            for _ in range(3):
                if initial_selection('composition-a1-reselected'): break
                key('Left',3); capture('composition-a1-reselected')
            assert initial_selection('composition-a1-reselected'), 'Fixture did not return to the initial platform'
            time.sleep(3.5); capture("composition-a1-reselected-idle")
            key("Return", 2); capture("composition-a2")
            key("Right", .7); capture("composition-a2-next")
            key("Right", .9); capture("composition-a2-next2")
            if width == 1920 and height == 1080:
                for _ in range(3): key("Right", 1)
                capture("composition-a2-real-cover")
            key("Escape", 2); capture("composition-return-a1")
            if not initial_selection('composition-return-a1'):
                key('Escape',3); capture('composition-return-a1')
            assert initial_selection('composition-return-a1'), 'Fixture did not return from games to A1; refusing Enter'
            key("space", 3); capture('composition-collections-entry')
            if initial_selection('composition-collections-entry'):
                key('space',3); capture('composition-collections-entry')
            assert not initial_selection('composition-collections-entry'), 'Fixture collection step was not received; refusing Enter'
            key("Return", 3); capture("composition-collections")
            if "collections/Coleções/layout/layout" not in (root / "log.txt").read_text(errors="replace"):
                # Retry only when the same root selection is still visible.
                entry=title_mask('composition-collections-entry');current=title_mask('composition-collections')
                overlap=sum(a and b for a,b in zip(entry,current))/max(1,sum(a or b for a,b in zip(entry,current)))
                assert overlap>.8, 'Unexpected context after Enter; refusing a blind retry'
                key('Return',3); capture('composition-collections')
            assert "collections/Coleções/layout/layout" in (root / "log.txt").read_text(errors="replace"), "Collections view was not entered"
            key("Down", .7); capture("composition-collections-focus")
            key("Escape", 1.5)
            key("q", 1)
            try: app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                key("q", 1); app.wait(timeout=15)
            engine_log = (root / "log.txt").read_text(errors="replace")
            assert "[ERROR]" not in engine_log, "Engine logged an error"
            assert "[Launcher]" not in engine_log, "Unexpected gameplay launcher activity"
            assert app.returncode == 0
            import hashlib, importlib.util
            spec=importlib.util.spec_from_file_location('aeon_runtime_geometry',root / "Tools/materializers/materialize-aeon-nox-layouts.py")
            generator=importlib.util.module_from_spec(spec);spec.loader.exec_module(generator)
            g=generator.composition_geometry(width,height);u=g['unit'];x,y,w,h=g['ticker']
            box=tuple(round(v) for v in (x+18*u,y+4*u,x+w-18*u,y+h-4*u))
            from PIL import Image, ImageChops, ImageStat
            ticker_evidence={}
            for early,late in (("composition-a1","composition-a1-idle"),("composition-a1-reselected","composition-a1-reselected-idle")):
                with Image.open(root/captures[early]['path']) as im: left=im.convert('L').crop(box)
                with Image.open(root/captures[late]['path']) as im: right=im.convert('L').crop(box)
                # Large PNG capture time adds to the nominal sleep. Search
                # half the viewport and retain glyph shape, rather than a
                # narrow 1D projection that can alias repeated letters.
                limit=left.width//2
                def error(delta):
                    lo=max(0,-delta);hi=min(left.width,left.width-delta)
                    difference=ImageChops.difference(left.crop((lo+delta,0,hi+delta,left.height)),right.crop((lo,0,hi,right.height)))
                    return ImageStat.Stat(difference).mean[0]
                coarse=min(range(-limit,limit+1,4),key=error)
                shift=min(range(max(-limit,coarse-4),min(limit,coarse+4)+1),key=error)
                mean=ImageStat.Stat(ImageChops.difference(left,right)).mean[0]
                ticker_evidence[early]={'crop':list(box),'mean_pixel_difference':mean,'estimated_shift_to_left_pixels':shift,'matched_mean_pixel_difference':error(shift),'method':'2D glyph comparison across half viewport'}
                assert mean>.2 and shift>1, f"Ticker did not move right-to-left after {early}: {ticker_evidence[early]}"
            import hashlib
            # A bare strip between the platform carousel and device panel is
            # deliberately unobscured. Confirm the actual rendered ambient
            # image has visible structure, rather than only an existing path.
            ambient_evidence={}
            for name in ('composition-a1','composition-a2'):
                # A2 cards occupy the lower strip, so sample its empty upper
                # left area instead; never confuse dark cover art with a
                # missing global background.
                region=(g['left'],height*.51,g['left']+g['content'],height*.53) if name=='composition-a1' else (g['left'],height*.18,g['left']+g['content']*.6,height*.30)
                ambient_box=tuple(round(v) for v in region)
                with Image.open(root/captures[name]['path']) as im:
                    stats=ImageStat.Stat(im.convert('RGB').crop(ambient_box))
                ambient_evidence[name]={'crop':list(ambient_box),'mean_rgb':stats.mean,'stddev_rgb':stats.stddev}
                # A quiet abstract surface can have little local variation;
                # this only rejects near-black/flat output, not art direction.
                assert max(stats.mean)>35 and max(stats.stddev)>1, f"Ambient artwork indistinguishable from black: {ambient_evidence[name]}"
            print(json.dumps({"status":"passed", "ticker_motion":ticker_evidence, "ambient_visibility":ambient_evidence, "captures":captures, "requested_size":[width,height],
                "engine_sha256":hashlib.sha256((root / "RetroFE").read_bytes()).hexdigest(),
                "generated_layouts_sha256":{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "layouts/Aeon Nox").glob("layout*.xml")},
                "materializer_sha256":hashlib.sha256((root / "Tools/materializers/materialize-aeon-nox-layouts.py").read_bytes()).hexdigest(),
                "scope":"Native renderer on isolated Xvfb; no physical input, audio, host placement or FPS certification"}), flush=True)
            return
        capture("options-a1")
        panel = launch([sys.executable, str(root / "Tools/launchers/retrofe/navigation-panel.py"),
                        "--root", str(root), "--app-pid", str(app.pid)], "panel")
        wait_text(scratch / "panel.log", '"event": "ready"')
        key("F10", .5)
        wait_text(scratch / "panel.log", '"event": "open"')
        assert_panel_focus()
        capture("options-panel")
        key("Down"); key("Return", .45)
        wait_text(scratch / "panel.log", '"screen": "themes"')
        capture("options-theme")
        key("Escape"); key("Escape", .5)
        assert app.poll() is None and panel.poll() is None
        capture("options-return-a1")
        key("Return", 1); capture("options-a2-before-jump")
        key("Prior", .65); capture("letter-jump-visible")
        time.sleep(.35); capture("letter-jump-destination")
        time.sleep(.35); capture("letter-jump-late")
        time.sleep(.6); capture("letter-jump-idle")
        key("Next", .65); capture("letter-reverse-visible")
        time.sleep(1.3); capture("letter-reverse-idle")
        key("F10", .5); assert_panel_focus(); capture("options-from-a2")
        for _ in range(5): key("Down", .18)
        key("Return", .4)
        wait_text(scratch / "panel.log", '"screen": "exit"')
        capture("exit-confirmation")
        key("Escape", .5)
        assert app.poll() is None
        capture("options-return-a2")
        key("F10", .5); assert_panel_focus()
        # A held Confirm key must not pass through to the frontend on resume.
        run(["xdotool", "keydown", "Return"]); time.sleep(.5)
        assert_panel_focus()
        run(["xdotool", "keyup", "Return"]); time.sleep(.5)
        assert app.poll() is None
        key("F10", .5); assert_panel_focus()
        for _ in range(5): key("Down", .18)
        key("Return", .4); key("Down"); key("Return", 1)
        app.wait(timeout=8)
        assert app.returncode == 0
        engine_log = (root / "log.txt").read_text(errors="replace")
        assert "[ERROR]" not in engine_log, "Engine logged an error"
        assert "[Launcher]" not in engine_log, "Unexpected gameplay launcher activity"
        result = {"status": "passed", "requested_size": [width, height], "captures": captures, "exit_code": app.returncode,
                  "checks": ["Options on A1", "theme submenu", "return A1", "A1 to A2",
                             "letter jump captures", "Options on A2", "cancel exit", "return A2",
                             "held Confirm waits for key release", "confirmed exit sends clean Q"]}
        print(json.dumps(result), flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill()
        for log in opened_logs: log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--stamp", default=time.strftime("%Y%m%d"), help="Data das evidências AAAAMMDD, com sufixo opcional de revisão")
    parser.add_argument("--ambient-cycle", action="store_true", help="Capture 45 seconds of stationary A1 in the isolated composition test")
    parser.add_argument("--virtual-display", action="store_true", help="Options test with an isolated D-Bus/Xvfb/KWin session")
    parser.add_argument("--composition", action="store_true", help="Native A1/A2 layout captures on an isolated virtual display; no physical input")
    parser.add_argument("--inner", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    if not re.fullmatch(r"\d{8}(?:-[a-z0-9]+)*", args.stamp): parser.error("--stamp deve usar AAAAMMDD[-revisao]")
    if args.inner:
        inner(root, args.inner, args.width, args.height, args.stamp, args.composition, args.virtual_display, args.ambient_cycle)
        return 0
    for command in (("bwrap", "xdotool", "xprop", "wmctrl", "import")
                    + (("xvfb-run", "Xvfb") if args.composition or args.virtual_display else ())
                    + (("dbus-run-session", "kwin_x11") if args.virtual_display and not args.composition else ())):
        if not shutil.which(command): raise RuntimeError(f"Missing test dependency: {command}")
    scratch = Path(tempfile.mkdtemp(prefix="retrofe-options-validation-"))
    out = root / "Docs/TestResults"
    process = None
    transcript = {}
    try:
        for name in ("runtime", "cache", "config", "UserData/retrofe"):
            (scratch / name).mkdir(parents=True, mode=0o700)
        for name in ("theme-profile.json", "display-profile.json"):
            shutil.copy2(root / "UserData/retrofe" / name, scratch / "UserData/retrofe" / name)
        settings = (root / "settings.conf").read_text()
        for key, value in {"layout": "Aeon Nox", "horizontal": args.width, "vertical": args.height, "fullscreen": "no", "fullscreen0": "no",
                           "fullscreen1": "no", "screenNum1": 0,
                           "horizontal0": args.width, "vertical0": args.height, "horizontal1": args.width,
                           "vertical1": args.height, "screenNum0": 0, "firstCollection": "Main",
                           "firstPlaylist": "all", "attractModeTime": 0}.items():
            settings = re.sub(r"(?m)^(" + re.escape(key) + r"\s*=\s*)[^\r\n#]*", lambda m: m[1]+str(value), settings)
        (scratch / "settings.conf").write_text(settings)
        (scratch / "log.txt").touch()
        # A dropped navigation key must never turn a visual test into gameplay.
        guard = scratch / "blocked-game-launch.py"
        guard.write_text("#!/usr/bin/env python3\nraise SystemExit('Gameplay is disabled in the visual audit')\n")
        guard.chmod(0o555)
        shutil.copytree(root / "collections/Main/playlists", scratch / "playlists")
        command = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                   *(["--ro-bind", "/dev/dri", "/dev/dri"] if Path("/dev/dri").is_dir() else []),
                   "--bind", str(scratch), str(scratch), "--bind", str(scratch / "UserData"), str(root / "UserData"),
                   "--bind", str(scratch / "settings.conf"), str(root / "settings.conf"),
                   "--bind", str(scratch / "log.txt"), str(root / "log.txt"),
                   "--ro-bind", str(guard), str(root / "Tools/launchers/emulators/host-emulator-launcher.py"),
                   "--bind", str(scratch / "playlists"), str(root / "collections/Main/playlists"),
                   "--bind", str(out), str(out), sys.executable, str(Path(__file__).resolve()),
                   "--root", str(root), "--width", str(args.width), "--height", str(args.height), "--inner", str(scratch), "--stamp", args.stamp]
        if args.composition: command += ["--composition"]
        if args.ambient_cycle: command += ["--ambient-cycle"]
        if args.virtual_display: command += ["--virtual-display"]
        if args.composition or args.virtual_display:
            command = ["xvfb-run", "--auto-servernum", "--server-args", f"-screen 0 {args.width+100}x{args.height+100}x24 -nolisten tcp", *command]
        if args.virtual_display and not args.composition:
            command = ["dbus-run-session", "--", *command]
        session_env = {**os.environ, "XDG_RUNTIME_DIR": str(scratch / "runtime"),
                       "XDG_CACHE_HOME": str(scratch / "cache"), "XDG_CURRENT_DESKTOP": "",
                       "QT_QPA_PLATFORMTHEME": "", "QT_NO_XDG_DESKTOP_PORTAL": "1", "QT_QPA_PLATFORM": "xcb"}
        session_env.pop("WAYLAND_DISPLAY", None)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=session_env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=110)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=8)
            transcript["timeout"] = True
        transcript.update(exit_code=process.returncode, stdout=stdout, stderr=stderr)
        print(stdout)
        print(stderr[-1600:])
    finally:
        for name in ("xvfb.log", "panel.log", "retrofe-stdout.log", "kwin.log", "log.txt"):
            p = scratch / name
            if p.exists():
                full = p.read_text(errors="replace")
                transcript[name] = full[-22000:]
                if name == "log.txt":
                    transcript["engine_log_head"] = full[:5000]
        report = out / f"aeon-nox-{'composition' if args.composition else ('virtual-options' if args.virtual_display else 'options')}-runtime-{args.width}x{args.height}-{args.stamp}.json"
        if report.exists():
            previous = json.loads(report.read_text())
            history = previous.pop("previous_attempts", [])
            transcript["previous_attempts"] = [*history, previous]
        report.write_text(json.dumps(transcript, ensure_ascii=False, indent=2)+"\n")
        shutil.rmtree(scratch)
    return 0 if process and process.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
