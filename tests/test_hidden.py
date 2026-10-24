"""Backing up through a hidden (-nogui) DaVinci Resolve, and the continuous / off-hours schedule.

Everything that would touch the real machine (starting processes, ps, lsappinfo, signals) is faked.
"""
import datetime as dt
import itertools
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import resolve_cloud_backup as rcb  # noqa: E402
from test_backup import Clock, FakePM, FakeResolve, stamp  # noqa: E402

# Never start or quit a real DaVinci Resolve from the tests; tests that need it pass fakes.
rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
_fake_prefs = __import__("tempfile").mkdtemp()  # and never edit Resolve's real preferences
rcb.resolve_prefs_dir = lambda: rcb.Path(_fake_prefs)

EXE = rcb.RESOLVE_EXE
GUI = EXE
NOGUI = EXE + " -nogui"


class FakeOS:
    """Stands in for the machine: running processes, the front app, signals and commands run."""

    def __init__(self):
        self.procs: dict[int, str] = {}
        self.front = None
        self.commands = []
        self.signals = []
        self.pids = itertools.count(500)
        self.quit_ignored = False

    def popen(self, cmd, **kw):
        pid = next(self.pids)
        self.commands.append(cmd)
        self.procs[pid] = " ".join(cmd)
        fs = self

        class Proc:
            def __init__(self):
                self.pid = pid

            def poll(self):
                return None if pid in fs.procs else 0
        return Proc()

    def run(self, cmd, **kw):
        self.commands.append(cmd)
        out = ""
        if cmd[0] == "ps":
            out = "".join(f"{p:>6} {a}\n" for p, a in self.procs.items())
        elif cmd[0] == "lsappinfo":
            out = f'"pid"={self.front}\n' if self.front else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def kill(self, pid, sig):
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        if sig:
            self.signals.append(sig)
            del self.procs[pid]


class QuittableResolve(FakeResolve):
    def __init__(self, pm, fs, pid):
        super().__init__(pm)
        self.fs, self.pid = fs, pid

    def Quit(self):
        if not self.fs.quit_ignored:
            self.fs.procs.pop(self.pid, None)


def mono():
    c = itertools.count()
    return lambda: next(c) * 2.0  # every look at the clock is 2 s later


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fs = FakeOS()
        self.pm = FakePM()
        self.cfg = json.loads(json.dumps(rcb.DEFAULTS))
        self.cfg.update(backup_dir=str(self.tmp / "out"), pause_between_exports_seconds=0, min_free_gb=0)
        self.connects = 0
        self.ready_after = 0  # connection attempts before the hidden Resolve answers

    def connect(self, cfg):
        self.connects += 1
        live = [p for p, a in self.fs.procs.items() if "-nogui" in a]
        if not live or self.connects <= self.ready_after:
            return None
        return QuittableResolve(self.pm, self.fs, live[0])

    def hidden(self, cfg=None):
        return rcb.HiddenResolve(cfg or self.cfg, connect=self.connect, popen=self.fs.popen, run=self.fs.run,
                                 sleep=lambda s: None, clock=mono(), kill=self.fs.kill)


class ProcessListTests(Base):
    def test_finds_resolve_but_not_its_crash_reporter_or_this_app(self):
        self.fs.procs = {10: GUI, 11: NOGUI, 12: EXE + " -reportCrash /tmp/x -versionUpdate",
                         13: "/Applications/Resolve Backup.app/Contents/MacOS/Resolve Backup",
                         14: "/usr/bin/grep Resolve"}
        self.assertEqual(rcb.resolve_processes(self.fs.run), [(10, GUI), (11, NOGUI)])

    def test_front_app(self):
        self.fs.front = 42
        self.assertEqual(rcb.front_app_pid(self.fs.run), 42)
        self.fs.front = None
        self.assertIsNone(rcb.front_app_pid(self.fs.run))


class HiddenResolveTests(Base):
    def test_launch_connect_and_quit(self):
        h = self.hidden()
        self.ready_after = 3
        self.assertTrue(h.launch())
        self.assertEqual(self.fs.commands[0], [EXE, "-nogui"])
        self.assertIsNotNone(h.connect(self.cfg))
        self.assertEqual(self.connects, 4)                # waited for it to start answering
        h.quit()
        self.assertEqual(self.fs.procs, {})
        self.assertEqual(self.fs.signals, [])             # quit politely, no signals
        self.assertNotIn("open", [c[0] for c in self.fs.commands])

    def test_gives_up_if_it_exits_while_starting(self):
        h = self.hidden()
        h.launch()
        self.fs.procs.clear()
        self.assertIsNone(h.connect(self.cfg))

    def test_bringing_it_to_the_front_hands_resolve_over(self):
        h = self.hidden()
        h.launch()
        h.connect(self.cfg)
        self.assertFalse(h.wanted())
        self.fs.front = h.pid
        self.assertTrue(h.wanted())
        h.quit()
        self.assertEqual(self.fs.procs, {})
        self.assertIn(["open", "-a", "/Applications/DaVinci Resolve/DaVinci Resolve.app"], self.fs.commands)

    def test_quit_while_still_starting_waits_to_quit_properly(self):
        h = self.hidden()
        self.ready_after = 5
        h.launch()
        h.quit()                                          # never connected yet
        self.assertEqual(self.fs.procs, {})
        self.assertEqual(self.fs.signals, [])             # killing it would bring up Resolve's crash reporter

    def test_stuck_resolve_is_stopped(self):
        h = self.hidden()
        h.launch()
        h.connect(self.cfg)
        self.fs.quit_ignored = True
        h.quit()
        self.assertEqual(self.fs.signals, [signal.SIGTERM])
        self.assertEqual(self.fs.procs, {})


