"""Tests run against a fake Resolve scripting API (no Resolve needed)."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import resolve_cloud_backup as rcb  # noqa: E402

FMT = "%a %b %d %H:%M:%S %Y"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += dt.timedelta(**kw)


class FakeProject:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class FakePM:
    """Folder tree: {"projects": {name: {"mod": str}}, "folders": {name: subtree}}."""

    def __init__(self):
        self.tree = {"projects": {}, "folders": {}}
        self.path = []
        self.open = None
        self.dbs = [{"DbType": "Cloud", "DbName": "Studio Cloud"}, {"DbType": "Disk", "DbName": "Local"}]
        self.db = self.dbs[0]
        self.need_load = set()      # exports fail unless project is loaded
        self.always_fail = set()
        self.locked = set()         # open on another workstation, NOT in collaboration mode
        self.locked_blocks_export = False
        self.exports = []
        self.loads = []
        self.saves = 0

    # tree helpers
    def _node(self):
        n = self.tree
        for p in self.path:
            n = n["folders"][p]
        return n

    def add(self, path, name, mod, collab=False):
        n = self.tree
        for p in path:
            n = n["folders"].setdefault(p, {"projects": {}, "folders": {}})
        n["projects"][name] = {"mod": mod, "collab": collab}

    # API
    def GetCurrentDatabase(self): return self.db
    def GetDatabaseList(self): return self.dbs
    def SetCurrentDatabase(self, d):
        self.db, self.open, self.path = d, None, []
        return True
    def GetCurrentProject(self): return FakeProject(self.open) if self.open else None
    def SaveProject(self): self.saves += 1; return True
    def GotoRootFolder(self): self.path = []; return True
    def GotoParentFolder(self):
        if not self.path:
            return False
        self.path.pop()
        return True
    def GetCurrentFolder(self): return self.path[-1] if self.path else "Root"
    def OpenFolder(self, n):
        if n in self._node()["folders"]:
            self.path.append(n)
            return True
        return False
    def GetFolderListInCurrentFolder(self): return list(self._node()["folders"])
    def GetProjectListInCurrentFolder(self): return list(self._node()["projects"])
    def GetProjectAttributesInCurrentFolder(self):
        return {n: {"lastModifiedDate": p["mod"], "creationDate": p["mod"],
                    "liveCollaborationMode": p["collab"]}
                for n, p in self._node()["projects"].items()}
    def LoadProject(self, name):
        self.loads.append(name)
        if name in self.locked:      # "project in use" -> no handle
            return None
        if name in self._node()["projects"]:
            self.open = name
            return FakeProject(name)
        return None
    def CloseProject(self, proj): self.open = None; return True
    def ExportProject(self, name, file_path, stills=True):
        if name in self.always_fail or (name in self.need_load and self.open != name):
            return False
        if name in self.locked and self.locked_blocks_export:
            return False
        Path(file_path).write_bytes(b"DRP:" + name.encode())
        self.exports.append(name)
        return True


class FakeResolve:
    def __init__(self, pm):
        self.pm = pm

    def GetProjectManager(self): return self.pm
    def GetVersionString(self): return "fake"


def stamp(now, **ago):
    return (now - dt.timedelta(**ago)).strftime(FMT)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.now = dt.datetime(2026, 10, 13, 12, 0, 0)
        self.clock = Clock(self.now)
        self.pm = FakePM()
        self.cfg = json.loads(json.dumps(rcb.DEFAULTS))
        self.cfg.update(backup_dir=str(self.tmp / "out"), pause_between_exports_seconds=0, min_free_gb=0)

    def backup(self, **over):
        self.cfg.update(over)
        return rcb.Backup(self.cfg, self.tmp / "state.json", connect=lambda c: FakeResolve(self.pm),
                          clock=self.clock, sleep=lambda s: None)

    def files(self):
        return sorted(p for p in (self.tmp / "out").rglob("*.drp"))

    # ---- core incremental behaviour
    def test_first_run_lookback_then_incremental(self):
        self.pm.add([], "Fresh", stamp(self.now, hours=2))
        self.pm.add(["Client A", "2026"], "Nested", stamp(self.now, hours=1))
        self.pm.add([], "Ancient", stamp(self.now, days=90))
        b = self.backup()
        s = b.cycle()
        self.assertEqual((s["exported"], s["skipped_old"]), (2, 1))
        self.assertEqual(sorted(self.pm.exports), ["Fresh", "Nested"])
        # nested folder structure is mirrored on disk
        self.assertTrue(any("Client A" in str(p) and "2026" in str(p) for p in self.files()))

        # nothing changed -> nothing exported
        self.clock.advance(minutes=30)
        s = b.cycle()
        self.assertEqual((s["exported"], s["unchanged"]), (0, 3))

        # modify one project -> only it is exported again, as a new file
        self.pm.tree["projects"]["Fresh"]["mod"] = stamp(self.clock.t, minutes=1)
        self.clock.advance(minutes=30)
        s = b.cycle()
        self.assertEqual(s["exported"], 1)
        self.assertEqual(len([p for p in self.files() if p.name.startswith("Fresh__")]), 2)

        # a previously "old" project that gets edited is picked up
        self.pm.tree["projects"]["Ancient"]["mod"] = stamp(self.clock.t, minutes=1)
        self.clock.advance(minutes=30)
        self.assertEqual(b.cycle()["exported"], 1)

    def test_resolve_ui_location_restored(self):
        self.pm.add(["A", "B"], "P", stamp(self.now, hours=1))
        self.pm.path = ["A"]
        self.backup().cycle()
        self.assertEqual(self.pm.path, ["A"])

    def test_resolve_unavailable_is_not_fatal(self):
        b = rcb.Backup(self.cfg, self.tmp / "state.json", connect=lambda c: None, clock=self.clock)
        self.assertEqual(b.cycle()["status"], "resolve_unavailable")

    # ---- open project / load fallback
    def test_load_fallback_when_nothing_open(self):
        self.pm.add([], "Cloudy", stamp(self.now, hours=1), collab=True)
        self.pm.need_load.add("Cloudy")
        s = self.backup().cycle()
        self.assertEqual(s["exported"], 1)
        self.assertIsNone(self.pm.open)  # closed again

    # ---- projects open elsewhere
    def test_open_elsewhere_direct_export_still_works(self):
        self.pm.add([], "Busy", stamp(self.now, minutes=5))
        self.pm.locked.add("Busy")                       # someone else has it open
        s = self.backup().cycle()
        self.assertEqual(s["exported"], 1)
        self.assertEqual(self.pm.loads, [])              # never needed to load it

    def test_collab_project_falls_back_to_load(self):
        self.pm.add([], "Team", stamp(self.now, minutes=5), collab=True)
        self.pm.need_load.add("Team")                    # direct export refused
        s = self.backup().cycle()
        self.assertEqual(s["exported"], 1)
        self.assertEqual(self.pm.loads, ["Team"])

    def test_collab_mode_does_not_load_noncollab(self):
        self.pm.add([], "Locked", stamp(self.now, minutes=5))
        self.pm.locked.add("Locked")
        self.pm.locked_blocks_export = True
        s = self.backup(load_to_export="collab").cycle()
        self.assertEqual(s["failed"], 1)
        self.assertEqual(self.pm.loads, [])              # avoids any in-use dialog
        rec = list(json.loads((self.tmp / "state.json").read_text())["projects"].values())[0]
        self.assertIn("open elsewhere", rec["last_error"])

    def test_default_is_auto_and_loads_noncollab_when_direct_fails(self):
        self.assertEqual(rcb.DEFAULTS["load_to_export"], "auto")
        self.pm.add([], "Odd", stamp(self.now, minutes=5))
        self.pm.need_load.add("Odd")                     # direct refused, loading works
        s = self.backup().cycle()
        self.assertEqual((s["exported"], self.pm.loads), (1, ["Odd"]))

    def test_auto_mode_tries_load_for_locked_noncollab(self):
        self.pm.add([], "Locked", stamp(self.now, minutes=5))
        self.pm.locked.add("Locked")
        self.pm.locked_blocks_export = True
        self.backup().cycle()
        self.assertEqual(self.pm.loads, ["Locked"])

    def test_backoff_then_retry_and_recovery(self):
        self.pm.add([], "Locked", stamp(self.now, minutes=5))
        self.pm.locked.add("Locked")
        self.pm.locked_blocks_export = True
        b = self.backup()
        self.assertEqual(b.cycle()["failed"], 1)         # fails; retry in 5 min
        self.clock.advance(minutes=2)
        s = b.cycle()
        self.assertEqual((s["failed"], s["backoff"]), (0, 1))   # still backing off
        self.clock.advance(minutes=4)
        self.assertEqual(b.cycle()["failed"], 1)         # retried; now 10 min backoff
        self.clock.advance(minutes=9)
        self.assertEqual(b.cycle()["backoff"], 1)
        self.pm.locked.clear()                           # colleague closes the project
        self.clock.advance(minutes=2)
        s = b.cycle()
        self.assertEqual(s["exported"], 1)
        rec = list(json.loads((self.tmp / "state.json").read_text())["projects"].values())[0]
        self.assertEqual(rec["failures"], 0)
        self.assertNotIn("next_try", rec)

    def test_alert_fires_once_at_threshold(self):
        self.pm.add([], "Locked", stamp(self.now, minutes=5))
        self.pm.always_fail.add("Locked")
        alerts = []
        b = self.backup(alert_after_failures=3, retry_backoff_max_minutes=1)
        b.alert = lambda *a: alerts.append(a)
        for _ in range(5):
            b.cycle()
            self.clock.advance(minutes=2)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "Locked")
        self.assertEqual(alerts[0][2], 3)

    def test_alert_command_gets_environment(self):
        out = self.tmp / "alert.txt"
        cmd = (f'"{sys.executable}" -c "import os;open(r\'{out}\',\'w\').write('
               f'os.environ[\'RCB_PROJECT\']+\'|\'+os.environ[\'RCB_FAILURES\'])"')
        b = self.backup(alert_command=cmd)
        b._run_alert("Folder/P", "Studio Cloud", 3, "why")
        self.assertEqual(out.read_text(), "Folder/P|3")

    def test_truthy(self):
        for v in (True, 1, "1", "true", "True", "Enabled"):
            self.assertTrue(rcb.truthy(v), v)
        for v in (False, 0, "0", "false", "", None):
            self.assertFalse(rcb.truthy(v), v)

    def _probe(self, target, **kw):
        import contextlib
        import io
        orig = rcb.connect_resolve
        rcb.connect_resolve = lambda cfg: FakeResolve(self.pm)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = rcb.cmd_probe(self.cfg, target, **kw)
        finally:
            rcb.connect_resolve = orig
        return rc, buf.getvalue()

    def test_probe_direct_ok_while_open_elsewhere(self):
        self.pm.add(["F"], "P", stamp(self.now, minutes=5))
        self.pm.locked.add("P")
        self.pm.path = ["F"]
        rc, out = self._probe("F/P", try_load=False)
        self.assertEqual(rc, 0)
        self.assertIn("Direct ExportProject: OK", out)
        self.assertEqual(self.pm.path, ["F"])            # location restored
        self.assertEqual(self.files(), [])               # probe leaves no backups behind

    def test_probe_locked_reports_failure_and_never_loads_without_flag(self):
        self.pm.add([], "P", stamp(self.now, minutes=5))
        self.pm.locked.add("P")
        self.pm.locked_blocks_export = True
        rc, out = self._probe("P", try_load=False)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out)
        self.assertEqual(self.pm.loads, [])
        rc, out = self._probe("P", try_load=True)
        self.assertEqual(self.pm.loads, ["P"])
        self.assertIn("Neither route works", out)

    def test_probe_unknown_project(self):
        rc, out = self._probe("Nope", try_load=False)
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)

    def test_never_loads_when_user_has_project_open(self):
        self.pm.add([], "Cloudy", stamp(self.now, hours=1))
        self.pm.add([], "Mine", stamp(self.now, hours=1))
        self.pm.need_load.add("Cloudy")
        self.pm.open = "Mine"
        s = self.backup().cycle()
        self.assertEqual((s["exported"], s["failed"]), (1, 1))
        self.assertEqual(self.pm.open, "Mine")  # untouched

    def test_load_mode_never(self):
        self.pm.add([], "Cloudy", stamp(self.now, hours=1))
        self.pm.need_load.add("Cloudy")
        self.assertEqual(self.backup(load_to_export="never").cycle()["failed"], 1)

    def test_failed_export_leaves_no_partial_and_retries(self):
        self.pm.add([], "Bad", stamp(self.now, hours=1))
        self.pm.always_fail.add("Bad")
        b = self.backup()
        self.assertEqual(b.cycle()["failed"], 1)
        self.assertEqual(list((self.tmp / "out").rglob("*.drp*")), [])
        self.pm.always_fail.clear()
        self.clock.advance(minutes=30)
        self.assertEqual(b.cycle()["exported"], 1)

    # ---- database selection
    def test_database_not_current_without_switch_permission(self):
        self.pm.add([], "P", stamp(self.now, hours=1))
        s = self.backup(database={"name": "Local", "type": None}).cycle()
        self.assertEqual(s["status"], "database_not_current")
        self.assertEqual(self.pm.db["DbName"], "Studio Cloud")

    def test_database_switch_and_restore(self):
        self.pm.add([], "P", stamp(self.now, hours=1))
        s = self.backup(database={"name": "Local", "type": "Disk"}, switch_database=True).cycle()
        self.assertEqual(s["status"], "ok")
        self.assertEqual(self.pm.db["DbName"], "Studio Cloud")  # restored

    def test_no_switch_when_project_open(self):
        self.pm.open = "Mine"
        s = self.backup(database={"name": "Local"}, switch_database=True).cycle()
        self.assertEqual(s["status"], "project_open_cannot_switch")
        self.assertEqual(self.pm.open, "Mine")

    def test_unknown_database(self):
        self.assertEqual(self.backup(database={"name": "Nope"}).cycle()["status"], "database_not_found")

    # ---- safety valves & filters
    def test_unparseable_dates_are_capped_per_cycle(self):
        for i in range(5):
            self.pm.add([], f"P{i}", "some weird format")
        b = self.backup(max_exports_per_cycle=2)
        s = b.cycle()
        self.assertEqual((s["exported"], s["deferred"]), (2, 3))
        self.clock.advance(minutes=30)
        self.assertEqual(b.cycle()["exported"], 2)  # the rest drain over later cycles

    def test_include_exclude(self):
        self.pm.add(["Keep"], "A", stamp(self.now, hours=1))
        self.pm.add(["Other"], "B", stamp(self.now, hours=1))
        self.pm.add(["Keep"], "Scratch 1", stamp(self.now, hours=1))
        s = self.backup(include=["Keep"], exclude=["*/Scratch*"]).cycle()
        self.assertEqual((s["exported"], s["filtered"]), (1, 2))

    def test_save_open_project_opt_in(self):
        self.pm.open = "Mine"
        self.backup().cycle()
        self.assertEqual(self.pm.saves, 0)
        self.backup(save_open_project=True).cycle()
        self.assertEqual(self.pm.saves, 1)

    # ---- retention + helpers
    def test_retention_policy(self):
        now = self.now
        mk = lambda **a: (Path(f"f_{len(a)}_{list(a.values())[0]}"), now - dt.timedelta(**a))  # noqa: E731
        files = [mk(hours=1), mk(hours=3), mk(hours=30), mk(hours=34), mk(days=5), mk(days=90)]
        dels = rcb.select_deletions(files, now, keep_all_hours=24, keep_daily_days=30)
        kept = {p for p, _ in files} - set(dels)
        # recent two kept; one of the two same-day older ones kept; 5-day kept; 90-day pruned
        self.assertIn(files[0][0], kept)
        self.assertIn(files[1][0], kept)
        self.assertEqual(len([p for p in (files[2][0], files[3][0]) if p in kept]), 1)
        self.assertIn(files[4][0], kept)
        self.assertIn(files[5][0], dels)

    def test_newest_always_kept(self):
        only = [(Path("x"), self.now - dt.timedelta(days=900))]
        self.assertEqual(rcb.select_deletions(only, self.now, 24, 30), [])

    def test_retention_end_to_end(self):
        self.pm.add([], "P", stamp(self.now, hours=1))
        b = self.backup(retention={"keep_all_hours": 1, "keep_daily_days": 2})
        for i in range(4):
            self.pm.tree["projects"]["P"]["mod"] = stamp(self.clock.t, minutes=1)
            b.cycle()
            self.clock.advance(days=1)
        self.assertLessEqual(len(self.files()), 3)
        self.assertGreaterEqual(len(self.files()), 1)

    def test_safe_name_and_collisions(self):
        self.assertNotEqual(rcb.safe_name("a/b"), rcb.safe_name("a:b"))
        self.assertEqual(rcb.safe_name("Plain Name"), "Plain Name")
        self.assertNotIn("/", rcb.safe_name("x/y"))
        self.assertTrue(rcb.safe_name("CON").startswith("_"))

    def test_parse_ts(self):
        for v in ("Tue Oct 13 12:34:56 2026", "2026-10-13 12:34:56", "2026-10-13T12:34:56",
                  "2026-10-13T12:34:56+00:00", 1791894896, "1791894896"):
            self.assertIsNotNone(rcb.parse_ts(v), v)
        for v in (None, "", "garbage"):
            self.assertIsNone(rcb.parse_ts(v))

    def test_active_hours(self):
        t = lambda h: dt.datetime(2026, 1, 1, h, 0)  # noqa: E731
        self.assertTrue(rcb.in_active_hours(None, t(3)))
        self.assertTrue(rcb.in_active_hours(["07:00", "22:00"], t(9)))
        self.assertFalse(rcb.in_active_hours(["07:00", "22:00"], t(23)))
        self.assertTrue(rcb.in_active_hours(["22:00", "06:00"], t(2)))   # wraps midnight
        self.assertFalse(rcb.in_active_hours(["22:00", "06:00"], t(12)))

    def test_low_disk_skips(self):
        s = self.backup(min_free_gb=10**9).cycle()
        self.assertEqual(s["status"], "low_disk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
