"""Private, tiny fixtures for relative Steam save links and interrupted exchanges."""
from argparse import Namespace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import romtool, steam_centralizer as steam


class Interruption(BaseException):
    pass


class SteamSaveLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "library with spaces"
        self.output = self.root / "outputs/state"
        self.output.mkdir(parents=True)
        self.operations = self.output / "operations"
        self.api = {
            "PROJECT_ROOT": self.root, "OUTPUT_ROOT": self.output, "OPS_ROOT": self.operations,
            "operation_dir": lambda ident: self.operations / ident,
            "atomic_json": romtool.atomic_json, "sync_directory_chain": romtool.sync_directory_chain,
            "append_event": lambda *args, **kwargs: None, "now": steam.timestamp,
        }
        self.sources = []
        self.targets = []
        self.state = {"canonical_project_root": str(self.root), "steam_centralization": {"games": {}}}
        for index in range(3):
            source = self.root / f"roms/steam/Game {index}/DATA/save.dat"
            target = self.root / f"saves/steam/Game {index}/install/DATA/save.dat"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            target.write_bytes(f"own save {index}".encode())
            source.symlink_to(target)
            self.sources.append(source)
            self.targets.append(target)
            self.state["steam_centralization"]["games"][str(index + 1)] = {"saves": {"file": {
                "status": "centralized", "source_path": str(source), "canonical_path": str(target),
                "operation_id": "fixture-original-centralization"}}}
        self.write_state()
        running = patch.object(steam, "steam_processes_running", return_value=[])
        running.start()
        self.addCleanup(running.stop)

    def write_state(self):
        (self.output / "estado-final-verificacao.json").write_text(json.dumps(self.state))

    def plan(self, sources=None):
        args = Namespace(source=[str(p) for p in (sources or self.sources)])
        return steam.save_links_plan(args, self.api)

    def execute(self, plan, rollback=False):
        return steam.save_links_execute(Namespace(operation_id=plan["operation_id"], apply=True, rollback=True),
                                        self.api, rollback=rollback)

    def verify(self, plan):
        return steam.save_links_verify(Namespace(operation_id=plan["operation_id"]), self.api)

    def stage(self, plan, index=1):
        return self.operations / plan["operation_id"] / "links" / f"{index:04d}.link"

    def test_cli_parser_requires_explicit_sources_and_confirmation(self):
        parser = romtool.build_parser()
        args = parser.parse_args(["steam", "saves", "links", "plan", "--source", str(self.sources[0])])
        self.assertEqual(args.steam_save_links_command, "plan")
        self.assertEqual(args.source, [str(self.sources[0])])
        plan = self.plan()
        with self.assertRaises(ValueError):
            steam.save_links_execute(Namespace(operation_id=plan["operation_id"], apply=False), self.api)

    def test_apply_verify_repeat_and_rollback_preserve_all_payloads_and_link_inodes(self):
        snapshots = [p.stat() for p in self.targets]
        links = [steam._save_link_snapshot(p) for p in self.sources]
        plan = self.plan()
        self.assertEqual(len(plan["items"]), 3)
        self.assertEqual(self.execute(plan)["status"], "completed_verified")
        self.execute(plan)
        self.verify(plan)
        for index, (source, target) in enumerate(zip(self.sources, self.targets), 1):
            self.assertFalse(Path(os.readlink(source)).is_absolute())
            self.assertEqual(source.resolve(), target)
            self.assertEqual(steam._save_link_snapshot(self.stage(plan, index)), links[index - 1])
            self.assertEqual(target.stat(), snapshots[index - 1])
        self.assertEqual(self.execute(plan, rollback=True)["status"], "rolled_back_verified")
        self.execute(plan, rollback=True)
        self.assertEqual([steam._save_link_snapshot(p) for p in self.sources], links)
        self.assertEqual([p.stat() for p in self.targets], snapshots)

    def test_link_only_rollback_preserves_later_atomic_save_replacement(self):
        plan = self.plan()
        self.execute(plan)
        replacement = self.targets[0].with_name("save-next")
        replacement.write_bytes(b"new gameplay data")
        replacement.replace(self.targets[0])
        self.verify(plan)
        self.execute(plan, rollback=True)
        self.assertEqual(self.sources[0].read_bytes(), b"new gameplay data")

    def test_cut_after_exchange_resumes_apply_and_rollback(self):
        plan = self.plan()
        exchange = steam._save_links_exchange
        def cut(left, right):
            exchange(left, right)
            raise Interruption()
        with patch.object(steam, "_save_links_exchange", side_effect=cut):
            with self.assertRaises(Interruption):
                self.execute(plan)
        self.execute(plan)
        with patch.object(steam, "_save_links_exchange", side_effect=cut):
            with self.assertRaises(Interruption):
                self.execute(plan, rollback=True)
        self.execute(plan, rollback=True)
        for source, target in zip(self.sources, self.targets):
            self.assertEqual(os.readlink(source), str(target))

    def test_cut_after_staging_link_creation_resumes(self):
        plan = self.plan()
        symlink = os.symlink
        def cut(target, link, **kwargs):
            symlink(target, link, **kwargs)
            raise Interruption()
        with patch.object(steam.os, "symlink", side_effect=cut):
            with self.assertRaises(Interruption):
                self.execute(plan)
        self.execute(plan)
        self.verify(plan)

    def test_directory_sync_failure_after_exchange_resumes(self):
        plan = self.plan()
        original = self.api["sync_directory_chain"]
        def cut(path):
            original(path)
            if path == self.sources[0].parent:
                raise OSError("own fixture sync interruption")
        self.api["sync_directory_chain"] = cut
        with self.assertRaises(OSError):
            self.execute(plan)
        self.api["sync_directory_chain"] = original
        self.execute(plan)
        self.verify(plan)

    def test_complete_preflight_preserves_first_link_if_last_link_changed(self):
        plan = self.plan()
        self.sources[-1].unlink()
        self.sources[-1].write_bytes(b"foreign file")
        with self.assertRaises(ValueError):
            self.execute(plan)
        self.assertEqual(os.readlink(self.sources[0]), str(self.targets[0]))
        self.assertEqual(self.sources[-1].read_bytes(), b"foreign file")

    def test_foreign_file_racing_exchange_is_retained(self):
        plan = self.plan()
        exchange = steam._save_links_exchange
        preserved = self.sources[0].with_name("original-link-retained")
        def race(left, right):
            left.rename(preserved)
            left.write_bytes(b"foreign save content")
            exchange(left, right)
        with patch.object(steam, "_save_links_exchange", side_effect=race):
            with self.assertRaises(ValueError):
                self.execute(plan)
        self.assertEqual(self.stage(plan).read_bytes(), b"foreign save content")
        self.assertTrue(preserved.is_symlink())
        self.assertEqual(self.targets[0].read_bytes(), b"own save 0")

    def test_relocation_verifies_registry_and_blocks_restoring_old_absolute_links(self):
        plan = self.plan()
        self.execute(plan)
        old = self.root
        self.root = old.with_name("relocated library")
        old.rename(self.root)
        self.output = self.root / "outputs/state"
        self.operations = self.output / "operations"
        self.api.update(PROJECT_ROOT=self.root, OUTPUT_ROOT=self.output, OPS_ROOT=self.operations)
        self.verify(plan)
        with patch.multiple(romtool, PROJECT_ROOT=self.root, OUTPUT_ROOT=self.output):
            source = self.root / self.sources[0].relative_to(old)
            target = self.root / self.targets[0].relative_to(old)
            self.assertTrue(romtool.steam_save_link_is_registered(source, target, "1"))
            self.assertTrue(romtool.steam_game_has_centralized_saves("1", source.parents[1]))
        with self.assertRaisesRegex(ValueError, "raiz original"):
            self.execute(plan, rollback=True)

    def test_already_relative_selection_is_noop(self):
        source, target = self.sources[0], self.targets[0]
        source.unlink()
        source.symlink_to(os.path.relpath(target, source.parent))
        before = source.lstat()
        plan = self.plan([source])
        self.execute(plan)
        self.verify(plan)
        self.execute(plan, rollback=True)
        self.assertEqual(source.lstat(), before)
        self.assertFalse(os.path.lexists(self.stage(plan)))

    def test_unregistered_external_duplicate_and_ancestor_links_are_rejected(self):
        with self.assertRaises(ValueError):
            self.plan([self.sources[0], self.sources[0]])
        unregistered = self.sources[0].with_name("unregistered")
        unregistered.symlink_to(self.targets[0])
        with self.assertRaises(ValueError):
            self.plan([unregistered])
        parent = self.sources[0].parent
        moved = parent.with_name("DATA-moved")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "simbólico"):
            self.plan([self.sources[0]])

    def test_steam_running_blocks_changes(self):
        plan = self.plan()
        with patch.object(steam, "steam_processes_running", return_value=[{"pid": 123}]):
            with self.assertRaises(RuntimeError):
                self.execute(plan)
        self.assertEqual(os.readlink(self.sources[0]), str(self.targets[0]))

    def test_new_centralization_and_legacy_links_share_verifier(self):
        source, target = self.sources[0], self.targets[0]
        self.assertTrue(steam.save_link_matches(source, target, self.root))
        source.unlink()
        source.symlink_to(steam.save_link_target(source, target, self.root))
        self.assertTrue(steam.save_link_matches(source, target, self.root))
        external = self.root.parent / "host-user-save"
        self.assertEqual(steam.save_link_target(external, target, self.root), str(target))

    def test_normal_centralization_creates_internal_relative_link(self):
        source, target = self.sources[0], self.targets[0]
        source.unlink()
        target.rename(source)
        before = romtool.steam_payload_snapshot(source)
        action = {"source": str(source), "destination": str(target), "before": before,
                  "precondition": before, "steam": {"appid": "1", "source_kind": "own-fixture"}}
        with patch.multiple(romtool, PROJECT_ROOT=self.root, append_event=lambda *args, **kwargs: None), \
                patch.object(steam, "validate_steam_save_action"), patch.object(steam, "probe_symlink_support"):
            romtool.move_steam_save(action, "own-fixture")
            self.assertFalse(Path(os.readlink(source)).is_absolute())
            self.assertEqual(source.read_bytes(), b"own save 0")
            romtool.restore_steam_save(action, "own-fixture")
        self.assertFalse(source.is_symlink())
        self.assertEqual(source.read_bytes(), b"own save 0")

    def test_original_centralization_rollback_keeps_changed_payload_guard(self):
        source, target = self.sources[0], self.targets[0]
        source.unlink()
        source.symlink_to(steam.save_link_target(source, target, self.root))
        action = {"source": str(source), "destination": str(target), "after": {"sha256": "0" * 64}}
        with patch.multiple(romtool, PROJECT_ROOT=self.root), patch.object(steam, "validate_steam_save_action"):
            with self.assertRaisesRegex(ValueError, "mudou desde a centralização"):
                romtool.restore_steam_save(action, "own-fixture")
        self.assertEqual(target.read_bytes(), b"own save 0")


if __name__ == "__main__":
    unittest.main()
