"""Session-only input routing. No grabs, global remaps, services or key logs.

The Steam lease stays with steam-input.py. Four stable SDL pads let native
emulators keep their port assignments while physical controllers hotplug.
"""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time
import uuid

VID = 0x1209
PRODUCTS = tuple(0xE101 + i for i in range(4))
EXIT_SECONDS = 2.0


def slots(devices, docked, previous=()):
    """Deck is P1 handheld; external pads fill dock ports in connection order.

    Compact disconnected ports; preserve surviving controllers' relative order.
    The built-in pad is intentionally excluded in dock mode.
    """
    external = [d for d in devices if d['kind'] == 'external']
    order = {key: i for i, key in enumerate(previous)}
    external.sort(key=lambda d: (order.get(d['path'], 999), d['path']))
    builtins = [d for d in devices if d['kind'] == 'deck']
    chosen = external if docked else builtins[:1] + external
    return [d['path'] for d in chosen[:4]]


class HeldChord:
    def __init__(self):
        self.since = None
        self.fired = False

    def update(self, pressed, focused, now):
        if not pressed or not focused:
            self.since, self.fired = None, False
            return False
        if self.since is None:
            self.since = now
        if not self.fired and now - self.since >= EXIT_SECONDS:
            self.fired = True
            return True
        return False


def identity(pid):
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


class OwnedProcesses:
    """Track start ticks, including descendants that later get reparented."""
    def __init__(self):
        self.members = {}

    def add(self, pid):
        value = identity(pid)
        if value:
            self.members[pid] = value[1]

    def refresh(self):
        self.members = {pid: start for pid, start in self.members.items()
                        if (v := identity(pid)) and v[1] == start}
        processes = {}
        for p in Path('/proc').iterdir():
            if p.name.isdigit() and (v := identity(int(p.name))):
                processes[int(p.name)] = v
        changed = True
        while changed:
            changed = False
            for pid, (parent, start) in processes.items():
                if parent in self.members and pid not in self.members:
                    self.members[pid] = start
                    changed = True

    def contains(self, pid):
        value = identity(pid)
        return bool(value and self.members.get(pid) == value[1])


class KWinFocus:
    """Event driven focus information from KWin; accept only KWin's D-Bus sender."""
    XML = '<node><interface name="org.emulation.ControllerSession"><method name="Focus"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="in"/><arg type="s" direction="in"/></method></interface></node>'

    def __init__(self, directory):
        from gi.repository import Gio, GLib
        directory=Path(directory).resolve(strict=True)
        self.GLib = GLib
        self.qdbus = shutil.which('qdbus6') or shutil.which('qdbus')
        if not self.qdbus:
            raise RuntimeError('Saída segura e foco exigem o backend KWin desta sessão.')
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetNameOwner', GLib.Variant('(s)', ('org.kde.KWin',)),
            GLib.VariantType('(s)'), Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        self.current = (0, '', False, '')
        self.fresh=threading.Event()
        self.refresh_count=0
        self.registration = self.connection.register_object('/org/emulation/ControllerSession',
            Gio.DBusNodeInfo.new_for_xml(self.XML).interfaces[0], self.callback, None, None)
        self.loop = GLib.MainLoop()
        self.thread = threading.Thread(target=self.loop.run, daemon=True)
        self.thread.start()
        self.name = 'emulation-input-' + uuid.uuid4().hex
        self.directory = directory
        script = directory / 'focus.js'
        bus=json.dumps(self.connection.get_unique_name())
        script.write_text('var watched = {}; function report() { '
            'callDBus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", '+bus+', function(alive) { '
            'if (!alive) { callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", '+json.dumps(self.name)+'); return; } '
            'var w=workspace.activeWindow; callDBus(' + bus +
            ', "/org/emulation/ControllerSession", "org.emulation.ControllerSession", "Focus", '
            'w ? w.pid : 0, w ? String(w.internalId) : "", w ? Boolean(w.dialog) : false, '
            'w && w.output ? String(w.output.name) : ""); '
            'if (w && !watched[String(w.internalId)]) { watched[String(w.internalId)] = true; '
            'w.outputChanged.connect(function(){ if(workspace.activeWindow === w) report(); }); } }); }\n'
            'workspace.windowActivated.connect(report); report(workspace.activeWindow);\n')
        try:
            self.script(self.name, script)
        except BaseException:
            self.close()
            raise

    def callback(self, connection, sender, path, interface, method, values, invocation):
        if sender != self.sender:
            invocation.return_dbus_error('org.emulation.NotKWin', 'Unexpected sender')
            return
        self.current = values.unpack()
        self.fresh.set()
        invocation.return_value(None)

    def dbus(self, *args):
        result = subprocess.run([self.qdbus, 'org.kde.KWin', *args], capture_output=True, text=True, timeout=3)
        if result.returncode:
            raise RuntimeError('KWin não confirmou a operação de janela da sessão: '+result.stderr.strip()[:300])
        return result.stdout.strip()

    def script(self, name, path):
        value = self.dbus('/Scripting', 'org.kde.kwin.Scripting.loadScript', str(Path(path).resolve(strict=True)), name)
        if not value.isdigit():
            raise RuntimeError('KWin recusou o script de foco da sessão.')
        self.dbus('/Scripting/Script' + value, 'org.kde.kwin.Script.run')

    def close_window(self, processes):
        if hasattr(self,'connection') and not self.refresh_current():return False
        pid, window, _, _ = self.current
        if not window or not processes.contains(pid):
            return False
        name = self.name + '-close'
        script = self.directory / 'close.js'
        # Recheck focus and identity inside KWin immediately before normal WM close.
        script.write_text('var w=workspace.activeWindow; if (w && w.pid === ' + str(pid) +
            ' && String(w.internalId) === ' + json.dumps(window) + ') w.closeWindow();\n')
        try:
            self.script(name, script)
        finally:
            self.dbus('/Scripting', 'org.kde.kwin.Scripting.unloadScript', name)
        return True

    def refresh_current(self):
        """Recheck XWayland PID/window identity after late window properties.

        Only on an unresolved focus or deliberate exit/dialog action; no
        polling service and no trust in a guessed PID or window caption.
        """
        self.fresh.clear()
        path=self.directory/'focus-refresh.js'
        path.write_text('var w=workspace.activeWindow; callDBus('+json.dumps(self.connection.get_unique_name())+
            ',"/org/emulation/ControllerSession","org.emulation.ControllerSession","Focus",w?w.pid:0,'
            'w?String(w.internalId):"",w?Boolean(w.dialog):false,w&&w.output?String(w.output.name):"");\n')
        name=self.name+'-refresh'
        try:self.script(name,path)
        finally:self.dbus('/Scripting','org.kde.kwin.Scripting.unloadScript',name)
        self.refresh_count+=1
        return self.fresh.wait(.5)

    def close(self):
        try:
            self.dbus('/Scripting', 'org.kde.kwin.Scripting.unloadScript', self.name)
        finally:
            self.connection.unregister_object(self.registration)
            self.loop.quit()
            self.thread.join(timeout=2)


