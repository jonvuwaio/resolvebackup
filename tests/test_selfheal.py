"""Self-healing: calls into Resolve made from a worker process with time limits, the health check that restarts a
stuck app, taking back a Resolve that macOS reopened after a restart, and the memory warning for the user's Resolve.

The worker tests start real worker processes against a fake DaVinciResolveScript module; nothing else touches the
real machine.
"""
import datetime as dt
import json
import plistlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import menubar  # noqa: E402
import resolve_cloud_backup as rcb  # noqa: E402
import resolve_worker  # noqa: E402
from test_backup import Clock, FakePM, FakeResolve  # noqa: E402
from test_hidden import EXE, GUI, NOGUI, FakeOS, QuittableResolve, mono  # noqa: E402

FAKE_MODULE = textwrap.dedent('''
    import time

    class Folder:
        def __init__(self, name): self.name = name
        def GetName(self): return self.name

    class PM:
        def GetCurrentDatabase(self): return {"DbType": "Cloud", "DbName": "db_x"}
        def GetFolders(self): return [Folder("A"), Folder("B")]
        def Echo(self, *a, **kw): return [a, kw]
        def NameOf(self, folder): return folder.GetName()
        def Slow(self, s): time.sleep(s); return "done"
        def Boom(self): raise ValueError("nope")

    class Resolve:
        def __init__(self): self.pm = PM()
        def GetProjectManager(self): return self.pm

    _r = Resolve()
    def scriptapp(name): return _r if name == "Resolve" else None
''')


def fake_api() -> str:
    api = Path(tempfile.mkdtemp())
    (api / "Modules").mkdir()
    (api / "Modules" / "DaVinciResolveScript.py").write_text(FAKE_MODULE)
    return str(api)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.api = fake_api()
        self.w = resolve_worker.Worker(call_limit=10)
        self.addCleanup(self.w.close)

    def test_calls_and_objects_go_through_the_worker(self):
        r = self.w.connect(self.api, "unused")
        pm = r.GetProjectManager()
        self.assertEqual(pm.GetCurrentDatabase(), {"DbType": "Cloud", "DbName": "db_x"})
        folders = pm.GetFolders()
        self.assertEqual([f.GetName() for f in folders], ["A", "B"])
        self.assertEqual(pm.NameOf(folders[1]), "B")                 # objects can be passed back in
        self.assertEqual(pm.Echo(1, "x", k=[2]), [[1, "x"], {"k": [2]}])
        self.assertNotEqual(self.w.pid, None)

    def test_errors_come_back_as_exceptions(self):
        pm = self.w.connect(self.api, "unused").GetProjectManager()
        with self.assertRaises(resolve_worker.RemoteError):
            pm.Boom()
        with self.assertRaises(AttributeError):
            pm.NoSuchMethod()
        self.assertEqual(pm.GetCurrentDatabase()["DbName"], "db_x")   # still fine afterwards

    def test_a_stuck_call_is_given_up_and_the_worker_replaced(self):
        pm = self.w.connect(self.api, "unused").GetProjectManager()
        first = self.w.pid
        with self.w.limit(1, "backing up X"):
            with self.assertRaises(resolve_worker.ResolveNotAnswering) as e:
                pm.Slow(30)
        self.assertIn("backing up X", str(e.exception))
        with self.assertRaises(resolve_worker.ResolveNotAnswering):
            pm.GetCurrentDatabase()                                   # objects from the old worker are dead
        pm = self.w.connect(self.api, "unused").GetProjectManager()
        self.assertNotEqual(self.w.pid, first)
        self.assertEqual(pm.GetCurrentDatabase()["DbName"], "db_x")

    def test_the_app_keeps_running_while_resolve_is_slow(self):
        pm = self.w.connect(self.api, "unused").GetProjectManager()
        ticks = []
        t = threading.Thread(target=lambda: ticks.append(pm.Slow(1.5)))
        t.start()
        time.sleep(0.3)
        self.assertGreater(self.w.busy_for(), 0)
        n = sum(1 for _ in range(200_000))                            # this thread is not frozen meanwhile
        t.join(10)
        self.assertEqual((n, ticks), (200_000, ["done"]))
        self.assertEqual(self.w.busy_for(), 0)

    def test_connect_returns_none_when_the_module_is_missing(self):
        self.assertIsNone(self.w.connect(tempfile.mkdtemp(), "unused"))


