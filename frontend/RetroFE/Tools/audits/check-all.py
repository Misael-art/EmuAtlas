#!/usr/bin/env python3
"""Run every read-only RetroFE gate and report one summary.

Only commands documented as read-only are executed (`--check`, `verify`, static audits
and unit tests). Nothing is materialized, regenerated or launched; a failing gate keeps
its own command so it can be rerun and fixed through its owning tool. Session/runtime
validators that drive a real RetroFE instance are intentionally left out.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "Tools"
# Generated from data that changes with normal use; the launcher rebuilds them on every start.
DYNAMIC_NAVIGATION = {"collections/Recentes/menu.txt", "Manifests/aeon-nox-navigation-state.json"}

GATES = [
    ("host-profile", ["materializers/materialize-host-profile.py", "--check"]),
    ("platform-catalog", ["materializers/materialize-platform-catalog.py", "--check"]),
    ("pilot-profile", ["materializers/materialize-profile.py", "--check"]),
    ("aeon-nox-layouts", ["materializers/materialize-aeon-nox-layouts.py", "--check", "--quiet"]),
    ("aeon-nox-navigation", ["materializers/materialize-aeon-nox-navigation.py", "--check"]),
    ("aeon-nox-presentation", ["audits/validate-aeon-nox-presentation.py"]),
    ("platform-coverage", ["audits/platform-coverage.py", "--check"]),
    ("layout-references", ["audits/layout-references.py", "--check"]),
    ("asset-library", ["materializers/organize-asset-library.py", "verify"]),
    ("launcher-unit-tests", ["-m", "unittest", "test_input_session", "test_window_placement", "test_desktop_environment",
                             "test_media_tasks"]),
]


def navigation_result(stdout):
    """Accept a plan whose only differences are the use-derived Recentes menu."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    pending = sorted(c["path"] for c in data.get("changes", []) if c.get("action") != "unchanged")
    if data.get("conflicts"):
        return "fail", f"conflitos: {len(data['conflicts'])}"
    if not pending:
        return "pass", "sem diferenças"
    if set(pending) <= DYNAMIC_NAVIGATION:
        return "pass", "só Recentes (lastplayed) difere; reconstruído pelo launcher na próxima abertura"
    return "fail", "diferenças: " + ", ".join(pending[:5])


def run_gate(name, argv, timeout):
    cwd = TOOLS / "launchers/retrofe" if argv[0] == "-m" else ROOT
    command = [sys.executable, *(argv if argv[0] == "-m" else [str(TOOLS / argv[0]), *argv[1:]])]
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(gate=name, status="fail", detail=f"tempo esgotado ({timeout}s)", seconds=timeout)
    output = (result.stdout + result.stderr).strip()
    status, detail = ("pass" if result.returncode == 0 else "fail"), ""
    if name == "aeon-nox-navigation":
        status, detail = navigation_result(result.stdout) or (status, "")
    if not detail:
        try:
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                detail = f"JSON: status={data.get('status', '-')}, erros={len(data.get('errors') or [])}"
        except ValueError:
            pass
    if not detail:
        lines = [line for line in output.splitlines() if line.strip()]
        flagged = [line for line in lines if "[FALTA]" in line or "Error" in line or "FAILED" in line]
        detail = (flagged or lines or [""])[-1].strip()[:240]
    return dict(gate=name, status=status, detail=detail, seconds=round(time.monotonic() - started, 1),
                command=" ".join(["python3", *([str(Path(a).relative_to(ROOT)) if a.startswith(str(ROOT)) else a
                                               for a in command[1:]])]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", choices=[name for name, _ in GATES], help="Executa só este gate (repetível)")
    parser.add_argument("--json", action="store_true", help="Saída JSON")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    results = [run_gate(name, argv, args.timeout) for name, argv in GATES if not args.only or name in args.only]
    failed = [r for r in results if r["status"] != "pass"]
    if args.json:
        print(json.dumps(dict(status="passed" if not failed else "failed", gates=results), ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"[{'OK' if r['status'] == 'pass' else 'FALHA'}] {r['gate']:<22} {r['seconds']:>6}s  {r['detail']}")
            if r["status"] != "pass":
                print(f"         repetir: {r['command']}")
        print(f"\n{len(results) - len(failed)}/{len(results)} gates passaram.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