def input_inventory():
    import evdev
    from evdev import ecodes as e
    rows = []
    for path in sorted(evdev.list_devices()):
        try:
            with closing(evdev.InputDevice(path)) as dev:
                if dev.info.vendor == VID and dev.info.product in (*PRODUCTS, 0xE1FF):
                    continue
                caps = dev.capabilities()
                keys = caps.get(e.EV_KEY, [])
                pad = e.BTN_SOUTH in keys and e.BTN_START in keys
                keyboard = e.KEY_A in keys and e.KEY_ENTER in keys
                if not (pad or keyboard):
                    continue
                virtual = (dev.info.vendor, dev.info.product) == (0x28DE, 0x11FF)
                deck = (dev.info.vendor, dev.info.product) == (0x28DE, 0x1205)
                if keyboard and dev.info.vendor == 0x28DE:
                    continue  # Never interpret Steam's mouse/keyboard emulation as a physical keyboard.
                kind = 'keyboard' if keyboard and not pad else ('deck_virtual' if virtual else 'deck' if deck else 'external')
                rows.append({'path': path, 'name': dev.name, 'vendor': dev.info.vendor,
                             'product': dev.info.product, 'kind': kind})
        except (OSError, PermissionError):
            continue
    # Steam may expose the built-in pad virtually. Only a single unambiguous
    # virtual instance is eligible; raw external pads are used independently.
    virtuals = [r for r in rows if r['kind'] == 'deck_virtual']
    if len(virtuals) == 1 and not any(r['kind'] == 'deck' for r in rows):
        virtuals[0]['kind'] = 'deck'
    return rows


def dock_mode(output):
    if output:
        return not output.lower().startswith(('edp', 'dsi', 'lvds'))
    # Missing focus must not silently select an external cable that isn't used.
    return False