class StuckPM(FakePM):
    def ExportProject(self, name, file_path, stills=True):
        raise rcb.ResolveNotAnswering("DaVinci Resolve didn't answer within 900 s while backing up " + name)


class CycleTests(unittest.TestCase):
    def test_a_stuck_export_counts_against_that_project_and_ends_the_check(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = json.loads(json.dumps(rcb.DEFAULTS))
        cfg.update(backup_dir=str(tmp / "out"), pause_between_exports_seconds=0, min_free_gb=0)
        pm = StuckPM()
        now = dt.datetime(2026, 11, 17, 14, 0)
        stamp = now.strftime("%a %b %d %H:%M:%S %Y")
        pm.add([], "Hangs", stamp)
        pm.add([], "Next", stamp)
        b = rcb.Backup(cfg, tmp / "state.json", connect=lambda c: FakeResolve(pm), clock=Clock(now))
        res = b.cycle()
        self.assertEqual(res["status"], "resolve_not_answering")
        self.assertEqual(res["failed"], 1)
        rec = b.load_state()["projects"]["Studio Cloud::Hangs"]
        self.assertIn("stopped answering", rec["last_error"])
        self.assertIn("next_try", rec)                                 # backs off like any failure
        self.assertNotIn("Studio Cloud::Next", b.load_state()["projects"])
        self.assertEqual(b.load_state()["last_cycle"]["status"], "resolve_not_answering")


class FakeMachine(FakeOS):
    """FakeOS plus the ps/sysctl answers the self-healing code asks for."""

    def __init__(self):
        super().__init__()
        self.boot = "2000"
        self.rss_kb = {}
        self.age = {}
        self.login = 100

    def run(self, cmd, **kw):
        if cmd[:3] == ["ps", "-o", "rss="]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.rss_kb.get(int(cmd[-1]), '')}\n", stderr="")
        if cmd[:3] == ["ps", "-o", "etime="]:
            s = self.age.get(int(cmd[-1]))
            out = "" if s is None else f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if cmd[:2] == ["ps", "-x"]:
            dock = "/System/Library/CoreServices/Dock.app/Contents/MacOS/Dock"
            out = f"   01:00 /usr/libexec/foo\n{self.login // 60:02d}:{self.login % 60:02d} {dock}\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if cmd[0] == "sysctl":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{{ sec = {self.boot}, usec = 0 }} Mon\n", stderr="")
        return super().run(cmd, **kw)


class ServiceRig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fs = FakeMachine()
        self.pm = FakePM()
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({"backup_dir": str(self.tmp / "out"), "min_free_gb": 0,
                                             "pause_between_exports_seconds": 0, "background_resolve": True,
                                             "rotate_libraries": False, "dashboard": {"port": 0}}))
        self.alerts = []
        p = mock.patch.object(rcb.HiddenResolve, "available", staticmethod(lambda cfg: True))
        p.start()
        self.addCleanup(p.stop)

    def connect(self, cfg):
        live = list(self.fs.procs)
        return QuittableResolve(self.pm, self.fs, live[0]) if live else None

    def hidden(self, cfg):
        return rcb.HiddenResolve(cfg, connect=self.connect, popen=self.fs.popen, run=self.fs.run,
                                 sleep=lambda s: None, clock=mono(), kill=self.fs.kill)

    def service(self):
        svc = rcb.Service(self.cfg_path, connect=self.connect, clock=Clock(dt.datetime(2026, 11, 17, 9)),
                          state_dir=self.tmp, hidden_factory=self.hidden,
                          processes=lambda: rcb.resolve_processes(self.fs.run), watchdog=lambda pid: None,
                          active_library=rcb.ActiveLibrary(self.tmp / "activedb.conf", self.tmp / "saved"))
        svc.run = self.fs.run
        svc.isolated = True  # as in the app, where calls go through the worker
        svc.backup.alert = lambda *a: self.alerts.append(a)
        return svc


