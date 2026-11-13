"""Session-only input routing. No grabs, global remaps, services or key logs.

The Steam lease stays with steam-input.py. Stable SDL pads let native
emulators keep their port assignments while physical controllers hotplug.
"""
from __future__ import annotations

import json
import importlib.util
import hashlib
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
DEFAULT_PLAYERS = 8
PRODUCTS = tuple(0xE101 + i for i in range(DEFAULT_PLAYERS))
EXIT_SECONDS = 2.0
ORDER_PATH = Path('frontend/RetroFE/UserData/retrofe/controller-order.json')


def player_capacity(root):
    config = Path(root) / 'frontend/RetroFE/Config/controller-artwork.json'
    value = json.loads(config.read_text()).get('player_capacity', DEFAULT_PLAYERS) if config.exists() else DEFAULT_PLAYERS
    if type(value) is not int or not 1 <= value <= 128:
        raise ValueError('Capacidade de jogadores inválida no mapeamento dos controles.')
    return value


def device_key(row):
    return row.get('controller_id') or hashlib.sha256(row['path'].encode()).hexdigest()


def load_preferences(root):
    from emulator_controls import target
    path = target(root, ORDER_PATH)
    if not path.exists():
        return {'schema': 1, 'revision': 0, 'slots': None, 'models': {}}
    if path.stat().st_size > 16384:
        raise ValueError('Preferências de controles excedem o tamanho suportado.')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Preferências de controles inválidas.')
    ports = value.get('slots')
    if (value.get('schema') != 1 or type(value.get('revision')) is not int
            or value['revision'] < 0 or not isinstance(value.get('models', {}), dict)):
        raise ValueError('Preferências de controles inválidas.')
    if ports is not None:
        capacity = player_capacity(root)
        if not isinstance(ports, list) or not 1 <= len(ports) <= capacity:
            raise ValueError('A ordem excede a capacidade configurada de jogadores.')
        keys = [key for key in ports if key is not None]
        if any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in keys) or len(set(keys)) != len(keys):
            raise ValueError('Identidade duplicada ou inválida na ordem dos controles.')
        value['slots'] = ports + [None] * (capacity-len(ports))
    models = value.get('models', {})
    if any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 128 or len(v) > 128 for k, v in models.items()):
        raise ValueError('Identificação visual de controle inválida.')
    return {**value, 'models': models}


def save_preferences(root, value, expected_revision):
    """One durable preference file, serialized with native control preparation."""
    from emulator_controls import configuration_lock, target
    import romtool
    with configuration_lock():
        previous = load_preferences(root)
        if previous['revision'] != expected_revision:
            raise ValueError('A ordem mudou em outra janela. Tente novamente.')
        path = target(root, ORDER_PATH)
        before = path.read_bytes() if path.exists() else None
        after = {**value, 'schema': 1, 'revision': expected_revision + 1}
        operation_id = 'controller-order-' + time.strftime('%Y%m%dT%H%M%S-') + uuid.uuid4().hex[:8]
        directory = romtool.operation_dir(operation_id)
        directory.mkdir(mode=0o700)
        if before is not None:
            romtool.atomic_write_bytes(directory / 'before.json', before, mode=0o600)
        payload = (json.dumps(after, ensure_ascii=False, indent=2) + '\n').encode()
        romtool.atomic_write_bytes(directory / 'after.json', payload, mode=0o600)
        manifest = {'operation_id': operation_id, 'kind': 'controller_assignment',
            'created_at': romtool.now(), 'status': 'prepared', 'path': str(ORDER_PATH),
            'sha256_before': hashlib.sha256(before).hexdigest() if before is not None else None,
            'sha256_after': hashlib.sha256(payload).hexdigest(), 'slots': after['slots'],
            'models': after['models'], 'physical_gameplay_verified': False}
        romtool.write_manifest(manifest)
        try:
            if (path.read_bytes() if path.exists() else None) != before:
                raise ValueError('Preferências alteradas durante a aplicação.')
            romtool.atomic_write_bytes(path, payload, mode=0o600)
            if path.read_bytes() != payload:
                raise RuntimeError('A nova ordem não foi confirmada em disco.')
            manifest['status'] = 'completed_verified'
            romtool.write_manifest(manifest)
            romtool.append_event(operation_id, 'controller_order_updated', status='completed_verified',
                destination=str(ORDER_PATH), sha256_before=manifest['sha256_before'],
                sha256_after=manifest['sha256_after'])
        except Exception as exc:
            if path.exists() and path.read_bytes() == payload:
                if before is None:
                    os.rename(path, directory / 'uncommitted.json')
                else:
                    romtool.atomic_write_bytes(path, before, mode=0o600)
            manifest['status'] = 'failed_restored'
            manifest['error'] = str(exc)
            romtool.write_manifest(manifest)
            raise
        return after


