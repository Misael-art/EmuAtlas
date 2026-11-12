"""F07: games and maintenance share one reservation, held by the game process tree."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.roms import media_session, preview_games, romtool


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = {"OPS_ROOT": self.root / preview_games.MEDIA_WRITE_LOCK.parent}

    def spawn_holder(self, seconds="30"):
        fd = preview_games.acquire_game_reservation(self.root)
        try:
            child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"], pass_fds=(fd,))
        finally:
            os.close(fd)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait()))
        return child

    def test_second_game_and_maintenance_refused_while_child_holds(self):
        child = self.spawn_holder()
        with self.assertRaises(ValueError):
            preview_games.acquire_game_reservation(self.root)
        with self.assertRaises(media_session.MediaBusy):
            with media_session.write_lock(self.api):
                pass
        self.assertIsNone(child.poll())

    def test_reservation_released_when_child_exits_or_crashes(self):
        child = self.spawn_holder("0.1")
        child.wait(timeout=10)
        os.close(preview_games.acquire_game_reservation(self.root))
        crashed = self.spawn_holder()
        crashed.kill(); crashed.wait(timeout=10)
        with media_session.write_lock(self.api):
            pass

    def test_game_refused_while_maintenance_writes(self):
        with media_session.write_lock(self.api):
            with self.assertRaises(ValueError):
                preview_games.acquire_game_reservation(self.root)
        os.close(preview_games.acquire_game_reservation(self.root))

    def test_generic_apply_takes_the_shared_lock(self):
        child = self.spawn_holder()
        with patch.object(romtool, "OPS_ROOT", self.api["OPS_ROOT"]), \
                patch.object(romtool, "apply_operation", side_effect=AssertionError("must not run")):
            code = romtool.main(["apply", "--operation-id", "x", "--apply"])
        self.assertEqual(code, 2)
        self.assertIsNone(child.poll())

    def test_reentrant_lock_in_same_thread(self):
        with media_session.write_lock(self.api):
            with media_session.write_lock(self.api):
                pass


if __name__ == "__main__":
    unittest.main()
