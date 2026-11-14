"""Portable emulator installs: pinned sources, atomic entry replacement, manual large downloads, rollback."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools.roms import romtool, runtime_install as ri

APPIMAGE = b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\x00" * 64 + b"#!/bin/sh\necho portable-ok\n"


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for attr, value in (("PROJECT_ROOT", self.root), ("OUTPUT_ROOT", self.root / "outputs"),
                            ("OPS_ROOT", self.root / "outputs/operations")):
            patcher = patch.object(romtool, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        profile = self.root / "frontend/RetroFE/Config/profiles/host-linux.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"collections": {
            "Fixture Portable": {"command": "../../emulator/linux/emu/emu.AppImage"},
            "Fixture Flatpak": {"app_id": "org.example.Emu"}}}))
        self.config = self.root / "tools/roms/config/portable-runtimes.json"
        self.config.parent.mkdir(parents=True)
        self.source = self.root / "download/emu.AppImage"
        self.source.parent.mkdir()
        self.source.write_bytes(APPIMAGE)
        self.write_recipe()
        for name, value in (("host_flatpaks", lambda: ({}, True)),):
            patcher = patch.object(ri, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        which = patch.object(ri.shutil, "which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def write_recipe(self, payload=APPIMAGE, **overrides):
        recipe = dict(display_name="Emu", version="1.0", source_url="https://github.com/example/emu/releases/download/v1/emu.AppImage",
                      source_size_bytes=len(payload), source_sha256=hashlib.sha256(payload).hexdigest(), format="appimage",
                      entry="emu.AppImage", profile_command="../../emulator/linux/emu/emu.AppImage",
                      flatpak_app_id="org.example.Emu")
        recipe.update(overrides)
        self.config.write_text(json.dumps({"manual_download_limit_bytes": 70000000,
            "allowed_download_hosts": ["github.com"], "runtimes": {"emu": recipe}, "not_installable": {}}))

    def fake_download(self, payload):
        def download(url, destination, size, hosts):
            destination.write_bytes(payload)
        return patch.object(ri, "_download", side_effect=download)

    def entry(self):
        return self.root / "emulator/linux/emu/emu.AppImage"

    def test_install_downloads_verifies_records_and_reports_profile_usage(self):
        with self.fake_download(APPIMAGE):
            result = ri.install_runtime(self.root, "emu", True)
        self.assertEqual(result["status"], "installed_verified")
        self.assertEqual(self.entry().read_bytes(), APPIMAGE)
        self.assertTrue(self.entry().stat().st_mode & 0o111)
        self.assertEqual({u["launch"] for u in result["used_by"]}, {"portable", "flatpak"})
        status = ri.runtime_status(self.root)["runtimes"][0]
        self.assertEqual((status["status"], status["install_operation_id"]), ("installed_recorded", result["operation_id"]))
        self.assertFalse(list((self.root / "tools/roms/runtimes").glob(".staging-*")))

    def test_explicit_index_deferral_keeps_install_receipt_and_complete_journal(self):
        with self.fake_download(APPIMAGE), patch.object(romtool, 'rebuild_history_index') as rebuild:
            result = ri.install_runtime(self.root, 'emu', True, defer_history_index=True)
        rebuild.assert_not_called()
        self.assertEqual(result['status'], 'installed_verified')
        manifest_path = romtool.operation_dir(result['operation_id']) / 'manifest.json'
        self.assertEqual(json.loads(manifest_path.read_text())['history_index_refresh'], 'deferred')
        self.assertEqual(self.entry().read_bytes(), APPIMAGE)
        events = [json.loads(line) for line in (manifest_path.parent / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'portable_runtime_installed_verified')

    def test_hash_mismatch_installs_nothing(self):
        with self.fake_download(APPIMAGE[:-1] + b"X"):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                ri.install_runtime(self.root, "emu", True)
        self.assertFalse(self.entry().exists())
        self.assertFalse((self.root / "outputs/operations").exists())

    def test_verified_reinstall_does_not_download_or_probe_again(self):
        with self.fake_download(APPIMAGE):first=ri.install_runtime(self.root,'emu',True)
        with patch.object(ri,'_download',side_effect=AssertionError('network must not run')), \
             patch.object(ri,'_probe',side_effect=AssertionError('probe must not run')):
            result=ri.install_runtime(self.root,'emu',True)
        self.assertTrue(result['reused_without_download'])
        self.assertEqual(result['operation_id'],first['operation_id'])

    def test_host_installer_static_mode_never_runs_a_gui_probe(self):
        self.write_recipe(probe_args=["--version"])
        with self.fake_download(APPIMAGE), patch.object(ri, "_probe", side_effect=AssertionError("probe must not start")):
            result = ri.install_runtime(self.root, "emu", True, run_declared_probe=False)
        self.assertEqual(result["status"], "installed_verified")
        self.assertEqual(self.entry().read_bytes(), APPIMAGE)

    def test_requires_explicit_apply_and_valid_appimage(self):
        with self.assertRaises(ValueError):
            ri.install_runtime(self.root, "emu", False)
        plain = b"not an elf"
        self.write_recipe(plain)
        with self.fake_download(plain), self.assertRaisesRegex(ValueError, "AppImage"):
            ri.install_runtime(self.root, "emu", True)

    def test_large_download_is_manual_then_incoming_file_installs(self):
        self.write_recipe(source_size_bytes=len(APPIMAGE))
        config = json.loads(self.config.read_text())
        config["manual_download_limit_bytes"] = 10
        self.config.write_text(json.dumps(config))
        with patch.object(ri, "_download") as download, self.assertRaises(ri.ManualDownloadRequired):
            ri.install_runtime(self.root, "emu", True)
        download.assert_not_called()
        row = ri.runtime_status(self.root)["runtimes"][0]
        self.assertTrue(row["manual_download"])
        incoming = self.root / row["incoming_path"]
        incoming.parent.mkdir(parents=True)
        incoming.write_bytes(APPIMAGE)
        self.assertTrue(ri.runtime_status(self.root)["runtimes"][0]["incoming_ready"])
        result = ri.install_runtime(self.root, "emu", True)
        self.assertEqual((result["status"], result["source_origin"]), ("installed_verified", "local_file"))
        self.assertFalse(incoming.exists())

    def test_reinstall_same_build_is_noop_and_new_build_keeps_previous_for_rollback(self):
        with self.fake_download(APPIMAGE):
            first = ri.install_runtime(self.root, "emu", True)
            again = ri.install_runtime(self.root, "emu", True)
        self.assertEqual(again["status"], "already_installed_verified")
        self.assertEqual(ri.runtime_status(self.root)["runtimes"][0]["install_operation_id"], again["operation_id"])
        newer = APPIMAGE + b"v2\n"
        self.write_recipe(newer, version="2.0")
        with self.fake_download(newer):
            second = ri.install_runtime(self.root, "emu", True)
        self.assertEqual(self.entry().read_bytes(), newer)
        kept = romtool.operation_dir(second["operation_id"]) / "rollback/emu.AppImage"
        self.assertEqual(kept.read_bytes(), APPIMAGE)
        result = ri.rollback_install(self.root, second["operation_id"], True)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.entry().read_bytes(), APPIMAGE)
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        with self.assertRaises(ValueError):
            ri.rollback_install(self.root, first["operation_id"], False)

    def test_rollback_refuses_changed_entry(self):
        with self.fake_download(APPIMAGE):
            result = ri.install_runtime(self.root, "emu", True)
        self.entry().write_bytes(APPIMAGE + b"edited")
        with self.assertRaisesRegex(ValueError, "mudou"):
            ri.rollback_install(self.root, result["operation_id"], True)

    def test_zip_member_extraction_rejects_traversal(self):
        good = io.BytesIO()
        with zipfile.ZipFile(good, "w") as bundle:
            bundle.writestr("readme.txt", "x")
            bundle.writestr("dist/Emu.AppImage", APPIMAGE)
        payload = good.getvalue()
        self.write_recipe(payload, format="zip_member", member_suffix=".AppImage")
        with self.fake_download(payload):
            self.assertEqual(ri.install_runtime(self.root, "emu", True)["status"], "installed_verified")
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as bundle:
            bundle.writestr("../Emu.AppImage", APPIMAGE)
        self.write_recipe(bad.getvalue(), format="zip_member", member_suffix=".AppImage", version="2")
        with self.fake_download(bad.getvalue()), self.assertRaisesRegex(ValueError, "inseguro"):
            ri.install_runtime(self.root, "emu", True)

    def test_download_refuses_unofficial_hosts(self):
        with self.assertRaisesRegex(ValueError, "hosts oficiais"):
            ri._download("https://example.com/emu.AppImage", self.root / "x", 10, {"github.com"})
        with self.assertRaisesRegex(ValueError, "hosts oficiais"):
            ri._download("http://github.com/emu.AppImage", self.root / "x", 10, {"github.com"})

    def test_host_flatpak_or_path_blocks_duplicate_install(self):
        flatpak = lambda: ({"org.example.Emu": "0.9"}, True)
        with self.fake_download(APPIMAGE) as download, self.assertRaisesRegex(ri.HostDuplicate, "Flatpak org.example.Emu 0.9"):
            ri.install_runtime(self.root, "emu", True, flatpak_query=flatpak)
        download.assert_not_called()
        self.assertFalse(self.entry().exists())
        row = ri.runtime_status(self.root, flatpak_query=flatpak)["runtimes"][0]
        self.assertEqual((row["decision"], row["host_sources"][0]["app_id"]), ("use_host_configured", "org.example.Emu"))
        self.write_recipe(host_commands=["emu-bin"])
        with patch.object(ri.shutil, "which", return_value="/usr/bin/emu-bin"):
            with self.assertRaisesRegex(ri.HostDuplicate, "/usr/bin/emu-bin"):
                ri.install_runtime(self.root, "emu", True)
            with self.fake_download(APPIMAGE):
                forced = ri.install_runtime(self.root, "emu", True, allow_host_duplicate=True)
        self.assertEqual(forced["status"], "installed_verified")

    def test_unreadable_flatpak_list_blocks_install_and_project_paths_are_not_host(self):
        with patch.object(ri.shutil, "which", side_effect=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None):
            with self.assertRaisesRegex(RuntimeError, "Flatpaks"):
                ri.install_runtime(self.root, "emu", True, flatpak_query=lambda: ({}, False))
        recipe = {"host_commands": ["emu"]}
        inside = str(self.root / "emulator/linux/emu/emu")
        self.assertEqual(ri.scan_host(self.root, recipe, {}, which=lambda name: inside), [])
        self.assertEqual(ri.scan_host(self.root, recipe, {}, which=lambda name: "/opt/emu")[0]["path"], "/opt/emu")
        row = ri.runtime_status(self.root)["runtimes"][0]
        self.assertEqual(row["decision"], "install_available")

    def test_libretro_core_installs_into_shared_folder_and_host_core_blocks_duplicate(self):
        core = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + (3).to_bytes(2, "little") + b"\x00" * 40
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("demo_libretro.so", core)
        payload = archive.getvalue()
        self.write_recipe(payload, kind="libretro_core", format="zip_member", member="demo_libretro.so",
                          entry="demo_libretro.so", install_dir="retroarch-cores", host_core="demo_libretro.so",
                          flatpak_app_id=None, profile_command=None)
        profile = self.root / "frontend/RetroFE/Config/profiles/host-linux.json"
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        host_cores = Path(outside.name) / "host-cores"
        host_cores.mkdir()
        profile.write_text(json.dumps({"retroarch": {"core_root": str(host_cores)}, "collections": {
            "Demo": {"adapter": "retroarch", "core": "demo_libretro.so", "core_root": "../../emulator/linux/retroarch-cores"}}}))
        (host_cores / "demo_libretro.so").write_bytes(core)
        with self.fake_download(payload), self.assertRaisesRegex(ri.HostDuplicate, "host-cores"):
            ri.install_runtime(self.root, "emu", True)
        (host_cores / "demo_libretro.so").unlink()
        with self.fake_download(payload):
            result = ri.install_runtime(self.root, "emu", True)
        self.assertEqual(result["installation_path"], "emulator/linux/retroarch-cores/demo_libretro.so")
        self.assertEqual(result["used_by"], [{"collection": "Demo", "launch": "portable"}])
        self.write_recipe(APPIMAGE, kind="libretro_core", entry="other_libretro.so", install_dir="retroarch-cores", version="2")
        with self.fake_download(APPIMAGE), self.assertRaisesRegex(ValueError, "ELF compartilhada"):
            ri.install_runtime(self.root, "emu", True)

    def test_symlinked_install_dir_is_preserved(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "emulator/linux").mkdir(parents=True)
        (self.root / "emulator/linux/emu").symlink_to(elsewhere, target_is_directory=True)
        with self.fake_download(APPIMAGE), self.assertRaisesRegex(ValueError, "symlink"):
            ri.install_runtime(self.root, "emu", True)
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