def assign_player(root, devices, assigned, source_id, player, revision):
    capacity = player_capacity(root)
    if type(player) is not int or not 0 <= player < capacity:
        raise ValueError('Jogador de destino inválido.')
    available = {device_key(row): row for row in devices if row.get('routable', True)}
    if source_id not in available:
        raise ValueError('O controle foi desconectado ou está sem mapeamento seguro.')
    by_path = {row['path']: key for key, row in available.items()}
    ports = [by_path.get(path) for path in assigned]
    ports = (ports + [None] * capacity)[:capacity]
    source = ports.index(source_id) if source_id in ports else None
    if source == player:
        return load_preferences(root)
    if source is not None:
        ports[source], ports[player] = ports[player], ports[source]
    else:
        ports[player] = source_id
    prefs = load_preferences(root)
    return save_preferences(root, {**prefs, 'slots': ports}, revision)


def identify_controller(root, source_id, model, revision):
    prefs = load_preferences(root)
    models = {**prefs['models'], source_id: model}
    return save_preferences(root, {**prefs, 'models': models}, revision)


def slots(devices, docked, previous=(), preferred=None, capacity=DEFAULT_PLAYERS):
    """Use explicit ports when present, preserving holes and surviving identities.

    Automatic handheld order starts with Deck. Automatic dock order uses external
    pads. An explicit preference can place the built-in pad in either mode.
    """
    external = [d for d in devices if d['kind'] == 'external']
    order = {key: i for i, key in enumerate(previous)}
    external.sort(key=lambda d: (order.get(d['path'], 999), d['path']))
    builtins = [d for d in devices if d['kind'] == 'deck']
    chosen = external if docked else builtins[:1] + external
    if preferred is not None:
        available = {device_key(d): d['path'] for d in devices if d['kind'] in {'deck', 'external'}}
        selected = [available.get(key) for key in preferred] + [None] * (capacity-len(preferred))
        remaining = [d['path'] for d in chosen if d['path'] not in selected]
        for n, path in enumerate(selected):
            if path is None and remaining:
                selected[n] = remaining.pop(0)
        return selected
    return [d['path'] for d in chosen[:capacity]]


def source_mapping(row, pads):
    from mobile_controller import REMOTE_PRODUCT, RAW_MAPPING
    if row.get('vendor') == VID and row.get('product') == REMOTE_PRODUCT and row.get('mobile'):
        return dict(RAW_MAPPING)
    matches = [p for p in pads if p.get('path') == row['path']]
    if not matches:
        matches = [p for p in pads if (p.get('vendor'), p.get('product')) == (row['vendor'], row['product'])]
    unique = {json.dumps(p['mapping'], sort_keys=True): p['mapping'] for p in matches if p.get('mapping')}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


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
    XML = '<node><interface name="org.emulation.ControllerSession"><method name="Focus"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="in"/><arg type="s" direction="in"/></method><method name="Geometry"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method></interface></node>'

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
        self.geometry = None
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
            'var g=w ? (w.clientGeometry || w.frameGeometry) : null; '
            'callDBus('+bus+', "/org/emulation/ControllerSession", "org.emulation.ControllerSession", "Geometry", '
            'g?Math.round(g.x):0,g?Math.round(g.y):0,g?Math.round(g.width):0,g?Math.round(g.height):0); '
            'if (w && !watched[String(w.internalId)]) { watched[String(w.internalId)] = true; '
            'w.outputChanged.connect(function(){ if(workspace.activeWindow === w) report(); }); '
            'w.frameGeometryChanged.connect(function(){ if(workspace.activeWindow === w) report(); }); } }); }\n'
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
        if method == 'Geometry':
            self.geometry = values.unpack()
        else:
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
                if dev.info.vendor == VID and 0xE101 <= dev.info.product <= 0xE1FF:
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
                identifier = f'{dev.info.vendor:04x}:{dev.info.product:04x}:{dev.uniq or dev.phys or path}'
                rows.append({'path': path, 'name': dev.name, 'vendor': dev.info.vendor,
                             'product': dev.info.product, 'kind': kind, 'bus': dev.info.bustype,
                             'mobile': dev.info.vendor == VID and dev.info.product == 0xE201 and (dev.phys or '').startswith('emuatlas/mobile/'),
                             'device_cookie': os.fstat(dev.fd).st_ino,
                             'controller_id': hashlib.sha256(identifier.encode()).hexdigest()})
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