class ReopenedResolveTests(ServiceRig):
    """macOS reopens a Resolve that was running hidden when the Mac restarted, with its window."""

    def reopened(self, **over):
        self.fs.procs = {700: GUI}
        self.fs.age = {700: over.get("age", 90)}
        self.fs.login = over.get("login", 100)
        note = {"pid": 4242, "boot": over.get("old_boot", "1000")}
        self.tmp.joinpath("hidden_resolve.json").write_text(json.dumps(note))

    def test_takes_it_back_and_runs_hidden_again(self):
        self.reopened()
        svc = self.service()
        h = svc._hidden_session()
        self.assertIsNotNone(h)                                      # backups run hidden again
        self.assertNotIn(700, self.fs.procs)                         # the reopened one was quit
        self.assertIn([EXE, "-nogui"], self.fs.commands)
        self.assertEqual(self.fs.signals, [])                        # asked to quit, not killed
        h.quit()
        self.assertFalse(self.tmp.joinpath("hidden_resolve.json").exists())

    def test_leaves_it_if_a_project_is_open(self):
        self.reopened()
        self.pm.open = "Grade_v3"
        self.assertIsNone(self.service()._hidden_session())
        self.assertIn(700, self.fs.procs)
        self.assertFalse(self.tmp.joinpath("hidden_resolve.json").exists())  # only one look

    def test_leaves_it_if_it_was_opened_after_login(self):
        self.reopened(age=60, login=3600)
        self.assertIsNone(self.service()._hidden_session())
        self.assertIn(700, self.fs.procs)

    def test_leaves_it_if_in_front(self):
        self.reopened()
        self.fs.front = 700
        self.assertIsNone(self.service()._hidden_session())
        self.assertIn(700, self.fs.procs)

    def test_leaves_it_without_a_restart_since_the_hidden_one(self):
        self.reopened(old_boot="2000")                                # same boot: the user opened Resolve
        self.assertIsNone(self.service()._hidden_session())
        self.assertIn(700, self.fs.procs)

    def test_leaves_it_without_a_note(self):
        self.fs.procs = {700: GUI}
        self.fs.age = {700: 90}
        self.assertIsNone(self.service()._hidden_session())
        self.assertIn(700, self.fs.procs)

    def test_a_hidden_launch_leaves_a_note_until_it_quits(self):
        svc = self.service()
        h = svc._hidden_session()
        note = json.loads(self.tmp.joinpath("hidden_resolve.json").read_text())
        self.assertEqual(note, {"pid": h.pid, "boot": "2000"})
        h.quit()
        self.assertFalse(self.tmp.joinpath("hidden_resolve.json").exists())

    def test_power_off_stops_the_hidden_resolve_and_keeps_the_note(self):
        svc = self.service()
        h = svc._hidden_session()
        with mock.patch.object(rcb.os, "kill", self.fs.kill):
            svc.power_off()
        self.assertTrue(svc.stopping)
        self.assertEqual(self.fs.signals, [signal.SIGTERM])
        h.quit()
        self.assertTrue(self.tmp.joinpath("hidden_resolve.json").exists())


