import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import install  # noqa: E402
import resolve_cloud_backup as rcb  # noqa: E402

# Never start or quit a real DaVinci Resolve from the tests; tests that need it pass fakes.
rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
_fake_prefs = __import__("tempfile").mkdtemp()  # and never edit Resolve's real preferences
rcb.resolve_prefs_dir = lambda: rcb.Path(_fake_prefs)
rcb.start_watchdog = lambda cmd: None  # nor start a real watchdog process


class MacInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.cmds = []
        self.stack = [
            mock.patch.object(Path, "home", staticmethod(lambda: home)),
            mock.patch.object(rcb, "SYSTEM", "Darwin"),
            mock.patch.object(install, "SYSTEM", "Darwin"),
            mock.patch.object(install, "run", side_effect=self._run),
        ]
        for m in self.stack:
            m.start()
        self.home = home

    def tearDown(self):
        for m in reversed(self.stack):
            m.stop()
        self.tmp.cleanup()

    def _run(self, cmd, check=True):
        self.cmds.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    def test_deploy_copies_every_program_file(self):
        args = mock.Mock(backup_dir="/Volumes/B", interval=15)
        script, cfg = install.deploy(args)
        for name in install.FILES:
            self.assertTrue((rcb.app_dir() / name).exists(), name)
        self.assertEqual(script.name, "resolve_cloud_backup.py")
        self.assertIn("/Volumes/B", cfg.read_text())

    def test_menubar_agent_plist(self):
        d = install.menubar_plist_dict("/venv/bin/python3", Path("/app/menubar.py"), Path("/logs"))
        self.assertEqual(d["Label"], "com.resolvecloudbackup.menubar")
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(d["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(d["ProgramArguments"], ["/venv/bin/python3", "/app/menubar.py"])
        plistlib.dumps(d)  # serialisable

    def test_menu_install_and_uninstall(self):
        (rcb.app_dir()).mkdir(parents=True)
        with mock.patch.object(install, "mac_menu_python", return_value="/venv/bin/python3"):
            self.assertTrue(install.mac_menu_install())
        p = install.mac_menu_plist()
        self.assertEqual(plistlib.loads(p.read_bytes())["Label"], install.MENU_LABEL)
        self.assertTrue(any(c[:2] == ["launchctl", "bootstrap"] for c in self.cmds))
        install.mac_menu_uninstall()
        self.assertFalse(p.exists())

    def test_menu_is_skipped_not_fatal_when_rumps_cannot_install(self):
        with mock.patch.object(install, "mac_menu_python", return_value=None):
            self.assertFalse(install.mac_menu_install())
        self.assertFalse(install.mac_menu_plist().exists())


if __name__ == "__main__":
    unittest.main()