class ServiceHiddenTests(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(rcb.HiddenResolve, "available", staticmethod(lambda cfg: True))
        p.start()
        self.addCleanup(p.stop)
        orig_dirs = rcb.resolve_log_dirs
        rcb.resolve_log_dirs = lambda: []
        self.addCleanup(setattr, rcb, "resolve_log_dirs", orig_dirs)
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({"backup_dir": str(self.tmp / "out"), "min_free_gb": 0,
                                             "pause_between_exports_seconds": 0, "dashboard": {"port": 0}}))
        self.clock = Clock(dt.datetime(2026, 10, 13, 12, 0, 0))
        self.status = rcb.LiveStatus()
        self.gui_connects = 0

        def gui_connect(cfg):
            self.gui_connects += 1
            return FakeResolve(self.pm)
        self.svc = rcb.Service(self.cfg_path, status=self.status, connect=gui_connect, clock=self.clock,
                               state_dir=self.tmp, hidden_factory=lambda cfg: self.hidden(cfg),
                               processes=lambda: rcb.resolve_processes(self.fs.run))

    def test_backs_up_through_a_hidden_resolve_when_none_is_open(self):
        self.pm.add([], "Promo", stamp(self.clock.t, minutes=5))
        seen = {}
        orig = self.pm.ExportProject

        def spy(name, path, stills=True):
            seen["resolve"] = self.status.snapshot()["resolve"]
            seen["procs"] = dict(self.fs.procs)
            return orig(name, path, stills)
        self.pm.ExportProject = spy
        self.svc.tick()
        self.assertEqual(self.pm.exports, ["Promo"])
        self.assertTrue(seen["resolve"]["hidden"])
        self.assertEqual(list(seen["procs"].values()), [NOGUI])
        self.assertEqual(self.fs.procs, {})               # quit afterwards
        self.assertEqual(self.gui_connects, 0)
        self.assertFalse(self.status.snapshot()["resolve"]["connected"])

    def test_uses_the_open_resolve_when_the_user_has_it_open(self):
        self.fs.procs = {77: GUI}
        self.pm.add([], "Promo", stamp(self.clock.t, minutes=5))
        self.svc.tick()
        self.assertEqual(self.pm.exports, ["Promo"])
        self.assertEqual(self.gui_connects, 1)
        self.assertEqual([c for c in self.fs.commands if c[0] != "ps"], [])   # nothing started
        self.assertEqual(self.fs.procs, {77: GUI})        # and the user's Resolve left alone

    def test_takes_over_a_hidden_resolve_left_running(self):
        self.fs.procs = {88: NOGUI}
        self.pm.add([], "Promo", stamp(self.clock.t, minutes=5))
        self.svc.tick()
        self.assertEqual(self.pm.exports, ["Promo"])
        self.assertEqual(self.fs.procs, {})
        self.assertNotIn([EXE, "-nogui"], self.fs.commands)

    def test_turned_off_uses_the_normal_connection(self):
        with mock.patch.object(rcb.HiddenResolve, "available", staticmethod(lambda cfg: False)):
            self.svc.tick()
        self.assertEqual(self.gui_connects, 1)
        self.assertEqual(self.fs.commands, [])

    def test_opening_resolve_mid_check_stops_and_hands_over(self):
        for i in range(5):
            self.pm.add([], f"P{i}", stamp(self.clock.t, minutes=5))
        orig = self.pm.ExportProject

        def spy(name, path, stills=True):
            self.fs.front = next(p for p, a in self.fs.procs.items() if "-nogui" in a)  # user opens Resolve
            return orig(name, path, stills)
        self.pm.ExportProject = spy
        self.svc.tick()
        self.assertEqual(len(self.pm.exports), 1)
        self.assertEqual(self.status.snapshot()["last_cycle"]["status"], "interrupted")
        self.assertEqual(self.fs.procs, {})
        self.assertEqual(self.fs.commands[-1][:2], ["open", "-a"])
        # the next check (through the Resolve the user now has open) carries on where it stopped
        self.fs.procs = {99: GUI}
        self.pm.ExportProject = orig
        self.svc.request_run_now()
        self.svc.tick()
        self.assertEqual(len(self.pm.exports), 5)



