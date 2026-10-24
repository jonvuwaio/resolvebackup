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
