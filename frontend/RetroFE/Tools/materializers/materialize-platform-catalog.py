#!/usr/bin/env python3
"""Build a portable platform catalog from ROM metadata and directory names."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "Config/profiles/host-linux.json").is_file() and (candidate / "Manifests").is_dir():
            return candidate
    raise RuntimeError("Não encontrei a raiz do RetroFE acima do materializador.")


ROOT = find_project_root()
PROJECT_ROOT = ROOT.parent.parent
PROFILE = json.loads((ROOT / "Config/profiles/host-linux.json").read_text(encoding="utf-8"))
CATALOG = ROOT / "Config/platform-catalog.json"
# roms/media is stray scraper output, not a platform directory.
NON_PLATFORM_DIRS = {"bios", ".steamzero", "media"}


def resolve_rom_root(value: str | None) -> Path:
    configured = value or os.environ.get("RETROFE_ROM_ROOT") or PROFILE["rom_root"]
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        header = re.match(r"^([^:\n]+):\s*$", line)
        if header:
            current = header.group(1).strip()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.strip())
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def metadata_source_for(directory: Path, existing_by_id: dict[str, dict[str, object]]) -> tuple[Path, str]:
    configured = existing_by_id.get(directory.name, {}).get("source_metadata")
    if isinstance(configured, str) and configured and configured != "systeminfo.txt":
        source = Path(configured).expanduser()
        if not source.is_absolute():
            source = PROJECT_ROOT / source
        if not source.is_file():
            raise RuntimeError(
                f"Metadata externa configurada para {directory.name} não existe: {source}"
            )
        return source, str(configured)
    local_source = directory / "systeminfo.txt"
    if local_source.is_file():
        return local_source, "systeminfo.txt"
    centralized_source = PROJECT_ROOT / "metadata" / directory.name / "platform" / "systeminfo.txt"
    if centralized_source.is_file():
        return centralized_source, str(centralized_source.relative_to(PROJECT_ROOT).as_posix())
    return local_source, "systeminfo.txt"


def collect_systems(rom_root: Path) -> list[dict[str, object]]:
    systems: list[dict[str, object]] = []
    if not rom_root.is_dir():
        raise RuntimeError(f"A raiz de ROMs não existe ou não é uma pasta: {rom_root}")

    existing_by_id: dict[str, dict[str, object]] = {}
    if CATALOG.is_file():
        saved_catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        existing_by_id = {
            str(record.get("id", "")): record
            for record in saved_catalog.get("systems", [])
            if record.get("id")
        }

    for directory in sorted(rom_root.iterdir(), key=lambda item: item.name.casefold()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        if directory.name.casefold() in NON_PLATFORM_DIRS:
            continue
        metadata_path, metadata_source = metadata_source_for(directory, existing_by_id)
        if not metadata_path.is_file():
            observed_extensions: set[str] = set()
            for base, directories, files in os.walk(directory, followlinks=False):
                current = Path(base)
                directories[:] = [name for name in directories if not (current / name).is_symlink()]
                for name in files:
                    path = current / name
                    if not path.is_symlink() and path.suffix:
                        observed_extensions.add(path.suffix.lstrip(".").casefold())
            systems.append(
                {
                    "id": directory.name,
                    "display_name": f"{directory.name} (sem systeminfo.txt)",
                    "kind": "unclassified",
                    "rom_subdir": directory.name,
                    "extensions": [],
                    "observed_file_extensions": sorted(observed_extensions),
                    "preferred_retroarch_cores": [],
                    "emulator_hints": [],
                    "source_metadata": None,
                }
            )
            continue

        metadata = metadata_path.read_text(encoding="utf-8", errors="replace")
        sections = parse_sections(metadata)
        display_name = sections.get("Full system name", "").splitlines()[0].strip()
        if not display_name:
            display_name = sections.get("System name", directory.name).splitlines()[0].strip()

        extensions = sorted(
            {extension.casefold() for extension in re.findall(r"\.([A-Za-z0-9]+)", sections.get("Supported file extensions", ""))}
        )
        cores = list(dict.fromkeys(re.findall(r"%CORE_RETROARCH%/([^\s%\"']+_libretro\.so)", metadata)))
        emulator_hints = sorted(
            set(re.findall(r"%EMULATOR_([A-Z0-9_-]+)%", metadata))
        )

        systems.append(
            {
                "id": directory.name,
                "display_name": display_name,
                "kind": "support_tools" if directory.name.casefold() == "emulators" else "platform",
                "rom_subdir": directory.name,
                "extensions": extensions,
                "observed_file_extensions": [],
                "preferred_retroarch_cores": cores,
                "emulator_hints": emulator_hints,
                "source_metadata": metadata_source,
            }
        )

    # Frontend integration is owned by the controller/frontend tool, not by
    # systeminfo.txt. Rebuilding metadata must retain that existing contract.
    for system in systems:
        previous = existing_by_id.get(str(system['id']), {})
        if 'frontends' in previous:
            system['frontends'] = previous['frontends']
    return systems


def serialize(rom_root: Path) -> str:
    document = {
        "schema_version": 1,
        "description": "Catálogo baseado em systeminfo.txt; metadados podem usar caminho relativo à raiz do projeto. Diretórios sem metadados permanecem marcados para revisão.",
        "rom_root": PROFILE["rom_root"],
        "bios_root": PROFILE["bios_root"],
        "host_profile": "Config/profiles/host-linux.json",
        "systems": collect_systems(rom_root),
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom-root", help="substitui RETROFE_ROM_ROOT e o caminho relativo do perfil")
    parser.add_argument(
        "--check",
        action="store_true",
        help="confere se o catálogo salvo corresponde às pastas e metadados atuais sem gravar",
    )
    args = parser.parse_args()

    generated = serialize(resolve_rom_root(args.rom_root))
    if args.check:
        if not CATALOG.is_file() or CATALOG.read_text(encoding="utf-8") != generated:
            print(f"[FALTA] Catálogo desatualizado: {CATALOG}", file=sys.stderr)
            return 1
        print(f"[OK] Catálogo de plataformas atualizado: {CATALOG}")
        return 0

    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(generated, encoding="utf-8")
    systems = json.loads(generated)["systems"]
    count = sum(1 for system in systems if system.get("kind") == "platform")
    print(f"[OK] {count} plataformas e {len(systems) - count} diretórios de suporte/revisão catalogados em {CATALOG}")
    print("[INFO] Apenas IDs, nomes, extensões e referências de configuração foram registrados; nenhuma mídia foi alterada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