def virtual_mappings():
    """Read actual SDL GUIDs even before these new pads have a mapping."""
    import ctypes
    import ctypes.util
    sdl = ctypes.CDLL(ctypes.util.find_library('SDL2-2.0'))
    class GUID(ctypes.Structure):
        _fields_ = [('data', ctypes.c_uint8 * 16)]
    sdl.SDL_JoystickGetDeviceGUID.argtypes = [ctypes.c_int]
    sdl.SDL_JoystickGetDeviceGUID.restype = GUID
    sdl.SDL_JoystickGetGUIDString.argtypes = [GUID, ctypes.c_char_p, ctypes.c_int]
    sdl.SDL_JoystickNameForIndex.argtypes = [ctypes.c_int]
    sdl.SDL_JoystickNameForIndex.restype = ctypes.c_char_p
    sdl.SDL_InitSubSystem(0x2000)
    result = []
    try:
        for i in range(sdl.SDL_NumJoysticks()):
            if sdl.SDL_JoystickGetDeviceVendor(i) != VID or sdl.SDL_JoystickGetDeviceProduct(i) not in PRODUCTS:
                continue
            value = ctypes.create_string_buffer(33)
            sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(i), value, 33)
            name = sdl.SDL_JoystickNameForIndex(i).decode()
            result.append(value.value.decode()+','+name+',a:b0,b:b1,x:b3,y:b2,back:b6,start:b7,guide:b8,leftstick:b9,rightstick:b10,leftshoulder:b4,rightshoulder:b5,leftx:a0,lefty:a1,rightx:a3,righty:a4,lefttrigger:a2,righttrigger:a5,dpup:h0.1,dpright:h0.2,dpdown:h0.4,dpleft:h0.8,platform:Linux,')
    finally:
        sdl.SDL_QuitSubSystem(0x2000)
    return result


def source_gamepads():
    """Use SDL's device database to distinguish Xbox and Sony raw layouts."""
    from emulator_controls import gamepads
    hints = {'SDL_JOYSTICK_HIDAPI':'0','SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT':'',
             'SDL_GAMECONTROLLER_IGNORE_DEVICES':'','SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD':'1',
             'SDL_JOYSTICK_HIDAPI_STEAMDECK':'0'}
    previous = {k:os.environ.get(k) for k in hints}
    try:
        os.environ.update(hints)
        return gamepads()
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def binding_value(token, buttons, axes, hats):
    """SDL mapping token -> normalized [-1,1] axis or [0,1] button."""
    if not token:return 0
    if token.startswith('b'):
        return float(int(token[1:]) in buttons)
    if token.startswith('h'):
        hat,mask=token[1:].split('.')
        return float(bool(hats.get(int(hat),0) & int(mask)))
    invert=token.endswith('~')
    token=token.rstrip('~')
    half=token[0] if token.startswith(('+','-')) else ''
    token=token.lstrip('+-')
    if token.startswith('a'):
        value=axes.get(int(token[1:]),0)
        if invert:value=-value
        return max(0,value) if half=='+' else max(0,-value) if half=='-' else value
    return 0


def diagnose(root):
    from emulator_controls import coverage
    try:
        devices = input_inventory()
        error = None
    except ImportError:
        devices, error = [], 'Python evdev ausente'
    output=''
    qdbus=shutil.which('qdbus6') or shutil.which('qdbus')
    if qdbus:
        try:
            result=subprocess.run([qdbus,'org.kde.KWin','/KWin','org.kde.KWin.activeOutputName'],capture_output=True,text=True,timeout=3)
            if result.returncode==0:output=result.stdout.strip()
        except (OSError,subprocess.TimeoutExpired):pass
    return {'devices': devices, 'uinput_writable': os.access('/dev/uinput', os.W_OK),
            'active_output':output or None,'docked':dock_mode(output) if output else None,
            'kwin_available':bool(output),
            'backend': 'KWin + evdev/uinput, sessão somente', 'error': error,
            'exit': {'gamepad': 'Back/Select + Start', 'keyboard': 'Ctrl + Shift + F10', 'hold_seconds': 2},
            'policy': {'handheld': 'Deck P1; externos P2-P4', 'dock': 'externos P1-P4; teclado sem externo',
                       'hotplug': True, 'exclusive_grabs': False}, 'coverage': coverage(root),
            'physical_gameplay_verified': False}