class MemoryWarningTests(ServiceRig):
    def test_warns_once_about_a_huge_resolve_and_never_stops_it(self):
        self.fs.procs = {700: GUI}
        self.fs.rss_kb = {700: 53_000_000}
        svc = self.service()
        svc._check_own_resolve(svc.processes())
        svc._check_own_resolve(svc.processes())
        health = svc.status.get("resolve_health")
        self.assertEqual((health["warn"], health["memory_gb"], health["pid"]), (True, 54.3, 700))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("54 GB", self.alerts[0][3])
        self.assertIn(700, self.fs.procs)
        self.assertEqual(self.fs.signals, [])

    def test_quiet_below_the_limit_and_when_hidden(self):
        self.fs.procs = {700: GUI}
        self.fs.rss_kb = {700: 8_000_000}
        svc = self.service()
        svc._check_own_resolve(svc.processes())
        self.assertFalse(svc.status.get("resolve_health")["warn"])
        self.fs.procs = {701: NOGUI}
        svc._check_own_resolve(svc.processes())
        self.assertIsNone(svc.status.get("resolve_health"))
        self.assertEqual(self.alerts, [])

    def test_menu_bar_asks_for_a_restart(self):
        s = menubar.summarize({"state": "idle", "last_cycle": {"status": "ok", "at": "2026-11-17T09:00:00"},
                               "resolve_health": {"warn": True, "memory_gb": 53.4, "pid": 700}})
        self.assertEqual((s["title"], s["colour"]), ("DaVinci Resolve needs a restart", "wait"))
        alerts = menubar.Alerts()
        self.assertIn("53 GB", alerts.check(s))
        self.assertIsNone(alerts.check(s))                           # once


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ran, self.killed = [], []
        self.alive = {111}
        self.answer = True

    def write_info(self):
        (self.dir / "service.json").write_text(json.dumps({"pid": 111, "port": 8765,
                                                           "relaunch": ["/usr/bin/open", "-a", "/X/CPM.app"]}))

    def check(self):
        def kill(pid, sig):
            self.killed.append(sig)
            self.alive.discard(pid)
        return rcb.run_health_check(self.dir, fetch=lambda port: self.answer, alive=lambda p: p in self.alive,
                                    kill=kill, run=lambda cmd, **kw: self.ran.append(cmd), sleep=lambda s: None)

    def test_nothing_to_do_when_quit_on_purpose(self):
        self.alive = set()
        self.check()
        self.assertEqual((self.ran, self.killed), ([], []))

    def test_starts_it_again_after_a_crash(self):
        self.write_info()
        self.alive = set()
        self.check()
        self.assertEqual(self.ran, [["/usr/bin/open", "-a", "/X/CPM.app"]])
        self.assertFalse((self.dir / "service.json").exists())

    def test_restarts_it_after_five_minutes_without_an_answer(self):
        self.write_info()
        self.answer = False
        for _ in range(rcb.HEALTH_FAILS - 1):
            self.check()
        self.assertEqual((self.ran, self.killed), ([], []))
        self.check()
        self.assertEqual(self.killed, [signal.SIGTERM])
        self.assertEqual(self.ran, [["/usr/bin/open", "-a", "/X/CPM.app"]])
        self.assertFalse((self.dir / rcb.HEALTH_FILE).exists())

    def test_an_answer_resets_the_count(self):
        self.write_info()
        self.answer = False
        for _ in range(rcb.HEALTH_FAILS - 1):
            self.check()
        self.answer = True
        self.check()
        self.answer = False
        self.check()
        self.assertEqual(self.killed, [])

    def test_healthz_reports_a_stuck_loop(self):
        status = rcb.LiveStatus()
        dash = rcb.DashboardServer.__new__(rcb.DashboardServer)
        dash.svc = mock.Mock(status=status, worker=resolve_worker.Worker())
        self.assertTrue(dash.health()[0])
        with mock.patch.object(rcb.time, "monotonic", return_value=time.monotonic() + rcb.HEALTH_STALE + 1):
            ok, body = dash.health()
        self.assertFalse(ok)
        self.assertGreater(body["quiet_s"], rcb.HEALTH_STALE)

    def test_relaunch_command_is_the_app_bundle(self):
        self.assertEqual(rcb.relaunch_command("/Users/x/Downloads/CPM-1.30.0/Colour Project Manager.app/Contents/"
                                              "MacOS/Colour Project Manager"),
                         ["/usr/bin/open", "-a", "/Users/x/Downloads/CPM-1.30.0/Colour Project Manager.app"])


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name in ("login_plist_path", "health_plist_path"):
            p = mock.patch.object(menubar, name, lambda n=name: self.dir / f"{n}.plist")
            p.start()
            self.addCleanup(p.stop)

    def test_login_item_follows_the_copy_that_runs(self):
        menubar.set_login(True, ["/usr/bin/open", "-a", "/old/1.26.1/CPM.app"])
        self.assertTrue(menubar.sync_login(["/usr/bin/open", "-a", "/new/1.30.0/CPM.app"]))
        d = plistlib.loads(menubar.login_plist_path().read_bytes())
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/open", "-a", "/new/1.30.0/CPM.app"])
        self.assertFalse(menubar.sync_login(["/usr/bin/open", "-a", "/new/1.30.0/CPM.app"]))

    def test_login_item_left_off_when_off(self):
        self.assertFalse(menubar.sync_login(["/usr/bin/open", "-a", "/new/CPM.app"]))
        self.assertFalse(menubar.login_plist_path().exists())

    def test_health_check_agent_installed_once(self):
        ran = []
        cmd = ["/X/CPM.app/Contents/MacOS/CPM", rcb.HEALTH_FLAG]
        self.assertTrue(menubar.install_health_check(cmd, run=lambda c, **kw: ran.append(c)))
        d = plistlib.loads(menubar.health_plist_path().read_bytes())
        self.assertEqual((d["ProgramArguments"], d["StartInterval"]), (cmd, 60))
        self.assertEqual([c[1] for c in ran], ["bootout", "bootstrap"])
        self.assertFalse(menubar.install_health_check(cmd, run=lambda c, **kw: ran.append(c)))
        self.assertEqual(len(ran), 2)


if __name__ == "__main__":
    unittest.main()
