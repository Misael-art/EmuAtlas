#!/usr/bin/env python3
"""Exercise theme round-trip and independent recovery on an owned live session.

Requires --launcher-pid of a session opened for this test. Sends visible keyboard
commands, switches registered themes and exits normally. The crash phase kills
only that launcher and its local guards, leaving the frontend to exit via Options.
Stops if physical SDL input is observed. Does not launch games or edit media.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"Tools/launchers/retrofe"))
import steam_desktop_session as steam
import window_placement as windows
STATE = steam.state_directory().parent
LOG = STATE/"input-debug-overlay.log"

class Exercise:
    def __init__(self,pid):
        self.pid=pid
        self.cursor=LOG.stat().st_size if LOG.exists() else 0
        self.events=[]
        self.expected=None
    def log(self,event,**data):
        row={"time":time.strftime("%Y-%m-%dT%H:%M:%S"),"event":event,**data}
        self.events.append(row); print(json.dumps(row,ensure_ascii=False),flush=True)
    def physical_idle(self):
        if LOG.exists():
            with LOG.open() as log:
                log.seek(self.cursor); new=log.read();self.cursor=log.tell()
            if '"input": "SDL ' in new:raise RuntimeError("Entrada física detectada: teste interrompido para não disputar o controle.")
    def run(self,args):
        result=subprocess.run(args,capture_output=True,text=True,timeout=8)
        if result.returncode:raise RuntimeError(f"{args}: {result.stderr}")
        return result.stdout.strip()
    def wait(self,condition,timeout=55):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.physical_idle()
            value=condition()
            if value:return value
            time.sleep(.25)
        raise RuntimeError("Timeout aguardando estado esperado")
    def ready(self,theme,previous=None):
        def current():
            for p in STATE.glob("navigation-panel-*.json"):
                try:
                    d=json.loads(p.read_text())
                    if d["theme_id"]!=theme or d["app_pid"]==previous:continue
                    stat=Path(f"/proc/{d['app_pid']}/stat").read_text()
                    fields=stat[stat.rfind(")")+2:].split()
                    if int(fields[1])==self.pid and steam.proc_identity(d["pid"]):return d
                except (OSError,ValueError,KeyError):pass
        data=self.wait(current);time.sleep(3)
        self.log("session-ready",**data)
        return data
    def key(self,key,delay=.35):
        self.physical_idle();self.run(["xdotool","key","--clearmodifiers",key]);time.sleep(delay)
    def focus(self,app):
        target=windows.find_window(pid=app,class_hint="RetroFE")
        if not target:raise RuntimeError("Janela não encontrada")
        self.run(["wmctrl","-ia",target.window_id]);time.sleep(.3)
    def assert_panel(self):
        active=self.run(["xdotool","getactivewindow"])
        if "retrofeoptions" not in self.run(["xprop","-id",active,"WM_CLASS"]).lower():raise RuntimeError("Options não recebeu foco; recuso enviar seleções")
    def capture(self,name):
        active=self.run(["xdotool","getactivewindow"])
        path=ROOT/f"Docs/TestResults/retrofe-session-safety-{name}-{time.strftime('%Y%m%d')}.png"
        self.run(["import","-window",active,str(path)])
        self.log("capture",path=str(path.relative_to(ROOT)))
    def options(self,app):
        self.focus(app);self.key("F10",.6);self.assert_panel()
    def switch(self,session,target,index):
        self.options(session["app_pid"])
        self.key("Down");self.key("Return");self.assert_panel()
        for _ in range(index):self.key("Down")
        self.key("Return",.6)
        return self.ready(target,session["app_pid"])
    def exit(self,app):
        self.options(app)
        for _ in range(5):self.key("Down",.18)
        self.key("Return");self.assert_panel()
        self.capture("exit-confirmation")
        self.key("Down");self.key("Return",.8)
        self.wait(lambda:not steam.proc_identity(app),timeout=15)
    def desktop(self):
        self.wait(lambda:not (steam.state_directory()/steam.STATE_NAME).exists() and not (steam.state_directory()/"kde-global-shortcut-guard.json").exists(),timeout=45)
        current=steam.inspect()
        for saved in self.expected:
            target=next((c for c in current if c["type"]==saved["type"]),None)
            if not target or any(target[k]!=saved[k] for k in ("url","gamepad","keyboard","mouse")):raise RuntimeError(f"Desktop não restaurado: {current}")
        self.log("desktop-restored",controllers=current,recovery=steam.snapshot())
    def execute(self):
        initial=self.wait(lambda:self.owned_snapshot())
        owner=initial["owners"].get(str(self.pid)) if initial else None
        if not owner or not steam.alive(owner):raise RuntimeError("Launcher informado não possui esta sessão Steam; teste recusado")
        self.expected=initial["controllers"]
        preference=json.loads((ROOT/"UserData/retrofe/theme-profile.json").read_text())
        session=self.ready(preference["theme_id"])
        self.options(session["app_pid"]);self.capture(preference["theme_id"]+"-options");self.key("Escape",.6)
        if preference["theme_id"]!="aura-memorias":session=self.switch(session,"aura-memorias",1)
        self.options(session["app_pid"]);self.capture("aura-options");self.key("Escape",.6)
        session=self.switch(session,"aeon-nox",0)
        self.options(session["app_pid"]);self.capture("aeon-options");self.key("Escape",.6)
        # From A1 Enter opens a platform; never confirm a game in the frontend.
        self.focus(session["app_pid"]);self.key("Return",1)
        self.options(session["app_pid"]);self.capture("a2-options");self.key("Escape",.6)
        self.exit(session["app_pid"]);self.desktop()
        self.wait(lambda:not steam.proc_identity(self.pid),timeout=15)
        self.log("theme-round-trip-and-normal-exit-passed")
        launcher=subprocess.Popen([str(ROOT/"Tools/launchers/retrofe/launch-retrofe.sh"),"--input-debug"],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.pid=launcher.pid
        session=self.ready("aeon-nox")
        observer=self.observer(session["pid"])
        if not observer:raise RuntimeError("Observador de teclado não ficou ativo")
        os.kill(observer,signal.SIGKILL)
        self.wait(lambda:self.observer(session["pid"]) not in (None,observer))
        self.options(session["app_pid"]);self.capture("recovered-keyboard");self.key("Escape",.6)
        self.log("keyboard-observer-reconnected")
        os.kill(session["pid"],signal.SIGKILL)
        previous=session["pid"]
        session=self.wait(lambda:self.replacement(session["app_pid"],previous))
        self.options(session["app_pid"]);self.capture("recovered-panel");self.key("Escape",.6)
        self.log("navigation-panel-restarted",helper=session["pid"])
        # Remove only this session's local guards to exercise the independent
        # systemd/login guard, rather than accidentally proving the old watchdog.
        state=steam.snapshot();owner=state["owners"][str(self.pid)]
        local_guards=[owner["watchdog_pid"]]
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:
                args=(proc/'cmdline').read_bytes().split(b'\0')
                if b'guard' in args and b'watch' in args and str(session["app_pid"]).encode() in args and any(a.endswith(b'/input-session.py') for a in args):local_guards.append(int(proc.name))
            except OSError:pass
        for pid in local_guards:
            if steam.proc_identity(pid):os.kill(pid,signal.SIGKILL)
        os.kill(self.pid,signal.SIGKILL);launcher.wait(timeout=5)
        time.sleep(3)
        current=steam.inspect()
        if not any(c['url']==steam.GAMEPAD_URL for c in current):raise RuntimeError("Desktop foi retomado com frontend ainda vivo")
        self.log("supervisor-and-local-guards-killed",pids=local_guards,frontend_still_alive=session['app_pid'])
        self.exit(session["app_pid"]);self.desktop()
        self.run(["systemctl","--user","is-active","retrofe-input-recovery.service"])
        self.log("independent-recovery-passed")
    def owned_snapshot(self):
        state=steam.snapshot()
        return state if state and str(self.pid) in state['owners'] else None
    def replacement(self,app,old):
        p=STATE/f"navigation-panel-{app}.json"
        try:
            data=json.loads(p.read_text())
            return data if data["pid"]!=old and steam.proc_identity(data["pid"]) else None
        except (OSError,ValueError):return None
    def observer(self,helper):
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:
                stat=(proc/'stat').read_text();fields=stat[stat.rfind(')')+2:].split()
                if int(fields[1])==helper and fields[0]!='Z' and b'xinput' in (proc/'cmdline').read_bytes():return int(proc.name)
            except (OSError,ValueError):pass

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launcher-pid',type=int,required=True)
    args=parser.parse_args();test=Exercise(args.launcher_pid)
    status,error='passed',None
    try:test.execute()
    except Exception as exc:status,error='failed',str(exc);test.log('failure',reason=error)
    finally:
        report={'status':status,'error':error,'events':test.events,'scope':'real host keyboard/theme/API; no gameplay; no reboot performed'}
        path=ROOT/f"Docs/TestResults/retrofe-session-safety-runtime-{time.strftime('%Y%m%d')}.json"
        if path.exists():
            previous=json.loads(path.read_text());history=previous.pop('previous_attempts',[])
            report['previous_attempts']=[*history,previous]
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    raise SystemExit(0 if status=='passed' else 1)