class Session:
    def __init__(self, root, system_id):
        import evdev
        from evdev import ecodes as e
        import romtool
        self.evdev, self.e = evdev, e
        self.root, self.system_id = root, system_id
        self.operation_id = 'controller-session-' + time.strftime('%Y%m%dT%H%M%S-') + uuid.uuid4().hex[:8]
        self.directory = romtool.operation_dir(self.operation_id)
        self.directory.mkdir(mode=0o700)
        self.inputs, self.virtual, self.assigned = {}, [], []
        self.source_maps = {}
        self.source_signature = None
        self.output_state = [{}, {}, {}, {}]
        self.effects = [{}, {}, {}, {}]
        self.forwarded_effects = [{}, {}, {}, {}]
        self.processes, self.exit_chords = OwnedProcesses(), {}
        self.last_scan = 0
        self.last_process_scan = 0
        self.keyboard_down = set()
        self.keyboard_state = {}
        self.deck_identity = None
        self.exit_pending = False
        self.dialog_armed = False
        self.dialog_down = set()
        self.was_exit_dialog = False
        self.last_focus = False
        self.focus_retry_key=None
        self.focus_retries=0
        self.last_focus_retry=0
        self.focused_seconds = 0.0
        self.last_tick = time.monotonic()
        self.manifest = {'kind': 'controller_session', 'operation_id': self.operation_id,
            'created_at': romtool.now(), 'status': 'preparing', 'system_id': system_id,
            'owner': {'pid':os.getpid(),'start_ticks':identity(os.getpid())[1]},
            'physical_gameplay_verified': False, 'bytes_freed': 0, 'changes': []}
        self.save()
        try:
            self.focus = KWinFocus(self.directory)
            self.manifest['kwin_script_name']=self.focus.name
            axes = [(e.ABS_X, (-32768, 32767)), (e.ABS_Y, (-32768, 32767)),
                    (e.ABS_Z, (0, 255)), (e.ABS_RX, (-32768, 32767)),
                    (e.ABS_RY, (-32768, 32767)), (e.ABS_RZ, (0, 255)),
                    (e.ABS_HAT0X, (-1, 1)), (e.ABS_HAT0Y, (-1, 1))]
            self.axis_limits = dict(axes)
            self.buttons = [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST,
                e.BTN_TL, e.BTN_TR, e.BTN_SELECT, e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR]
            capabilities = {e.EV_KEY: self.buttons,
                e.EV_FF: [e.FF_RUMBLE],
                e.EV_ABS: [(code, evdev.AbsInfo(0 if bounds[0] < 0 else bounds[0], *bounds, 0, 0, 0)) for code, bounds in axes]}
            for n, product in enumerate(PRODUCTS):
                self.virtual.append(evdev.UInput(capabilities, name=f'Emulation P{n+1}', vendor=VID,
                    product=product, version=1, bustype=e.BUS_USB, phys=f'emulation/player/{n+1}', max_effects=16))
            self.dialog_keyboard = evdev.UInput({e.EV_KEY:[e.KEY_ENTER,e.KEY_ESC,e.KEY_LEFT,e.KEY_RIGHT,e.KEY_UP,e.KEY_DOWN]},
                name='Emulation Session Dialog',vendor=VID,product=0xE1FF,phys='emulation/dialog')
            self.env = os.environ.copy()
            self.env['SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT'] = ','.join(f'0x{VID:04x}/0x{p:04x}' for p in PRODUCTS)
            self.env['SDL_JOYSTICK_HIDAPI'] = '0'
            self.env['EMULATION_ROUTED_CONTROLS'] = '1'
            # Linux evdev button order is A,B,X,Y,LB,RB,Back,Start,Guide,LS,RS.
            from emulator_controls import gamepads
            deadline = time.monotonic()+4
            while True:
                mappings = virtual_mappings()
                if len(mappings) == 4 or time.monotonic() >= deadline: break
                time.sleep(.1)
            self.env['SDL_GAMECONTROLLERCONFIG'] = '\n'.join(filter(None, [self.env.get('SDL_GAMECONTROLLERCONFIG', ''), *mappings]))
            keys = ['SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT', 'SDL_GAMECONTROLLERCONFIG', 'SDL_JOYSTICK_HIDAPI']
            old = {k:os.environ.get(k) for k in keys}
            os.environ.update({k:self.env[k] for k in keys})
            try:
                deadline = time.monotonic()+4
                while True:
                    pads = gamepads()
                    if len(pads) == 4 or time.monotonic() >= deadline:
                        break
                    time.sleep(.1)
            finally:
                for key,value in old.items():
                    if value is None: os.environ.pop(key,None)
                    else: os.environ[key] = value
            if len(pads) != 4 or any(p['vendor'] != VID for p in pads):
                raise RuntimeError('As quatro portas SDL da sessão não foram enumeradas com isolamento.')
            self.manifest.update(status='ready', ports=[{'name': p['name'], 'guid': p['guid']} for p in pads])
            self.save()
        except BaseException:
            self.close('preparation_failed')
            raise

    def save(self):
        import romtool
        romtool.write_manifest(self.manifest)

    def neutral(self, port):
        v = self.virtual[port]
        for key in self.buttons: self.write(port,self.e.EV_KEY,key,0)
        for key, limits in self.axis_limits.items(): self.write(port,self.e.EV_ABS,key,0 if limits[0]<0 else limits[0])
        v.syn()

    def write(self, port, kind, code, value):
        if self.output_state[port].get((kind,code)) != value:
            self.virtual[port].write(kind,code,value)
            self.output_state[port][kind,code] = value

    def stop_effects(self):
        for entries in self.forwarded_effects:
            for device,effect_id in entries.values():
                try:
                    device.write(self.e.EV_FF,effect_id,0)
                    device.erase_effect(effect_id)
                except OSError: pass
            entries.clear()

    def force_feedback(self, port, event):
        e, v = self.e, self.virtual[port]
        if event.type == e.EV_UINPUT and event.code == e.UI_FF_UPLOAD:
            upload = v.begin_upload(event.value)
            try:
                self.effects[port][upload.effect.id] = upload.effect
                previous = self.forwarded_effects[port].pop(upload.effect.id,None)
                if previous:
                    try: previous[0].erase_effect(previous[1])
                    except OSError: pass
                upload.retval = 0
            finally: v.end_upload(upload)
        elif event.type == e.EV_UINPUT and event.code == e.UI_FF_ERASE:
            erase = v.begin_erase(event.value)
            try:
                self.effects[port].pop(erase.effect_id,None)
                previous = self.forwarded_effects[port].pop(erase.effect_id,None)
                if previous:
                    try: previous[0].erase_effect(previous[1])
                    except OSError: pass
                erase.retval = 0
            finally: v.end_erase(erase)
        elif event.type == e.EV_FF:
            if port >= len(self.assigned): return
            source = self.inputs.get(self.assigned[port])
            if not source or e.FF_RUMBLE not in source.capabilities().get(e.EV_FF,[]): return
            effect = self.effects[port].get(event.code)
            if effect is None: return
            previous = self.forwarded_effects[port].get(event.code)
            if not previous:
                effect.id = -1
                effect_id = source.upload_effect(effect)
                self.forwarded_effects[port][event.code] = (source,effect_id)
            else: effect_id = previous[1]
            source.write(e.EV_FF,effect_id,event.value)

    def scan(self, docked):
        rows = input_inventory()
        for row in rows:
            if row['kind'] == 'deck' and row['vendor'] == 0x28DE:
                try:self.deck_identity = (row['path'],os.stat(row['path']).st_ino)
                except OSError:pass
            if row['kind'] == 'deck_virtual' and self.deck_identity:
                try:
                    if self.deck_identity == (row['path'],os.stat(row['path']).st_ino):row['kind']='deck'
                except OSError:pass
        available = {r['path']: r for r in rows}
        for path in list(self.inputs):
            if path not in available:
                self.inputs.pop(path).close()
                self.keyboard_down.clear()
                self.keyboard_state.pop(path, None)
                self.source_maps.pop(path,None)
        for row in rows:
            path = row['path']
            if path not in self.inputs and row['kind'] in {'deck', 'external', 'keyboard'}:
                try: self.inputs[path] = self.evdev.InputDevice(path)
                except OSError: continue
        signature=tuple((r['path'],r['kind']) for r in rows)
        if signature != self.source_signature:
            mappings=source_gamepads()
            self.source_signature=signature
            for row in rows:
                if row['kind'] not in {'deck','external'} or row['path'] not in self.inputs:continue
                matches=[p for p in mappings if p.get('path')==row['path']]
                if not matches:
                    matches=[p for p in mappings if (p['vendor'],p['product'])==(row['vendor'],row['product'])]
                unique={json.dumps(p['mapping'],sort_keys=True):p['mapping'] for p in matches}
                if len(unique)==1:self.source_maps[row['path']]=next(iter(unique.values()))
            self.manifest['unmapped_controllers']=[r['name'] for r in rows if r['kind'] in {'deck','external'} and r['path'] not in self.source_maps]
            self.manifest['source_mappings']=[{'name':r['name'],'mapping':self.source_maps[r['path']]}
                for r in rows if r['path'] in self.source_maps]
            self.manifest['ambiguous_steam_virtuals']=sum(r['kind']=='deck_virtual' for r in rows)
            if self.manifest['unmapped_controllers']:
                print('[CONTROLE] Há controle sem mapeamento SDL seguro; preservado para diagnóstico, teclado mantido.',flush=True)
            self.save()
        new = slots([r for r in rows if r['path'] in self.inputs and r['path'] in self.source_maps], docked, self.assigned)
        if new != self.assigned or docked != self.manifest.get('docked'):
            self.stop_effects()
            for n in range(4): self.neutral(n)
            self.assigned = new
            self.keyboard_down.clear()
            self.keyboard_state.clear()
            self.exit_chords.clear()
            self.manifest['docked'] = docked
            change = {'at': time.time(), 'players': [{'player': n+1, 'name': available[p]['name']} for n,p in enumerate(new)],
                      'keyboard_fallback': not new, 'docked': docked}
            self.manifest['changes'].append(change)
            self.save()
        self.rows = available

    def forward(self, device, port):
        """Translate the whole current state after an event batch; writes are diffed."""
        e=self.e
        mapping=self.source_maps[device.path]
        caps=device.capabilities(absinfo=True)
        keys=sorted(k for k in caps.get(e.EV_KEY,[]) if k>=e.BTN_MISC)
        active=set(device.active_keys())
        buttons={n for n,key in enumerate(keys) if key in active}
        # capabilities() retains the values read when the device was opened.
        # EVIOCGABS supplies the live state for sticks, triggers and d-pad hats.
        raw_axes={key:device.absinfo(key) for key,_ in caps.get(e.EV_ABS,[])}
        axes={}
        for n,(code,info) in enumerate((k,v) for k,v in sorted(raw_axes.items()) if not e.ABS_HAT0X<=k<=e.ABS_HAT3Y):
            axes[n]=2*(info.value-info.min)/(info.max-info.min)-1 if info.max>info.min else 0
        hats={}
        for n in range(4):
            x=raw_axes.get(e.ABS_HAT0X+n*2)
            y=raw_axes.get(e.ABS_HAT0Y+n*2)
            hats[n]=(1 if y and y.value<0 else 4 if y and y.value>0 else 0)|(8 if x and x.value<0 else 2 if x and x.value>0 else 0)
        def value(key):return binding_value(mapping.get(key,''),buttons,axes,hats)
        if getattr(self,'exit_pending',False) and self.dialog_armed:
            desired={key for binding,key in {'a':e.KEY_ENTER,'b':e.KEY_ESC,'dpleft':e.KEY_LEFT,
                'dpright':e.KEY_RIGHT,'dpup':e.KEY_UP,'dpdown':e.KEY_DOWN}.items() if value(binding)>.5}
            if desired-self.dialog_down:
                if not self.focus.refresh_current() or not self.processes.contains(self.focus.current[0]):
                    self.neutral(port)
                    return
            if self.focus.current[2]:
                for key in desired-self.dialog_down:
                    for state in (1,0):
                        self.dialog_keyboard.write(e.EV_KEY,key,state)
                        self.dialog_keyboard.syn()
                self.dialog_down=desired
                self.neutral(port)
                return
        for key,code in {'a':e.BTN_SOUTH,'b':e.BTN_EAST,'x':e.BTN_WEST,'y':e.BTN_NORTH,
                'back':e.BTN_SELECT,'start':e.BTN_START,'guide':e.BTN_MODE,'leftshoulder':e.BTN_TL,
                'rightshoulder':e.BTN_TR,'leftstick':e.BTN_THUMBL,'rightstick':e.BTN_THUMBR}.items():
            self.write(port,e.EV_KEY,code,int(value(key)>.5))
        for key,code in {'leftx':e.ABS_X,'lefty':e.ABS_Y,'rightx':e.ABS_RX,'righty':e.ABS_RY}.items():
            self.write(port,e.EV_ABS,code,round(max(-1,min(1,value(key)))*32767))
        for key,code in [('lefttrigger',e.ABS_Z),('righttrigger',e.ABS_RZ)]:
            token=mapping.get(key,'')
            v=value(key)
            if token.startswith('a'):v=(v+1)/2
            self.write(port,e.EV_ABS,code,round(max(0,min(1,v))*255))
        self.write(port,e.EV_ABS,e.ABS_HAT0X,int(value('dpright')>.5)-int(value('dpleft')>.5))
        self.write(port,e.EV_ABS,e.ABS_HAT0Y,int(value('dpdown')>.5)-int(value('dpup')>.5))
        self.virtual[port].syn()

    def axis(self, device, code, value):
        source = device.absinfo(code)
        target = self.axis_limits[code]
        if not source or source.max <= source.min: return 0
        value = target[0] + (value-source.min)*(target[1]-target[0])/(source.max-source.min)
        return round(max(target[0], min(target[1], value)))

    def tick(self):
        e, now = self.e, time.monotonic()
        if now-self.last_process_scan > .25:
            self.processes.refresh()
            self.last_process_scan = now
        focused = self.processes.contains(self.focus.current[0])
        retry_key=self.focus.current[:2]
        if retry_key!=self.focus_retry_key:self.focus_retry_key=retry_key;self.focus_retries=0
        if (not focused and self.focus.current[1] and self.focus_retries<3
                and now-self.last_focus_retry>1):
            self.last_focus_retry=now;self.focus_retries+=1
            self.focus.refresh_current()
            focused=self.processes.contains(self.focus.current[0])
        if focused:
            self.focused_seconds += min(now-self.last_tick, .25)
            if not self.manifest.get('owned_window_seen'):
                self.manifest['owned_window_seen'] = True
                self.save()
        self.last_tick = now
        if not focused and self.last_focus:
            self.stop_effects()
            for n in range(4): self.neutral(n)
            self.keyboard_down.clear()
            self.keyboard_state.clear()
            self.dialog_down.clear()
        if self.exit_pending and self.focus.current[2] and focused:
            self.was_exit_dialog=True
        elif self.was_exit_dialog and focused:
            self.exit_pending=self.dialog_armed=self.was_exit_dialog=False
            self.dialog_down.clear()
        self.last_focus = focused
        if now-self.last_scan > 1:
            self.scan(dock_mode(self.focus.current[3]) if focused else self.manifest.get('docked', False))
            self.last_scan = now
        ready, _, _ = select.select([*self.inputs.values(),*self.virtual], [], [], .04)
        for dev in ready:
            if dev in self.virtual:
                for event in dev.read():
                    try:self.force_feedback(self.virtual.index(dev),event)
                    except OSError: pass  # A disappearing pad cannot invalidate the game session.
                continue
            try: events = list(dev.read())
            except (OSError, BlockingIOError): continue
            row = self.rows.get(dev.path, {})
            forwarded=False
            for event in events:
                if event.type == e.EV_SYN and event.code == e.SYN_DROPPED:
                    if dev.path in self.assigned: self.neutral(self.assigned.index(dev.path))
                    continue
                if not focused: continue
                if row.get('kind') == 'keyboard':
                    if event.type == e.EV_KEY and event.value != 2:
                        state = self.keyboard_state.setdefault(dev.path, set())
                        if event.value: state.add(event.code)
                        else: state.discard(event.code)
                        self.keyboard_down = set().union(*self.keyboard_state.values())
                    continue
                if dev.path not in self.assigned: continue
                port = self.assigned.index(dev.path)
                v = self.virtual[port]
                if event.type in {e.EV_KEY,e.EV_ABS}:forwarded=True
            if forwarded and dev.path in self.assigned:self.forward(dev,self.assigned.index(dev.path))
        pressed = self.keyboard_down
        keyboard_exit = bool(pressed & {e.KEY_LEFTCTRL,e.KEY_RIGHTCTRL}) and bool(pressed & {e.KEY_LEFTSHIFT,e.KEY_RIGHTSHIFT}) and e.KEY_F10 in pressed
        chords = {'keyboard': keyboard_exit}
        for path in self.assigned:
            state=self.output_state[self.assigned.index(path)]
            chords[path] = bool(state.get((e.EV_KEY,e.BTN_SELECT)) and state.get((e.EV_KEY,e.BTN_START)))
        for key, held in chords.items():
            if self.exit_chords.setdefault(key, HeldChord()).update(held, focused, now):
                self.exit_pending = self.focus.close_window(self.processes)
                self.manifest['exit_requested'] = bool(self.exit_pending)
                self.save()
        if self.exit_pending and not any(chords.values()): self.dialog_armed = True
        # Keyboard fallback through the same stable pad, only while our game
        # owns focus. Physical keyboard events are never suppressed or logged.
        profile = getattr(self,'keyboard_fallback',None)
        if profile is None:
            host=json.loads((self.root/'frontend/RetroFE/Config/profiles/host-linux.json').read_text())
            entry = next((v for v in host['collections'].values() if v['rom_subdir']==self.system_id),{})
            self.keyboard_fallback = self.system_id not in {'psx','ps2','gc','wii','wiiware'}
            self.ra_keyboard=None
            if entry.get('adapter')=='retroarch':
                # Native keyboard driver may be X11 while video is Wayland.
                # Reuse its logical keys instead of guessing another layout.
                import re
                data=(self.root/'frontend/RetroFE'/host['retroarch']['config_path']).read_text()
                configured=dict(re.findall(r'(?m)^input_player1_(\w+)\s*=\s*"([^"\n]*)"',data))
                defaults={'b':'z','a':'x','y':'a','x':'s','start':'enter','select':'rshift','l':'q','r':'w',
                          'up':'up','down':'down','left':'left','right':'right'}
                def keycode(name):
                    name={'enter':'enter','rshift':'rightshift','shift':'leftshift','ctrl':'leftctrl','alt':'leftalt'}.get(name,name)
                    return getattr(e,'KEY_'+name.upper(),None)
                self.ra_keyboard={name:keycode(configured.get(name,default)) for name,default in defaults.items()}
        if focused and not self.assigned and self.keyboard_fallback:
            mapping = {e.KEY_Z:e.BTN_SOUTH, e.KEY_X:e.BTN_EAST, e.KEY_A:e.BTN_WEST,
                e.KEY_S:e.BTN_NORTH, e.KEY_Q:e.BTN_TL, e.KEY_E:e.BTN_TR,
                e.KEY_BACKSPACE:e.BTN_SELECT, e.KEY_ENTER:e.BTN_START}
            if self.ra_keyboard is not None:
                logical={'b':e.BTN_SOUTH,'a':e.BTN_EAST,'y':e.BTN_WEST,'x':e.BTN_NORTH,
                         'l':e.BTN_TL,'r':e.BTN_TR,'select':e.BTN_SELECT,'start':e.BTN_START}
                mapping={self.ra_keyboard[key]:button for key,button in logical.items() if self.ra_keyboard.get(key) is not None}
            v = self.virtual[0]
            for key, button in mapping.items(): self.write(0,e.EV_KEY,button,int(key in pressed))
            for code, minus, plus in [(e.ABS_HAT0X,e.KEY_LEFT,e.KEY_RIGHT),(e.ABS_HAT0Y,e.KEY_UP,e.KEY_DOWN),
                    (e.ABS_X,e.KEY_LEFT,e.KEY_RIGHT),(e.ABS_Y,e.KEY_UP,e.KEY_DOWN),
                    (e.ABS_RX,e.KEY_J,e.KEY_L),(e.ABS_RY,e.KEY_I,e.KEY_K)]:
                self.write(0,e.EV_ABS,code,(int(plus in pressed)-int(minus in pressed))*(1 if code in {e.ABS_HAT0X,e.ABS_HAT0Y} else 32767))
            self.write(0,e.EV_ABS,e.ABS_Z,255 if e.KEY_1 in pressed else 0)
            self.write(0,e.EV_ABS,e.ABS_RZ,255 if e.KEY_3 in pressed else 0)
            v.syn()

    def close(self, status='completed_with_validation_limits'):
        self.stop_effects()
        for dev in self.inputs.values(): dev.close()
        for v in self.virtual: v.close()
        if hasattr(self,'dialog_keyboard'):self.dialog_keyboard.close()
        if hasattr(self, 'focus'): self.focus.close()
        self.manifest['status'] = status
        self.manifest['focused_seconds'] = round(self.focused_seconds,2)
        self.manifest['focus_refresh_count']=getattr(getattr(self,'focus',None),'refresh_count',0)
        self.save()
        import romtool
        romtool.append_event(self.operation_id, 'controller_session_closed', status=status,
            details={'system_id': self.system_id, 'physical_gameplay_verified': False})
        romtool.rebuild_history_index()