def virtual_mappings(products=PRODUCTS):
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
            if sdl.SDL_JoystickGetDeviceVendor(i) != VID or sdl.SDL_JoystickGetDeviceProduct(i) not in products:
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
            'policy': {'handheld': 'Deck P1; externos nas portas seguintes', 'dock': 'externos por ordem; teclado sem externo',
                       'player_capacity': player_capacity(root), 'mobile_controllers': 'QR Code de sessão, rede local',
                       'hotplug': True, 'manual_player_order': True, 'occupied_target': 'swap',
                       'manual_overrides_automatic': True, 'preference_file': str(ORDER_PATH),
                       'exclusive_grabs': False}, 'coverage': coverage(root),
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
        self.preferences = {'schema': 1, 'revision': 0, 'slots': None, 'models': {}}
        self.wait_for_release = set()
        self.capacity = player_capacity(root)
        self.products = tuple(0xE101+n for n in range(self.capacity))
        self.output_state = [{} for _ in self.products]
        self.effects = [{} for _ in self.products]
        self.forwarded_effects = [{} for _ in self.products]
        from mobile_controller import Client
        self.mobile = Client(root)
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
            for n, product in enumerate(self.products):
                self.virtual.append(evdev.UInput(capabilities, name=f'Emulation P{n+1}', vendor=VID,
                    product=product, version=1, bustype=e.BUS_USB, phys=f'emulation/player/{n+1}', max_effects=16))
            self.dialog_keyboard = evdev.UInput({e.EV_KEY:[e.KEY_ENTER,e.KEY_ESC,e.KEY_LEFT,e.KEY_RIGHT,e.KEY_UP,e.KEY_DOWN]},
                name='Emulation Session Dialog',vendor=VID,product=0xE1FF,phys='emulation/dialog')
            self.env = os.environ.copy()
            self.env['SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT'] = ','.join(f'0x{VID:04x}/0x{p:04x}' for p in self.products)
            self.env['SDL_JOYSTICK_HIDAPI'] = '0'
            self.env['EMULATION_ROUTED_CONTROLS'] = '1'
            self.env['EMULATION_PLAYER_CAPACITY'] = str(self.capacity)
            # Linux evdev button order is A,B,X,Y,LB,RB,Back,Start,Guide,LS,RS.
            from emulator_controls import gamepads
            deadline = time.monotonic()+4
            while True:
                mappings = virtual_mappings(self.products)
                if len(mappings) == self.capacity or time.monotonic() >= deadline: break
                time.sleep(.1)
            self.env['SDL_GAMECONTROLLERCONFIG'] = '\n'.join(filter(None, [self.env.get('SDL_GAMECONTROLLERCONFIG', ''), *mappings]))
            keys = ['SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT', 'SDL_GAMECONTROLLERCONFIG', 'SDL_JOYSTICK_HIDAPI']
            old = {k:os.environ.get(k) for k in keys}
            os.environ.update({k:self.env[k] for k in keys})
            try:
                deadline = time.monotonic()+4
                while True:
                    pads = gamepads()
                    if len(pads) == self.capacity or time.monotonic() >= deadline:
                        break
                    time.sleep(.1)
            finally:
                for key,value in old.items():
                    if value is None: os.environ.pop(key,None)
                    else: os.environ[key] = value
            if len(pads) != self.capacity or any(p['vendor'] != VID for p in pads):
                raise RuntimeError('As portas SDL da sessão não foram enumeradas com isolamento.')
            self.manifest.update(status='ready', ports=[{'name': p['name'], 'guid': p['guid']} for p in pads])
            self.feedback = None
            try:
                path = root / 'frontend/RetroFE/Tools/launchers/retrofe/session-feedback.py'
                spec = importlib.util.spec_from_file_location('emulation_session_feedback', path)
                feedback = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(feedback)
                self.feedback = feedback.SessionFeedback(game=True, root=root,
                    on_assign=self.reassign, on_identify=self.identify, on_add=self.mobile.pairing)
                self.manifest['feedback'] = 'ready'
            except Exception as exc:
                self.manifest['feedback_error'] = str(exc)
                print('[CONTROLE] Avisos visuais indisponíveis: ' + str(exc), flush=True)
            self.save()
        except BaseException:
            self.close('preparation_failed')
            raise

    def save(self):
        import romtool
        romtool.write_manifest(self.manifest)

    def controller_rows(self):
        mobile = {r['controller_id']: r for r in getattr(getattr(self, 'mobile', None), 'clients', [])}
        return [{**row, 'controller_id': device_key(row), 'routable': row['path'] in self.source_maps,
                 'display_name': mobile.get(device_key(row), {}).get('name', row['name']),
                 'online': mobile.get(device_key(row), {}).get('online', True),
                 'model_override': self.preferences['models'].get(device_key(row))}
                for row in self.rows.values() if row['kind'] in {'deck', 'external'}]

    def reassign(self, source_id, player):
        docked = self.manifest.get('docked', False)
        self.scan(docked)
        self.preferences = assign_player(self.root, self.controller_rows(), self.assigned,
                                         source_id, player, self.preferences['revision'])
        self.scan(docked)

    def identify(self, source_id, model):
        if source_id not in {device_key(row) for row in self.controller_rows()}:
            raise ValueError('O controle foi desconectado.')
        self.preferences = identify_controller(self.root, source_id, model, self.preferences['revision'])
        self.scan(self.manifest.get('docked', False))

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
        previous_rows = getattr(self, 'rows', {})
        replaced = set()
        for path in list(self.inputs):
            changed = path in available and previous_rows.get(path, {}).get('device_cookie') != available[path].get('device_cookie')
            if path not in available or changed:
                replaced.add(path)
                self.inputs.pop(path).close()
                self.keyboard_down.clear()
                self.keyboard_state.pop(path, None)
                self.source_maps.pop(path,None)
        for row in rows:
            path = row['path']
            if path not in self.inputs and row['kind'] in {'deck', 'external', 'keyboard'}:
                try: self.inputs[path] = self.evdev.InputDevice(path)
                except OSError: continue
        signature = tuple((r['path'], r['kind'], device_key(r), r.get('device_cookie')) for r in rows)
        if signature != self.source_signature:
            mappings=source_gamepads()
            self.source_signature=signature
            for row in rows:
                if row['kind'] not in {'deck','external'} or row['path'] not in self.inputs:continue
                mapping = source_mapping(row, mappings)
                if mapping:
                    self.source_maps[row['path']] = mapping
                else:
                    self.source_maps.pop(row['path'], None)
            self.manifest['unmapped_controllers']=[r['name'] for r in rows if r['kind'] in {'deck','external'} and r['path'] not in self.source_maps]
            self.manifest['source_mappings']=[{'name':r['name'],'mapping':self.source_maps[r['path']]}
                for r in rows if r['path'] in self.source_maps]
            self.manifest['ambiguous_steam_virtuals']=sum(r['kind']=='deck_virtual' for r in rows)
            if self.manifest['unmapped_controllers']:
                print('[CONTROLE] Há controle sem mapeamento SDL seguro; preservado para diagnóstico, teclado mantido.',flush=True)
            self.save()
        try:
            self.preferences = load_preferences(self.root)
        except (ValueError, OSError) as exc:
            message = str(exc)
            if self.manifest.get('controller_order_error') != message:
                self.manifest['controller_order_error'] = message
                self.save()
        new = slots([r for r in rows if r['path'] in self.inputs and r['path'] in self.source_maps],
                    docked, self.assigned, self.preferences['slots'], getattr(self, 'capacity', DEFAULT_PLAYERS))
        if new != self.assigned or docked != self.manifest.get('docked') or replaced.intersection(new):
            self.stop_effects()
            for n in range(len(self.virtual)): self.neutral(n)
            self.assigned = new
            self.wait_for_release = {path for path in new if path}
            self.keyboard_down.clear()
            self.keyboard_state.clear()
            self.exit_chords.clear()
            self.manifest['docked'] = docked
            change = {'at': time.time(), 'players': [{'player': n+1, 'name': available[p]['name']}
                      for n,p in enumerate(new) if p], 'keyboard_fallback': not any(new), 'docked': docked,
                      'order_revision': self.preferences['revision']}
            self.manifest['changes'].append(change)
            self.save()
            for port, path in enumerate(new):
                if path:
                    self.forward(self.inputs[path], port)
        self.rows = available
        if getattr(self, 'feedback', None):
            self.feedback.set_devices(self.controller_rows(),
                [device_key(available[p]) if p else None for p in self.assigned],
                keyboard=not any(self.assigned))

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
        waiting = getattr(self, 'wait_for_release', set())
        if device.path in waiting:
            held = any(value(key) > .5 for key in ('a', 'b', 'x', 'y', 'start', 'back',
                'leftshoulder', 'rightshoulder', 'leftstick', 'rightstick',
                'dpup', 'dpdown', 'dpleft', 'dpright'))
            moved = any(abs(value(key)) > .35 for key in ('leftx', 'lefty', 'rightx', 'righty'))
            triggers = any(value(key) > (-.8 if mapping.get(key, '').startswith('a') else .1)
                           for key in ('lefttrigger', 'righttrigger') if key in mapping)
            if not held and not moved and not triggers:
                waiting.discard(device.path)
            self.neutral(port)
            return
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
            for n in range(len(self.virtual)): self.neutral(n)
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
        if getattr(self, 'mobile', None):
            players = {device_key(self.rows[path]): n for n, path in enumerate(self.assigned)
                       if path and path in self.rows}
            self.mobile.sync(focused, players, self.reassign)
        ready, _, _ = select.select([*self.inputs.values(),*self.virtual], [], [], .02)
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
            if path is None:
                continue
            state=self.output_state[self.assigned.index(path)]
            chords[path] = bool(state.get((e.EV_KEY,e.BTN_SELECT)) and state.get((e.EV_KEY,e.BTN_START)))
        for key, held in chords.items():
            if self.exit_chords.setdefault(key, HeldChord()).update(held, focused, now):
                self.exit_pending = self.focus.close_window(self.processes)
                self.manifest['exit_requested'] = bool(self.exit_pending)
                self.manifest['exit_input'] = {'kind': 'keyboard' if key == 'keyboard' else 'gamepad',
                    'player': None if key == 'keyboard' else self.assigned.index(key) + 1}
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
        if focused and not any(self.assigned) and self.keyboard_fallback:
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

        if getattr(self, 'feedback', None):
            try:
                self.feedback.update(focused, self.focus.geometry, now)
            except Exception as exc:
                # A failed status layer must preserve the running game/input.
                self.manifest['feedback_error'] = str(exc)
                self.feedback.close()
                self.feedback = None
                self.save()

    def close(self, status='completed_with_validation_limits'):
        if getattr(self, 'mobile', None):
            self.mobile.close()
        if getattr(self, 'feedback', None):
            self.feedback.close()
            self.feedback = None
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
        # The durable manifest/journal are authoritative. Rebuilding the aggregate
        # history cache belongs to maintenance, outside the input/exit lifecycle.


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
