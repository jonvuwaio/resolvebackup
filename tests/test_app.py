import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import menubar as mb  # noqa: E402
import resolve_cloud_backup as rcb  # noqa: E402

# Never start or quit a real DaVinci Resolve from the tests; tests that need it pass fakes.
rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
_fake_prefs = __import__("tempfile").mkdtemp()  # and never edit Resolve's real preferences
rcb.resolve_prefs_dir = lambda: rcb.Path(_fake_prefs)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        cfg = {"backup_dir": str(self.d / "out"), "min_free_gb": 0, "dashboard": {"enabled": True, "port": 8790}}
        (self.d / "config.json").write_text(json.dumps(cfg))
        p = mock.patch.object(rcb, "app_dir", lambda: self.d)
        p.start()
        self.addCleanup(p.stop)
        # Never reach a real DaVinci Resolve (or its logs) that happens to be on the test machine.
        for target, value in (("connect_resolve", lambda cfg: None), ("resolve_log_dirs", lambda: [])):
            c = mock.patch.object(rcb, target, value)
            c.start()
            self.addCleanup(c.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_runner_starts_serves_and_stops_cleanly(self):
        r = rcb.ServiceRunner(self.d / "config.json")
        self.assertTrue(r.start())
        try:
            st, off, den = mb.ServiceClient(self.d).poll()
            self.assertEqual((off, den), (False, False))
            self.assertIn(st["state"], ("starting", "running", "waiting_for_resolve"))
        finally:
            r.stop()
        self.assertFalse((self.d / "service.json").exists())
        self.assertFalse(r.thread.is_alive())

    def test_second_runner_defers_to_the_first(self):
        a = rcb.ServiceRunner(self.d / "config.json")
        b = rcb.ServiceRunner(self.d / "config.json")
        self.assertTrue(a.start())
        try:
            self.assertFalse(b.start())
        finally:
            a.stop()
        c = rcb.ServiceRunner(self.d / "config.json")
        self.assertTrue(c.start())  # lock released by stop()
        c.stop()

    def test_resource_path_prefers_the_bundle(self):
        self.assertTrue(rcb.resource_path("dashboard.html").exists())
        with mock.patch.object(sys, "_MEIPASS", "/bundle", create=True):
            self.assertEqual(rcb.resource_path("dashboard.html"), Path("/bundle/dashboard.html"))


class LoginItemTests(unittest.TestCase):
    def test_enable_and_disable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(Path, "home", staticmethod(lambda: Path(d))):
            self.assertFalse(mb.login_enabled())
            mb.set_login(True, ["/usr/bin/open", "-a", "/Applications/Resolve Backup.app"])
            self.assertTrue(mb.login_enabled())
            pl = plistlib.loads(mb.login_plist_path().read_bytes())
            self.assertEqual(pl["Label"], "com.resolvecloudbackup.app")
            self.assertTrue(pl["RunAtLoad"])
            self.assertEqual(pl["ProgramArguments"][:2], ["/usr/bin/open", "-a"])
            mb.set_login(False)
            self.assertFalse(mb.login_enabled())
            mb.set_login(False)  # idempotent

    def test_command_from_source_runs_app_py(self):
        cmd = mb.login_command("/usr/bin/python3", "/x/app.py")
        self.assertEqual(cmd, ["/usr/bin/python3", "/x/app.py"])

    def test_command_when_packaged_opens_the_bundle(self):
        exe = "/Applications/Resolve Backup.app/Contents/MacOS/Resolve Backup"
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(mb.login_command(exe),
                             ["/usr/bin/open", "-a", str(Path("/Applications/Resolve Backup.app").resolve())])


if __name__ == "__main__":
    unittest.main()