def run(root, system_id, command, env, attach=None, remaining=None, owned_children=None):
    """One input router for one owned emulator. Lifetime includes descendants."""
    session = Session(root, system_id)
    child = None
    status='preparation_failed'
    try:
        combined = dict(env)
        combined.update({k:v for k,v in session.env.items() if k.startswith(('SDL_', 'EMULATION_'))})
        if system_id == 'ps5' and str(root/'emulator/linux/ps5pcem/game-run') in command:
            session.manifest['runtime_input_limit'] = 'PS5PCEM Linux: native keyboard only; no gamepad/rumble backend'
            session.save()
            print('[CONTROLE] PS5PCEM Linux aceita somente teclado no jogo; o controle mantém a combinação de saída. Kyty usa outro backend, cuja jogabilidade exige validação.',flush=True)
        # Native control preparation uses exactly the same SDL view as the game.
        helper = root / 'frontend/RetroFE/Tools/launchers/retrofe/steam-input.py'
        subprocess.run([os.sys.executable, str(helper), '--root', str(root/'frontend/RetroFE'),
            'controller-prepare', '--system-id', system_id, '--require-gamepad'], env=combined, check=True)
        child = subprocess.Popen(command, env=combined)
        status='completed_with_validation_limits'
        session.processes.add(child.pid)
        if attach: attach(child.pid)
        while child.poll() is None or (remaining and remaining()):
            if owned_children:
                for row in owned_children():
                    current = identity(int(row['pid']))
                    if current and current[1] == row['start_ticks']:
                        session.processes.add(int(row['pid']))
            session.tick()
        return child.returncode
    except BaseException:
        if child is not None:status='interrupted_with_child_preserved'
        raise
    finally:
        # Never kill a live child or remove its devices after a recoverable loop
        # exception. A wrapper may exit before its detached emulator, so both
        # the original child and the Steam-owned descendants must finish.
        if child is not None:
            while True:
                try:
                    if child.poll() is not None and not (remaining and remaining()):
                        break
                except Exception:
                    # An inconclusive process scan cannot authorize removal of
                    # input devices. Retry until ownership can be checked.
                    time.sleep(.2)
                    continue
                try:
                    if owned_children:
                        for row in owned_children():
                            current = identity(int(row['pid']))
                            if current and current[1] == row['start_ticks']:
                                session.processes.add(int(row['pid']))
                    session.tick()
                except Exception:
                    time.sleep(.1)
        session.close(status)
