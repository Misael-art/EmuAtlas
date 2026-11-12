#!/usr/bin/env python3
"""Create an empty, repeatable customer workspace; acquire pinned external components."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener

HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parents[1]
MARKER = ".emulation-workspace.json"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def root_path(path):
    p = Path(os.path.abspath(Path(path).expanduser()))
    for part in (p, *p.parents):
        if part.is_symlink():
            raise ValueError(f"Raiz com symlink recusada: {part}")
    if p == Path(p.anchor) or p == Path.home():
        raise ValueError("Use uma pasta exclusiva do produto")
    return p


def safe_path(root, value):
    name = PurePosixPath(value)
    if not value or name.is_absolute() or ".." in name.parts or "\\" in value:
        raise ValueError(f"Caminho relativo inválido: {value}")
    p = root.joinpath(*name.parts)
    for part in (p, *p.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError(f"Symlink recusado: {part}")
    return p


def write_new(path, value):
    """Never replace customer files, including an identical file on repeat setup."""
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Configuração existente diferente; preservada: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    return True


@contextmanager
def lock(root):
    path = safe_path(root, "outputs/workspace-install.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def configuration(root, systems, product):
    template_dir = HERE / "templates"
    configs = {}
    for p in sorted(template_dir.rglob("*.json")):
        value = read(p)
        # romtool resolves these tokens at runtime, including after relocation.
        configs[p.relative_to(template_dir).as_posix()] = value
    configs[MARKER] = {"schema_version": 1, "product": product["slug"],
                       "version": product["version"], "systems": systems}
    return configs


def plan(root, systems):
    root = root_path(root)
    product = read(HERE / "config/product.json")
    registry = read(HERE / "templates/frontend/RetroFE/Config/platform-catalog.json")
    available = {s["id"] for s in registry["systems"]}
    systems = sorted(set(systems))
    if any(s not in available or not re.fullmatch(r"[a-z0-9_-]+", s) for s in systems):
        raise ValueError("Sistema desconhecido; consulte o catálogo de plataformas")
    marker = root / MARKER
    if not marker.exists():
        for name in ["roms", "bios", "saves", "media", "emulator/linux", "emulator/windows", "emulator/data", "frontend/RetroFE/UserData", "frontend/ES-DE/UserData"]:
            p = safe_path(root, name)
            if p.exists() and (not p.is_dir() or any(p.iterdir())):
                raise ValueError(f"Biblioteca existente sem marcador: {p}. Não será adotada automaticamente.")
    configs = configuration(root, systems, product)
    directories = list(product["directories"])
    for system in systems:
        directories += [f"roms/{system}"]
        directories += [f"media/{system}/{kind}" for kind in product["media_categories"]]
    conflicts = []
    for name in directories:
        p = safe_path(root, name)
        if p.exists() and not p.is_dir():
            conflicts.append(name)
    for name, value in configs.items():
        p = safe_path(root, name)
        if p.exists() and read(p) != value:
            conflicts.append(name)
    return {"root": str(root), "status": "blocked" if conflicts else "ready",
            "directories": directories, "configs": configs, "conflicts": conflicts,
            "downloads": False, "library_payloads_modified": False}


def initialize(root, systems, apply=False):
    proposal = plan(root, systems)
    if proposal["conflicts"]:
        raise ValueError("Conflitos preservados: " + ", ".join(proposal["conflicts"]))
    if not apply:
        return proposal
    root = root_path(root)
    root.mkdir(parents=True, exist_ok=True)
    created = []
    with lock(root):
        proposal = plan(root, systems)
        if proposal["conflicts"]:
            raise ValueError("Plano mudou; configuração preservada")
        for name in proposal["directories"]:
            safe_path(root, name).mkdir(parents=True, exist_ok=True)
        # Marker last: interruptions can resume if no payload was imported.
        for name, value in proposal["configs"].items():
            if write_new(safe_path(root, name), value):
                created.append(name)
        receipt(root, "initialize", {"created": created, "systems": systems})
    return {"status": "initialized" if created else "already_initialized", "root": str(root), "created": created}


def receipt(root, action, details):
    folder = safe_path(root, "outputs/workspace-operations")
    folder.mkdir(parents=True, exist_ok=True)
    ident = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    write_new(folder / f"{ident}-{action}.json", {"action": action, "details": details, "utc": ident})


def doctor(root):
    root = root_path(root)
    essentials = {"python": sys.version_info >= (3, 10),
                  "linux": platform.system() == "Linux", "x86_64": platform.machine() in {"x86_64", "AMD64"}}
    optional = {name: shutil.which(name) is not None for name in ["7z", "chdman", "flatpak", "steam", "bwrap", "ffmpeg"]}
    try:
        import PIL
        pillow = True
    except ImportError:
        pillow = False
    probe = root
    while not probe.exists():
        probe = probe.parent
    return {"status": "ready_for_workspace" if all(essentials.values()) else "missing_prerequisites",
            "essentials": essentials, "optional_commands": optional, "pillow": pillow,
            "free_bytes": shutil.disk_usage(probe).free, "initialized": (root / MARKER).is_file(),
            "frontend_launch_tested": False, "gameplay_tested": False,
            "note": "Diagnóstico não instala pacotes do host nem altera Steam Input."}


def source_component(component, source_lock=None):
    data = read(source_lock or HERE / "config/components.json")
    item = data["components"][component]
    if not item.get("approved_for_download") or not item.get("license_review"):
        raise ValueError("Origem requer revisão explícita de aquisição/licença")
    if not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) or not isinstance(item.get("size_bytes"), int) or item["size_bytes"] <= 0:
        raise ValueError("Hash/tamanho pinados obrigatórios")
    target = PurePosixPath(item["target"])
    if item["kind"] == "media":
        if len(target.parts) != 4 or target.parts[0] != "media" or target.parts[2] not in read(HERE / "config/product.json")["media_categories"]:
            raise ValueError("Mídia deve ficar em media/<system>/<categoria>/<arquivo>")
        if not item.get("license_id") or not item.get("source_page"):
            raise ValueError("Mídia requer licença e proveniência")
        if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".mp4", ".webm", ".ogg", ".mp3", ".flac", ".txt", ".pdf"}:
            raise ValueError("Formato não é mídia externa suportada")
    elif item["kind"] != "frontend" or item["target"] != "frontend/ES-DE/ES-DE_x64_SteamDeck.AppImage":
        raise ValueError("Use emutool para outros runtimes; jogos/BIOS/chaves não são componentes")
    validate_url(item["url"], item["allowed_hosts"])
    return item


def validate_url(url, hosts):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise ValueError("Apenas HTTPS nos hosts explícitos do lock de origem")


class RestrictedRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(root, component, apply=False, source_lock=None):
    root = root_path(root)
    item = source_component(component, source_lock)
    target = safe_path(root, item["target"])
    if target.exists():
        if target.stat().st_size == item["size_bytes"] and digest(target) == item["sha256"]:
            return {"status": "already_verified", "component": component}
        raise ValueError("Destino existente divergente; preservado")
    if not apply:
        return {"status": "planned", "component": component, "source": item["source_page"], "target": item["target"], "size_bytes": item["size_bytes"]}
    if not (root / MARKER).is_file():
        raise ValueError("Inicialize a instalação antes do download")
    with lock(root):
        if target.exists():
            raise ValueError("Destino apareceu; repetir verificação sem sobrescrever")
        if shutil.disk_usage(root).free < item["size_bytes"] + 32 * 1024 * 1024:
            raise ValueError("Espaço livre insuficiente para download verificado")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".acquire-", dir=target.parent)
        try:
            opener = build_opener(RestrictedRedirect(item["allowed_hosts"]))
            with os.fdopen(fd, "wb") as output, opener.open(Request(item["url"], headers={"User-Agent": "EmulationWorkspace/0.1"}), timeout=30) as response:
                validate_url(response.geturl(), item["allowed_hosts"])
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > item["size_bytes"]:
                        raise ValueError("Download excedeu tamanho pinado")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if size != item["size_bytes"] or digest(temp) != item["sha256"]:
                raise ValueError("Download divergente; destino não publicado")
            if item["kind"] == "frontend":
                with open(temp, "rb") as stream:
                    if stream.read(4) != b"\x7fELF":
                        raise ValueError("Frontend não é ELF")
            os.chmod(temp, int(item.get("mode", "0644"), 8))
            os.link(temp, target)  # Atomic publication with no overwrite.
            receipt(root, "acquire", {"component": component, "target": item["target"], "sha256": item["sha256"], "version": item["version"], "source_page": item["source_page"]})
        finally:
            Path(temp).unlink(missing_ok=True)
    return {"status": "downloaded_verified", "component": component, "target": item["target"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["plan", "init", "doctor", "fetch"]:
        p = commands.add_parser(name)
        p.add_argument("--root", type=Path, default=SOURCE_ROOT)
        if name in {"plan", "init"}:
            p.add_argument("--systems", nargs="*", default=[])
        if name in {"init", "fetch"}:
            p.add_argument("--apply", action="store_true")
        if name == "fetch":
            p.add_argument("--component", required=True)
            p.add_argument("--source-lock", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = doctor(args.root)
        elif args.command == "fetch":
            result = fetch(args.root, args.component, args.apply, args.source_lock)
        else:
            result = initialize(args.root, args.systems, args.command == "init" and args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
