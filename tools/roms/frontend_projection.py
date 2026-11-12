"""Single native ES-DE command projection shared with the control configurator."""
import json
import importlib.util
from pathlib import Path
import shlex
import subprocess
import xml.etree.ElementTree as ET
try:
    from .native_bindings import safe_path
except ImportError:
    from native_bindings import safe_path
HOST = Path('frontend/RetroFE')


def esde_settings_projection(root):
    """Reuse the existing settings configurator and read-only media gate."""
    from esde_media import command_validate
    source = root / 'frontend/ES-DE/configure-rom-directory.py'
    spec = importlib.util.spec_from_file_location('canonical_esde_settings', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rom_root, _, _ = module.project_rom_root()
    media = safe_path(root, 'media')
    if media.is_symlink() or not media.is_dir():
        raise ValueError('Raiz de mídia canônica ausente ou indireta')
    validation = command_validate(None, {'MEDIA_DEST_ROOT': media, 'PROJECT_ROOT': root})
    if validation['status'] != 'ready':
        raise ValueError('Mídia ES-DE bloqueada: ' + json.dumps(validation['issues'], ensure_ascii=False))
    path = module.settings_file()
    before = path.read_text(encoding='utf-8') if path.is_file() else ''
    after, _ = module.updated_settings_text(before, rom_root, media.resolve())
    return path.relative_to(root), after.encode('utf-8')

def esde_projection(root, profile, platform='linux'):
    systems_path = Path("frontend/ES-DE/UserData/custom_systems/es_systems.xml")
    rules_path = systems_path.with_name("es_find_rules.xml")
    systems_file, rules_file = safe_path(root, str(systems_path)), safe_path(root, str(rules_path))
    systems = ET.fromstring(systems_file.read_bytes()) if systems_file.exists() else ET.Element("systemList")
    rules = ET.fromstring(rules_file.read_bytes()) if rules_file.exists() else ET.Element("ruleList")
    if systems.tag != "systemList" or rules.tag != "ruleList":
        raise ValueError("Schema ES-DE personalizado inesperado")
    emulator = "EMULATION-PROJECT"
    desired_rule = "%ROMPATH%/../frontend/RetroFE/Tools/launchers/emulators/host-emulator-launcher.py"
    linux_rule = desired_rule
    windows_rule = '%ROMPATH%/../emulator/windows/python/python.exe'
    quote = shlex.quote if platform == 'linux' else lambda value: subprocess.list2cmdline([value])
    script_prefix = ''
    if platform == 'windows':
        desired_rule = windows_rule
        script_prefix = ' -X utf8 "%ROMPATH%/../frontend/RetroFE/Tools/launchers/emulators/host-emulator-launcher.py"'
    existing = rules.findall(f"emulator[@name='{emulator}']")
    if len(existing) > 1:
        raise ValueError("Regra ES-DE duplicada")
    if existing:
        if existing[0].findtext("rule/entry") not in {linux_rule, windows_rule}:
            raise ValueError("Regra ES-DE personalizada preservada")
        existing[0].find("rule/entry").text = desired_rule
    else:
        entry = ET.SubElement(ET.SubElement(rules, "emulator", name=emulator), "rule", type="staticpath")
        ET.SubElement(entry, "entry").text = desired_rule
    catalog = json.loads((root / HOST / "Config/platform-catalog.json").read_text())
    catalog = {p["id"]: p for p in catalog["systems"]}
    covered = []
    for collection, entry in profile["collections"].items():
        sid = entry["rom_subdir"]
        if entry["adapter"] == "unavailable" and not any(n.findtext("name") == sid and any(c.get("label", "").startswith("Projeto (") for c in n.findall("command")) for n in systems.findall("system")):
            continue
        definition = catalog.get(sid, {"display_name": collection,
                                       "extensions": entry.get("extensions", "").split(","),
                                       "frontends": entry.get("frontends", {})})
        found = systems.findall(f"system[name='{sid}']")
        if len(found) > 1:
            raise ValueError(f"Sistema ES-DE duplicado: {sid}")
        node = found[0] if found else ET.SubElement(systems, "system")
        if not found:
            for key, value in {"name": sid, "fullname": definition["display_name"],
                               "path": f"%ROMPATH%/{sid}",
                               "extension": " ".join(f".{x} .{x.upper()}" for x in definition["extensions"]),
                               "platform": sid, "theme": sid}.items():
                ET.SubElement(node, key).text = value
        command = f"%EMULATOR_{emulator}%{script_prefix} --collection {quote(collection)} --item %ROM%"
        commands = node.findall("command")
        managed = [n for n in commands if n.get("label") == "Projeto (controle compartilhado)"]
        if len(managed) > 1:
            raise ValueError(f"Comando ES-DE duplicado: {sid}")
        if managed:
            managed[0].text = command
        elif not commands or (sid == "switch2" and len(commands) == 1 and commands[0].text == "%EMULATOR_EDEN-NX2% %ROM%"):
            for old in commands:
                node.remove(old)
            ET.SubElement(node, "command", label="Projeto (controle compartilhado)").text = command
        else:
            # Existing custom systems need a deliberate choice, not a silent replacement.
            continue
        if any(node.find(field) is None for field in ("extension", "platform", "theme")):
            raise ValueError(f"Sistema ES-DE gerenciado incompleto, edição preservada: {sid}")
        extensions = set(definition["extensions"]) | set(entry.get("extensions", "").split(","))
        extensions.update(node.findtext("extension", "").lower().replace(".", "").split())
        extensions.discard("")
        node.find("extension").text = " ".join(f".{x} .{x.upper()}" for x in sorted(extensions))
        metadata = definition.get("frontends", {}).get("esde", {})
        for field in ("platform", "theme"):
            if metadata.get(field):
                node.find(field).text = metadata[field]
        covered.append(sid)
        for runtime_id,runtime in entry.get('runtime_options',{}).items():
            label='Projeto ('+runtime_id+')'
            found=node.findall("command[@label='"+label+"']")
            if len(found)>1:raise ValueError('Alternativa ES-DE duplicada: '+sid+'/'+runtime_id)
            desired=command+' --runtime '+quote(runtime_id)
            if found and not (found[0].text or '').startswith('%EMULATOR_'+emulator+'% '):
                raise ValueError('Alternativa ES-DE personalizada preservada: '+label)
            alternative=found[0] if found else ET.SubElement(node,'command',label=label)
            alternative.text=desired
    result = []
    for path, xml in [(systems_path, systems), (rules_path, rules)]:
        ET.indent(xml, space="    ")
        result.append((path, ET.tostring(xml, encoding="utf-8", xml_declaration=True) + b"\n"))
    return result, covered
