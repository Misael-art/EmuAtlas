"""F01 regression: script-style and package-style imports share one module."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SNIPPET = r'''
import sys
sys.path.insert(0, {tools!r})
order = {order!r}
if order == "script-first":
    import romtool as a
    import runtime_install as ri_a
    from tools.roms import romtool as b
    from tools.roms import runtime_install as ri_b
else:
    from tools.roms import romtool as b
    from tools.roms import runtime_install as ri_b
    import romtool as a
    import runtime_install as ri_a
assert a is b, "two romtool instances"
assert ri_a.romtool is a and ri_b.romtool is a, "runtime_install bound to another romtool"
import media_session
from tools.roms import media_session as ms
assert media_session is ms, "two media_session instances"
print("ok")
'''


class ModuleIdentityTests(unittest.TestCase):
    def run_order(self, order):
        code = SNIPPET.format(tools=str(ROOT / "tools/roms"), order=order)
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True,
                                text=True, timeout=120, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_script_import_first(self):
        self.run_order("script-first")

    def test_package_import_first(self):
        self.run_order("package-first")


if __name__ == "__main__":
    unittest.main()