A = "c31fb696-ac7b-4ad7-90cf-73708407fb40"
B = "0af96a7b-28e8-4037-9211-489e97dd6489"
C = "9089d4db-5a9c-48cc-abe5-ed5ce2eb0899"
NAMES = {A: "WashOrgSrvrDB_1", B: "WashOrgBackup_1", C: "WashOrgSrvrDyson_1"}
ORIGINAL = f"cloud*:{A}\ndisk:Local Database\nnetwork:washdb2023_110.0.0.53\n"


def dbname(uuid):
    return "db_" + uuid.replace("-", "")


class RotationTests(Base):
    """Each hidden check opens the next cloud library by editing Resolve's activedb.conf, then puts it back."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(rcb.HiddenResolve, "available", staticmethod(lambda cfg: True))
        p.start()
        self.addCleanup(p.stop)
        logs = self.tmp / "logs"
        logs.mkdir()
        (logs / "ResolveDebug.txt").write_text("".join(f"BlackmagicID: PL({u}:'{n}':D)\n" for u, n in NAMES.items()))
        orig_dirs = rcb.resolve_log_dirs
        rcb.resolve_log_dirs = lambda: [logs]
        self.addCleanup(setattr, rcb, "resolve_log_dirs", orig_dirs)
        self.conf = self.tmp / "prefs" / "activedb.conf"
        self.conf.parent.mkdir()
        self.conf.write_text(ORIGINAL)
        self.cfg_path = self.tmp / "config.json"
        self.write_cfg()
        self.clock = Clock(dt.datetime(2026, 10, 13, 12, 0, 0))
        self.status = rcb.LiveStatus()
        self.opened = []          # library each hidden Resolve opened
        self.conf_when_opened_for_user = None
        self.fallback = None      # set to make Resolve ignore the file and open this instead
        real_run = self.fs.run

        def run(cmd, **kw):
            if cmd[0] == "open":
                self.conf_when_opened_for_user = self.conf.read_text()
            return real_run(cmd, **kw)
        self.fs.run = run

    def write_cfg(self, **over):
        self.cfg_path.write_text(json.dumps({"backup_dir": str(self.tmp / "out"), "min_free_gb": 0,
                                             "pause_between_exports_seconds": 0, "dashboard": {"port": 0}, **over}))

    def service(self):
        return rcb.Service(self.cfg_path, status=self.status, connect=lambda c: None, clock=self.clock,
                           state_dir=self.tmp, hidden_factory=lambda cfg: self.hidden(cfg),
                           processes=lambda: rcb.resolve_processes(self.fs.run),
                           active_library=rcb.ActiveLibrary(self.conf, self.tmp / "activedb.conf.saved"))

    def connect(self, cfg):
        r = super().connect(cfg)
        if r:
            m = rcb.re.search(r"^cloud\*:(\S+)$", self.conf.read_text(), rcb.re.M)
            db = self.fallback or {"DbType": "PostgreSQL", "DbName": dbname(m.group(1)), "IpAddress": "aws"}
            self.pm.db = db
            self.opened.append((db["DbName"], self.connects))
        return r

    def check(self, svc):
        self.connects = 0
        svc.request_run_now()
        svc.tick()
        self.assertEqual(self.fs.procs, {})
        return self.status.snapshot()["last_cycle"]["status"]

    def test_takes_each_library_in_turn_and_puts_the_choice_back(self):
        svc = self.service()
        for _ in range(4):
            self.assertEqual(self.check(svc), "ok")
            self.assertEqual(self.conf.read_text(), ORIGINAL)       # back as the user left it
            self.clock.advance(minutes=2)
        # never-checked libraries first, alphabetically: Backup, SrvrDB, SrvrDyson; then the oldest again
        self.assertEqual([o[0] for o in self.opened], [dbname(B), dbname(A), dbname(C), dbname(B)])
        self.assertFalse((self.tmp / "activedb.conf.saved").exists())
        st = svc.backup.load_state()
        self.assertEqual({r["folder"] for r in st["databases"].values()}, set(NAMES.values()))

    def test_skip_libraries(self):
        self.write_cfg(skip_libraries=["WashOrgBackup_1"])
        svc = self.service()
        self.check(svc)
        self.clock.advance(minutes=2)
        self.check(svc)
        self.assertEqual([o[0] for o in self.opened], [dbname(A), dbname(C)])

    def test_turned_off_leaves_the_file_alone(self):
        self.write_cfg(rotate_libraries=False)
        svc = self.service()
        self.check(svc)
        self.assertEqual([o[0] for o in self.opened], [dbname(A)])
        self.assertEqual(self.conf.read_text(), ORIGINAL)

    def test_library_that_does_not_open_is_reported_and_skipped_next_time(self):
        svc = self.service()
        self.fallback = {"DbType": "Disk", "DbName": "Local Database"}
        self.assertEqual(self.check(svc), "library_not_opened")
        self.assertEqual(self.conf.read_text(), ORIGINAL)
        self.fallback = None
        self.clock.advance(minutes=2)
        self.assertEqual(self.check(svc), "ok")
        self.assertEqual(self.opened[-1][0], dbname(A))                # moved on from Backup
        self.assertNotIn("Local Database", json.dumps(svc.backup.load_state().get("databases", {})))

    def test_only_swaps_a_cloud_library_for_another(self):
        self.conf.write_text("disk*:Local Database\n")
        svc = self.service()
        self.fallback = {"DbType": "Disk", "DbName": "Local Database"}
        self.assertEqual(self.check(svc), "ok")
        self.assertEqual(self.conf.read_text(), "disk*:Local Database\n")

    def test_choice_put_back_before_resolve_is_reopened_for_the_user(self):
        svc = self.service()
        self.pm.add([], "P", stamp(self.clock.t, minutes=5))
        orig = self.pm.ExportProject

        def spy(name, path, stills=True):
            self.fs.front = next(p for p, a in self.fs.procs.items() if "-nogui" in a)
            return orig(name, path, stills)
        self.pm.ExportProject = spy
        self.check(svc)
        self.assertEqual(self.conf_when_opened_for_user, ORIGINAL)

    def test_left_changed_by_a_crash_is_put_back_at_start_up(self):
        (self.tmp / "activedb.conf.saved").write_text(ORIGINAL)
        self.conf.write_text(f"cloud*:{C}\ndisk:Local Database\n")
        self.service()
        self.assertEqual(self.conf.read_text(), ORIGINAL)
        self.assertFalse((self.tmp / "activedb.conf.saved").exists())

    def test_library_uuid(self):
        self.assertEqual(rcb.library_uuid(dbname(A)), A)
        self.assertIsNone(rcb.library_uuid("Local Database"))

class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_path = self.tmp / "config.json"
        self.clock = Clock(dt.datetime(2026, 10, 13, 12, 0, 0))
        self.wall = lambda: 1_800_000_000.0

    def svc(self, **cfg):
        base = {"backup_dir": str(self.tmp / "out"), "min_free_gb": 0, "dashboard": {"port": 0}}
        self.cfg_path.write_text(json.dumps({**base, **cfg}))
        return rcb.Service(self.cfg_path, connect=lambda c: None, clock=self.clock, wall=self.wall,
                           state_dir=self.tmp)

    def test_interval_zero_means_continuously(self):
        self.assertEqual(self.svc(interval_minutes=0)._wait_after_cycle(), 60)

    def test_off_hours_interval(self):
        s = self.svc(interval_minutes=0, active_hours=["07:00", "11:00"], off_hours_interval_minutes=60)
        self.assertEqual(s._wait_after_cycle(), 3600)     # 12:00 is out of hours
        self.clock.t = dt.datetime(2026, 10, 13, 6, 30)
        self.assertEqual(s._wait_after_cycle(), 1800)     # but never past 07:00
        self.clock.t = dt.datetime(2026, 10, 13, 8, 0)
        self.assertEqual(s._wait_after_cycle(), 60)

    def test_off_hours_checks_run_and_show_when_next(self):
        s = self.svc(active_hours=["07:00", "11:00"], off_hours_interval_minutes=60)
        s.tick()
        snap = s.status.snapshot()
        self.assertEqual(snap["state"], "outside_active_hours")
        self.assertEqual(snap["last_cycle"]["status"], "resolve_unavailable")   # a check did run
        self.assertIsNotNone(snap["next_run_at"])

    def test_no_off_hours_checks_by_default(self):
        s = self.svc(active_hours=["07:00", "11:00"])
        s.tick()
        snap = s.status.snapshot()
        self.assertIsNone(snap.get("last_cycle"))        # no check ran
        self.assertIsNone(snap["next_run_at"])

    def test_settings_validation(self):
        clean, errs = rcb.validate_settings({"interval_minutes": 0, "off_hours_interval_minutes": 120,
                                             "background_resolve": False})
        self.assertEqual(errs, {})
        self.assertEqual(clean, {"interval_minutes": 0, "off_hours_interval_minutes": 120,
                                 "background_resolve": False})
        _, errs = rcb.validate_settings({"off_hours_interval_minutes": -1, "background_resolve": "yes"})
        self.assertEqual(set(errs), {"off_hours_interval_minutes", "background_resolve"})


if __name__ == "__main__":
    unittest.main()
