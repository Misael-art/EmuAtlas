#!/usr/bin/env python3
"""Diagnose and prepare the project-local ES-DE installation and display target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
ROM_ROOT = PROJECT_ROOT / "roms"
APP_IMAGE = SCRIPT_DIR / "ES-DE_x64_SteamDeck.AppImage"
APP_URL = "https://gitlab.com/es-de/emulationstation-de/-/package_files/357718506/download"
APP_SIZE = 124_827_104
APP_SHA256 = "25c99841f14bc77272332dfc6bff6b219d3e77734380afbabb72a729f44161dc"
APP_MD5_ETAG = "df7f42c46a8109256cfbb5941783e896"
APP_VERSION = "3.5.0 (r52)"
ROMTOOL = PROJECT_ROOT / "tools" / "roms" / "romtool.py"
sys.path.insert(0, str(ROMTOOL.parent))
from esde_media import appdata_root
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OUTPUT_RE = re.compile(r"^Output:\s+\d+\s+(\S+)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_appimage(path: Path) -> tuple[str, int, int]:
    if path.is_symlink():
        raise RuntimeError(f"A instalação não pode ser symlink: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise RuntimeError(f"Não consegui inspecionar o AppImage {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"O AppImage não é um arquivo regular: {path}")
    if info.st_size != APP_SIZE:
        raise RuntimeError(
            f"Tamanho inesperado do AppImage ({info.st_size} bytes; esperado {APP_SIZE}); "
            "preservei o arquivo e não vou substituí-lo automaticamente."
        )
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        raise RuntimeError("O arquivo instalado não é um executável ELF64 little-endian válido.")
    if int.from_bytes(header[18:20], "little") != 62:
        raise RuntimeError("O AppImage não é para a arquitetura x86-64 deste frontend.")
    digest = sha256_file(path)
    if digest != APP_SHA256:
        raise RuntimeError(
            f"SHA-256 inesperado no AppImage ({digest}); esperado {APP_SHA256}. "
            "O arquivo foi preservado para revisão."
        )
    return digest, info.st_size, stat.S_IMODE(info.st_mode)


def record_environment_change(change_type: str, reason: str, details: dict[str, object]) -> str:
    if not ROMTOOL.is_file():
        raise RuntimeError(f"Não encontrei o registrador do projeto: {ROMTOOL}")
    command = [
        sys.executable,
        str(ROMTOOL),
        "record-change",
        "--change-type",
        change_type,
        "--reason",
        reason,
        "--details-json",
        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            "Não consegui registrar a alteração no histórico do projeto: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        return str(json.loads(result.stdout).get("operation_id", "recorded"))
    except (json.JSONDecodeError, AttributeError):
        return "recorded"


def download_appimage() -> tuple[str, str]:
    if APP_IMAGE.is_symlink():
        raise RuntimeError(f"O caminho de instalação é um symlink; não vou substituí-lo: {APP_IMAGE}")
    if APP_IMAGE.exists():
        raise RuntimeError(
            f"Já existe um arquivo no destino, mas ele não passou na validação; preservei-o: {APP_IMAGE}"
        )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{APP_IMAGE.name}.", suffix=".download", dir=SCRIPT_DIR
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        request = Request(APP_URL, headers={"User-Agent": "EmulationProject-ESDE-Structurer/1.0"})
        with os.fdopen(fd, "wb") as stream, urlopen(request, timeout=60) as response:
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https":
                raise RuntimeError("O servidor de download redirecionou para uma URL que não usa HTTPS.")
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) != APP_SIZE:
                raise RuntimeError(
                    f"O servidor anunciou {declared_size} bytes; o pacote fixado tem {APP_SIZE}."
                )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > APP_SIZE:
                    raise RuntimeError("O download excedeu o tamanho do pacote fixado; removi o temporário.")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        actual_digest = digest.hexdigest()
        if size != APP_SIZE or actual_digest != APP_SHA256:
            raise RuntimeError(
                f"Download não corresponde ao pacote fixado (bytes={size}, SHA-256={actual_digest}); "
                "o temporário foi descartado."
            )
        with temporary.open("rb") as stream:
            header = stream.read(20)
        if (
            len(header) != 20
            or header[:4] != b"\x7fELF"
            or header[4] != 2
            or header[5] != 1
            or int.from_bytes(header[18:20], "little") != 62
        ):
            raise RuntimeError("O pacote baixado não é um AppImage ELF64 x86-64; descartei o temporário.")
        os.chmod(temporary, 0o755)
        try:
            os.link(temporary, APP_IMAGE)
        except FileExistsError:
            existing_digest, _, _ = verify_appimage(APP_IMAGE)
            if existing_digest != APP_SHA256:
                raise RuntimeError("Outro arquivo apareceu no destino durante o download; não o substituí.")
            temporary.unlink()
            return existing_digest, "already-installed"
        temporary.unlink()
        try:
            directory_fd = os.open(SCRIPT_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        try:
            operation_id = record_environment_change(
                "frontend-appimage-installed",
                "Install the missing pinned ES-DE Steam Deck AppImage in the project frontend directory.",
                {
                    "product": "ES-DE",
                    "installation_path": str(APP_IMAGE),
                    "source_url": APP_URL,
                    "version": APP_VERSION,
                    "size_bytes": size,
                    "sha256": actual_digest,
                    "gitlab_etag_md5": APP_MD5_ETAG,
                    "https_required": True,
                    "elf_architecture": "x86_64",
                    "installed_only_because_missing": True,
                },
            )
        except Exception:
            if APP_IMAGE.is_file() and not APP_IMAGE.is_symlink() and sha256_file(APP_IMAGE) == APP_SHA256:
                APP_IMAGE.unlink()
            raise
        return actual_digest, operation_id
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_appimage() -> None:
    if not APP_IMAGE.exists() and not APP_IMAGE.is_symlink():
        digest, operation_id = download_appimage()
        if operation_id == "already-installed":
            print("[OK] Outra execução instalou o mesmo AppImage durante o diagnóstico; nenhuma cópia criada.")
        else:
            print(
                f"[INSTALADO] ES-DE {APP_VERSION} baixado do GitLab oficial e validado por SHA-256 "
                f"({digest}); registro {operation_id}."
            )
        return
    digest, size, mode = verify_appimage(APP_IMAGE)
    if mode & 0o111 == 0:
        os.chmod(APP_IMAGE, 0o755)
        try:
            operation_id = record_environment_change(
                "frontend-appimage-permission-repaired",
                "Restore the executable bit on the verified project-local ES-DE AppImage.",
                {
                    "product": "ES-DE",
                    "installation_path": str(APP_IMAGE),
                    "sha256": digest,
                    "size_bytes": size,
                    "previous_mode": oct(mode),
                    "new_mode": "0755",
                },
            )
        except Exception:
            os.chmod(APP_IMAGE, mode)
            raise
        print(f"[CORRIGIDO] Permissão executável restaurada; registro {operation_id}.")
    print(
        f"[OK] ES-DE {APP_VERSION} instalado em {APP_IMAGE}; "
        f"{size} bytes; SHA-256 {digest}."
    )


def kscreen_outputs() -> list[dict[str, object]]:
    if not shutil.which("kscreen-doctor"):
        return []
    try:
        result = subprocess.run(
            ["kscreen-doctor", "-o"], text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    outputs: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw_line in (result.stdout + "\n" + result.stderr).splitlines():
        line = ANSI_RE.sub("", raw_line).strip()
        match = OUTPUT_RE.match(line)
        if match:
            if current and current.get("enabled") and current.get("connected"):
                outputs.append(current)
            current = {"name": match.group(1), "enabled": False, "connected": False}
            continue
        if current is None:
            continue
        if line == "enabled":
            current["enabled"] = True
        elif line == "connected":
            current["connected"] = True
        elif line.startswith("priority "):
            try:
                current["priority"] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    if current and current.get("enabled") and current.get("connected"):
        outputs.append(current)
    return outputs


def active_output_name() -> str | None:
    for executable in ("qdbus6", "qdbus"):
        if not shutil.which(executable):
            continue
        try:
            result = subprocess.run(
                [executable, "org.kde.KWin", "/KWin", "org.kde.KWin.activeOutputName"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        value = result.stdout.strip().splitlines()
        if result.returncode == 0 and value and re.fullmatch(r"[A-Za-z0-9_.:-]+", value[-1]):
            return value[-1]
    return None


def xrandr_display_index(output_name: str | None) -> int | None:
    if not shutil.which("xrandr"):
        return None
    try:
        result = subprocess.run(
            ["xrandr", "--listmonitors"], text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+):.*\s(\S+)\s*$", line)
        if match and output_name and match.group(2) == output_name:
            return int(match.group(1)) + 1
    return None


def detect_display() -> tuple[int, str]:
    override = os.environ.get("ESDE_DISPLAY_INDEX", "").strip()
    if override:
        if not re.fullmatch(r"[1-4]", override):
            raise RuntimeError("ESDE_DISPLAY_INDEX deve ser um número entre 1 e 4.")
        return int(override), "override ESDE_DISPLAY_INDEX"

    active_name = active_output_name()
    outputs = kscreen_outputs()
    if active_name:
        for output in outputs:
            if output.get("name") == active_name:
                priority = output.get("priority")
                if isinstance(priority, int) and 1 <= priority <= 4:
                    return priority, f"saída ativa do KWin {active_name}, prioridade {priority}"
        fallback_index = xrandr_display_index(active_name)
        if fallback_index is not None and 1 <= fallback_index <= 4:
            return fallback_index, f"saída ativa do KWin {active_name}, índice XRandR {fallback_index}"

    primary = [
        output
        for output in outputs
        if isinstance(output.get("priority"), int) and output.get("priority") == 1
    ]
    if primary:
        return 1, f"tela primária {primary[0].get('name')} (KWin ativo indisponível)"
    return 1, "fallback para a tela primária do ES-DE"


def rom_root_summary() -> tuple[int, int]:
    if ROM_ROOT.is_symlink() or not ROM_ROOT.is_dir():
        return 0, 0
    systems = 0
    populated = 0
    for child in ROM_ROOT.iterdir():
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        systems += 1
        try:
            next(child.iterdir())
            populated += 1
        except StopIteration:
            pass
    return systems, populated


def saved_display_setting() -> str:
    raw_root = str(appdata_root(PROJECT_ROOT, os.environ.get("ESDE_APPDATA_DIR")))
    app_data = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(raw_root))))
    settings = app_data / "settings" / "es_settings.xml"
    if settings.is_symlink():
        return "não lido (arquivo de configurações é symlink)"
    if not settings.is_file():
        return "ausente; o ES-DE aplicará o índice recebido pelo launcher"
    try:
        text = settings.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "não legível; o launcher continuará passando o índice detectado"
    values = re.findall(
        r'<int\s+name="DisplayIndex"\s+value="([^"]*)"\s*/>', text
    )
    if len(values) > 1:
        return "duplicado; o launcher passará o índice ativo pela linha de comando"
    if not values:
        return "ausente; o ES-DE aplicará o índice recebido pelo launcher"
    return values[0]


def diagnose() -> int:
    print(f"[ES-DE] Raiz oficial do frontend: {SCRIPT_DIR}")
    print(f"[ES-DE] AppImage esperado: {APP_IMAGE}")
    if APP_IMAGE.exists() and not APP_IMAGE.is_symlink():
        digest, size, mode = verify_appimage(APP_IMAGE)
        executable = bool(mode & 0o111)
        print(
            f"[OK] Instalação {APP_VERSION}: {size} bytes; SHA-256 {digest}; "
            f"executável={str(executable).lower()}."
        )
        if not executable:
            print("[AÇÃO] O launcher restaurará a permissão executável antes de iniciar.")
    elif APP_IMAGE.is_symlink():
        raise RuntimeError(f"O caminho do AppImage é symlink e exige revisão: {APP_IMAGE}")
    else:
        print(f"[AÇÃO] AppImage ausente; o launcher fará download verificado de {APP_URL}.")
    systems, populated = rom_root_summary()
    print(
        f"[ES-DE] Biblioteca existente: {ROM_ROOT}; {systems} pastas de plataforma, "
        f"{populated} não vazias. Nenhum payload é examinado ou alterado."
    )
    display, reason = detect_display()
    saved_display = saved_display_setting()
    print(f"[ES-DE] Tela selecionada no próximo início: índice {display} ({reason}).")
    print(f"[ES-DE] DisplayIndex salvo atualmente: {saved_display}; o launcher passará índice {display}.")
    print("[ES-DE] `--display N` ou ESDE_DISPLAY_INDEX=1..4 substituem a detecção automática.")
    print("[ES-DE] O launcher também valida ROMDirectory e preserva as pastas atuais de ROMs/mídia.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--diagnose", action="store_true", help="inspeciona estrutura e tela sem escrever")
    modes.add_argument("--ensure-installation", action="store_true", help="valida ou baixa o AppImage fixado")
    modes.add_argument("--display-index", action="store_true", help="imprime o índice de tela detectado")
    modes.add_argument("--check-theme", action="store_true", help="planeja projeção Linear sem copiar imagens/fontes")
    modes.add_argument("--prepare-theme", action="store_true", help="aplica projeção reversível do tema Linear e artes compartilhadas")
    modes.add_argument("--rollback-theme", metavar="OPERATION_ID", help="reverte projeção inalterada, preservando edições pessoais")
    args = parser.parse_args()
    try:
        if args.diagnose:
            return diagnose()
        if args.check_theme or args.prepare_theme or args.rollback_theme:
            import esde_theme
            if args.rollback_theme:
                result = esde_theme.rollback(PROJECT_ROOT, args.rollback_theme)
            else:
                verify_appimage(APP_IMAGE)
                result = esde_theme.prepare(PROJECT_ROOT, apply=args.prepare_theme, expected_sha256=APP_SHA256)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.ensure_installation:
            ensure_appimage()
            return 0
        if args.display_index:
            display, reason = detect_display()
            print(display)
            print(f"[ES-DE] Tela índice {display}: {reason}.", file=sys.stderr)
            return 0
    except Exception as exc:
        print(f"[ES-DE] ERRO DE ESTRUTURAÇÃO: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
