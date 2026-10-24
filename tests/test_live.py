"""Live status, controls, hot reload and the dashboard API (fake Resolve, real HTTP on localhost)."""
import datetime as dt
import http.client
import json
import subprocess
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import resolve_cloud_backup as rcb  # noqa: E402

# Never start or quit a real DaVinci Resolve from the tests; tests that need it pass fakes.
rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
from test_backup import Clock, FakePM, FakeResolve, stamp  # noqa: E402

TOKEN = "t" * 32


class Wall:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_path = self.tmp / "config.json"
        self.logs = self.tmp / "resolve-logs"  # stands in for Resolve's own log folder
        orig_dirs = rcb.resolve_log_dirs
        rcb.resolve_log_dirs = lambda: [self.logs]
        self.addCleanup(setattr, rcb, "resolve_log_dirs", orig_dirs)
        self.write_cfg()
        self.pm = FakePM()
        self.clock = Clock(dt.datetime(2026, 10, 13, 12, 0, 0))
        self.wall = Wall()
        self.status = rcb.LiveStatus()
        self.svc = rcb.Service(self.cfg_path, status=self.status, connect=lambda c: FakeResolve(self.pm),
                               clock=self.clock, wall=self.wall, state_dir=self.tmp)

    def write_cfg(self, **over):
        cfg = {"backup_dir": str(self.tmp / "out"), "min_free_gb": 0, "pause_between_exports_seconds": 0,
               "interval_minutes": 30, "retry_minutes": 5, "dashboard": {"enabled": True, "port": 0},
               "alert_command": "echo keep-me"}
        cfg.update(over)
        self.cfg_path.write_text(json.dumps(cfg))

    def advance(self, seconds):
        self.wall.t += seconds
        self.clock.advance(seconds=seconds)


class ServiceTests(LiveBase):
    def test_first_tick_runs_a_cycle_and_publishes_progress(self):
        self.pm.add([], "Promo", stamp(self.clock.t, minutes=5))
        seen = {}
        orig = self.pm.ExportProject

        def spy(name, path, stills=True):
            seen["snap"] = self.status.snapshot()
            return orig(name, path, stills)
        self.pm.ExportProject = spy
        self.svc.tick()
        snap = seen["snap"]
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["cycle"]["current"], "Promo")
        self.assertEqual(snap["cycle"]["phase"], "exporting")
        self.assertEqual(snap["resolve"]["database"], "Studio Cloud")
        after = self.status.snapshot()
        self.assertEqual((after["state"], after["cycle"]), ("idle", None))
        self.assertEqual(after["last_cycle"]["exported"], 1)
        self.assertIsNotNone(after["next_run_at"])

    def test_waits_for_interval_then_runs_again(self):
        self.pm.add([], "P", stamp(self.clock.t, minutes=5))
        self.svc.tick()
        self.pm.tree["projects"]["P"]["mod"] = stamp(self.clock.t, seconds=1)
        self.advance(600)
        self.svc.tick()
        self.assertEqual(self.pm.exports, ["P"])         # not due yet
        self.advance(1300)
        self.svc.tick()
        self.assertEqual(self.pm.exports, ["P", "P"])

    def test_back_up_now_and_pause(self):
        self.pm.add([], "P", stamp(self.clock.t, minutes=5))
        self.svc.tick()
        self.pm.tree["projects"]["P"]["mod"] = stamp(self.clock.t, seconds=1)
        self.advance(60)
        self.assertTrue(self.svc.request_run_now())
        self.svc.tick()
        self.assertEqual(len(self.pm.exports), 2)        # ran early on request
        # paused: even when overdue nothing runs
        self.pm.tree["projects"]["P"]["mod"] = stamp(self.clock.t, seconds=1)
        self.svc.set_paused(True)
        self.advance(4000)
        self.svc.tick()
        self.assertEqual(len(self.pm.exports), 2)
        self.assertEqual(self.status.snapshot()["state"], "paused")
        self.svc.set_paused(False)
        self.svc.tick()
        self.assertEqual(len(self.pm.exports), 3)

    def test_run_now_ignored_while_running(self):
        self.status.set(state="running")
        self.assertFalse(self.svc.request_run_now())

    def test_outside_active_hours(self):
        self.write_cfg(active_hours=["07:00", "11:00"])
        self.svc.reload_config()
        self.pm.add([], "P", stamp(self.clock.t, minutes=5))
        self.svc.tick()                                  # it's 12:00
        self.assertEqual(self.pm.exports, [])
        self.assertEqual(self.status.snapshot()["state"], "outside_active_hours")
        self.assertTrue(self.svc.request_run_now())      # explicit request still works
        self.svc.tick()
        self.assertEqual(self.pm.exports, ["P"])

    def test_waiting_for_resolve_and_retry_interval(self):
        svc = rcb.Service(self.cfg_path, status=self.status, connect=lambda c: None, clock=self.clock,
                          wall=self.wall, state_dir=self.tmp)
        svc.tick()
        snap = self.status.snapshot()
        self.assertEqual(snap["state"], "waiting_for_resolve")
        self.assertFalse(snap["resolve"]["connected"])
        wait = svc.next_run - self.wall.t
        self.assertTrue(5 * 60 * 0.94 <= wait <= 5 * 60 * 1.06, wait)   # retry_minutes, not interval

    def test_hot_reload_applies_between_cycles(self):
        self.pm.add([], "P", stamp(self.clock.t, minutes=5))
        self.svc.tick()
        self.write_cfg(backup_dir=str(self.tmp / "elsewhere"), interval_minutes=1, retry_minutes=7)
        self.svc.tick()
        self.assertEqual(self.svc.cfg["backup_dir"], str(self.tmp / "elsewhere"))
        self.assertEqual(self.svc.backup.root, self.tmp / "elsewhere")
        self.assertLessEqual(self.svc.next_run - self.wall.t, 61)        # new, shorter interval

    def test_bad_config_keeps_previous_settings(self):
        self.svc.tick()
        self.cfg_path.write_text("{ not json")
        self.svc.tick()
        self.assertEqual(self.svc.cfg["interval_minutes"], 30)

    def test_history_recorded(self):
        self.pm.add([], "P", stamp(self.clock.t, minutes=5))
        self.svc.tick()
        self.advance(2000)
        self.svc.tick()
        hist = self.svc.backup.load_state()["history"]
        self.assertEqual(len(hist), 2)
        self.assertEqual((hist[0]["exported"], hist[1]["exported"]), (1, 0))

    def test_log_lines_reach_the_feed(self):
        h = rcb.EventHandler(self.status)
        rcb.log.addHandler(h)
        old = rcb.log.level
        rcb.log.setLevel(10)
        try:
            rcb.log.info("hello feed")
            rcb.log.debug("not shown")
        finally:
            rcb.log.removeHandler(h)
            rcb.log.setLevel(old)
        snap = self.status.snapshot()
        self.assertEqual([e["msg"] for e in snap["events"]], ["hello feed"])
        self.assertEqual(self.status.snapshot(since=snap["event_seq"])["events"], [])


class ChooseFolderTests(unittest.TestCase):
    class Run:
        def __init__(self, code=0, out="", err=""):
            self.code, self.out, self.err, self.args = code, out, err, None

        def __call__(self, args, **kw):
            self.args = args
            return subprocess.CompletedProcess(args, self.code, self.out, self.err)

    def setUp(self):
        p = mock.patch.object(rcb, "can_choose_folder", lambda: True)
        p.start()
        self.addCleanup(p.stop)

    def test_returns_the_chosen_path(self):
        run = self.Run(out="/Volumes/Backup/Resolve/\n")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rcb.choose_folder(str(Path(d) / "not" / "yet"), run=run), {"path": "/Volumes/Backup/Resolve"})
            self.assertEqual(run.args[-1], str(Path(d)))  # starts in the nearest folder that exists
        self.assertEqual(run.args[:2], ["osascript", "-e"])

    def test_cancel_and_errors(self):
        self.assertEqual(rcb.choose_folder(None, run=self.Run(1, err="execution error: User canceled. (-128)")),
                         {"cancelled": True})
        self.assertIn("error", rcb.choose_folder(None, run=self.Run(1, err="boom")))

    def test_not_on_a_mac(self):
        with mock.patch.object(rcb, "can_choose_folder", lambda: False):
            self.assertIn("error", rcb.choose_folder("/x", run=self.Run(out="/y")))


class SettingsValidation(unittest.TestCase):
    def test_valid(self):
        d = Path(tempfile.mkdtemp()) / "a" / "b"
        clean, errs = rcb.validate_settings({
            "backup_dir": str(d), "interval_minutes": 15, "active_hours": ["07:00", "22:30"],
            "include": [" Client A ", ""], "load_to_export": "auto", "switch_database": False,
            "retention": {"keep_all_hours": 12, "keep_daily_days": 10, "max_backups": 5},
            "database": {"name": "Studio Cloud", "type": ""}})
        self.assertEqual(errs, {})
        self.assertTrue(d.is_dir())
        self.assertEqual(clean["include"], ["Client A"])
        self.assertEqual(clean["database"], {"name": "Studio Cloud", "type": None})

    def test_invalid(self):
        blocker = Path(tempfile.mkdtemp()) / "file"
        blocker.write_text("x")
        _, errs = rcb.validate_settings({
            "interval_minutes": -1, "retry_minutes": "5", "active_hours": ["25:00", "22:00"],
            "backup_dir": str(blocker / "sub"), "load_to_export": "sometimes", "include": "x",
            "retention": {"keep_all_hours": -1, "max_backups": -3}, "alert_command": "rm -rf /", "min_free_gb": True})
        for k in ("interval_minutes", "retry_minutes", "active_hours", "backup_dir", "load_to_export",
                  "include", "retention.keep_all_hours", "retention.max_backups", "alert_command", "min_free_gb"):
            self.assertIn(k, errs, k)

    def test_relative_dir_rejected_and_same_times_rejected(self):
        _, errs = rcb.validate_settings({"backup_dir": "relative/path", "active_hours": ["07:00", "07:00"]})
        self.assertEqual(set(errs), {"backup_dir", "active_hours"})

    def test_apply_preserves_other_keys(self):
        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps({"alert_command": "echo hi", "retention": {"keep_all_hours": 24, "keep_daily_days": 30}}))
        rcb.apply_settings(p, {"interval_minutes": 10, "retention": {"keep_daily_days": 5}})
        cur = json.loads(p.read_text())
        self.assertEqual(cur["alert_command"], "echo hi")
        self.assertEqual(cur["retention"], {"keep_all_hours": 24, "keep_daily_days": 5})
        self.assertEqual(cur["interval_minutes"], 10)


class DashboardTests(LiveBase):
    def setUp(self):
        super().setUp()
        self.html = self.tmp / "d.html"
        self.html.write_text("<!doctype html><title>Test dash</title>")
        self.dash = rcb.DashboardServer(self.svc, port=0, html_path=self.html, token=TOKEN, state_dir=self.tmp)
        self.dash.start()
        self.addCleanup(self.dash.stop)
        self.pm.add(["Client"], "Feature", stamp(self.clock.t, minutes=5))
        self.pm.add([], "Old", stamp(self.clock.t, days=90))
        self.svc.tick()

    def req(self, method, path, headers=None, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.dash.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.dash.port}", **(headers or {})}
        data = json.dumps(body) if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    AUTH = {"X-RCB-Token": TOKEN}

    # ---- access control
    def test_requires_token(self):
        self.assertEqual(self.req("GET", "/")[0], 401)
        self.assertEqual(self.req("GET", "/api/status")[0], 401)
        self.assertEqual(self.req("GET", "/api/status", {"X-RCB-Token": "wrong"})[0], 401)
        self.assertEqual(self.req("GET", f"/?t={TOKEN}")[0], 200)
        self.assertEqual(self.req("GET", "/api/status", self.AUTH)[0], 200)

    def test_rejects_foreign_host_header(self):                  # DNS-rebinding defence
        self.assertEqual(self.req("GET", f"/?t={TOKEN}", host="evil.example.com")[0], 403)
        self.assertEqual(self.req("GET", "/api/status", self.AUTH, host=f"evil.example:{self.dash.port}")[0], 403)
        self.assertEqual(self.req("GET", "/api/status", self.AUTH, host=f"localhost:{self.dash.port}")[0], 200)

    def test_post_needs_header_token_and_same_origin(self):
        self.assertEqual(self.req("POST", f"/api/run-now?t={TOKEN}", body={})[0], 401)   # query token not enough
        self.assertEqual(self.req("POST", "/api/run-now", {**self.AUTH, "Origin": "https://evil.example"},
                                  body={})[0], 403)
        self.assertEqual(self.req("POST", "/api/run-now",
                                  {**self.AUTH, "Origin": f"http://127.0.0.1:{self.dash.port}"}, body={})[0], 200)

    def test_html_has_csp_and_no_unknown_routes(self):
        c = http.client.HTTPConnection("127.0.0.1", self.dash.port)
        c.request("GET", f"/?t={TOKEN}", headers={"Host": f"127.0.0.1:{self.dash.port}"})
        r = c.getresponse()
        r.read()
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.req("GET", "/etc/passwd", self.AUTH)[0], 404)
        self.assertEqual(self.req("GET", "/../../etc/passwd", self.AUTH)[0], 404)

    # ---- data
    def test_status_and_incremental_events(self):
        rcb.log.setLevel(20)
        h = rcb.EventHandler(self.status)
        rcb.log.addHandler(h)
        try:
            rcb.log.info("first")
            _, s = self.req("GET", "/api/status", self.AUTH)
            self.assertEqual(s["state"], "idle")
            self.assertEqual(s["last_cycle"]["exported"], 1)
            self.assertEqual(s["service"]["version"], rcb.VERSION)
            self.assertIn("first", [e["msg"] for e in s["events"]])
            rcb.log.info("second")
            _, s2 = self.req("GET", f"/api/status?since={s['event_seq']}", self.AUTH)
            self.assertEqual([e["msg"] for e in s2["events"]], ["second"])
        finally:
            rcb.log.removeHandler(h)

    def test_library(self):
        _, lib = self.req("GET", "/api/library", self.AUTH)
        by = {p["name"]: p for p in lib["projects"]}
        self.assertEqual(by["Feature"]["status"], "ok")
        self.assertEqual(by["Feature"]["folder"], "Client")
        self.assertGreater(by["Feature"]["size"], 0)
        self.assertEqual(by["Old"]["status"], "not_changed_recently")
        self.assertEqual(len(lib["history"]), 1)
        self.assertEqual(lib["storage"]["files"], 1)
        [db] = lib["databases"]
        self.assertEqual((db["DbName"], db["name"], db["projects"], db["folders"]), ("Studio Cloud", "Studio Cloud", 2, ["Client"]))
        self.assertTrue(db["last_checked"])

    def test_library_lists_cloud_libraries_never_opened(self):
        self.logs.mkdir()
        (self.logs / "ResolveDebug.txt").write_text(
            "x | BlackmagicID: PL(9089d4db-5a9c-48cc-abe5-ed5ce2eb0899:'WashOrgSrvrDyson_1':D)\n")
        self.advance(2000)
        self.svc.tick()
        _, lib = self.req("GET", "/api/library", self.AUTH)
        by = {d["name"]: d for d in lib["databases"]}
        self.assertIsNone(by["WashOrgSrvrDyson_1"]["last_checked"])
        self.assertTrue(by["Studio Cloud"]["last_checked"])

    def test_failing_project_reported(self):
        self.pm.add([], "Bad", stamp(self.clock.t, minutes=1))
        self.pm.always_fail.add("Bad")
        self.pm.locked_blocks_export = True
        self.advance(2000)
        self.svc.tick()
        _, lib = self.req("GET", "/api/library", self.AUTH)
        bad = [p for p in lib["projects"] if p["name"] == "Bad"][0]
        self.assertEqual((bad["status"], bad["failures"]), ("failing", 1))
        self.assertTrue(bad["last_error"])

    # ---- controls
    def test_run_now_and_pause_endpoints(self):
        _, r = self.req("POST", "/api/pause", self.AUTH, {"paused": True})
        self.assertTrue(r["paused"])
        self.svc.tick()
        self.assertEqual(self.req("GET", "/api/status", self.AUTH)[1]["state"], "paused")
        self.req("POST", "/api/pause", self.AUTH, {"paused": False})
        self.pm.tree["projects"]["Feature"] = {"mod": stamp(self.clock.t, seconds=1), "collab": False}
        _, r = self.req("POST", "/api/run-now", self.AUTH, {})
        self.assertTrue(r["ok"])
        self.svc.tick()
        self.assertEqual(self.pm.exports.count("Feature"), 2)

    def test_choose_folder_endpoint(self):
        seen = []
        with mock.patch.object(rcb, "choose_folder", lambda start: seen.append(start) or {"path": "/Volumes/B"}):
            code, r = self.req("POST", "/api/choose-folder", self.AUTH, {"start": "/Volumes"})
            self.assertEqual((code, r, seen), (200, {"path": "/Volumes/B"}, ["/Volumes"]))
            code, _ = self.req("POST", "/api/choose-folder", {}, {})
            self.assertEqual(code, 401)  # needs the dashboard token like every other action
        _, st = self.req("GET", "/api/settings", self.AUTH)
        self.assertIn("can_choose_folder", st)

    def test_settings_roundtrip_and_validation(self):
        _, s = self.req("GET", "/api/settings", self.AUTH)
        self.assertEqual(s["settings"]["interval_minutes"], 30)
        self.assertNotIn("alert_command", s["settings"])          # not editable from the browser
        code, r = self.req("POST", "/api/settings", self.AUTH, {"interval_minutes": -1})
        self.assertEqual((code, r["ok"]), (400, False))
        self.assertIn("interval_minutes", r["errors"])
        code, r = self.req("POST", "/api/settings", self.AUTH, {"interval_minutes": 20, "include": ["Client"]})
        self.assertEqual(code, 200)
        on_disk = json.loads(self.cfg_path.read_text())
        self.assertEqual((on_disk["interval_minutes"], on_disk["alert_command"]), (20, "echo keep-me"))
        self.svc.tick()
        self.assertEqual(self.svc.cfg["interval_minutes"], 20)    # hot reloaded
        self.assertEqual(self.req("POST", "/api/settings", self.AUTH, {"alert_command": "x"})[0], 400)

    def test_bad_json_and_oversize(self):
        c = http.client.HTTPConnection("127.0.0.1", self.dash.port)
        c.request("POST", "/api/pause", body="[1,2]", headers={"Host": f"127.0.0.1:{self.dash.port}", **self.AUTH})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 400)
        c = http.client.HTTPConnection("127.0.0.1", self.dash.port)
        c.putrequest("POST", "/api/pause", skip_host=True)
        for k, v in {"Host": f"127.0.0.1:{self.dash.port}", "Content-Length": "999999", **self.AUTH}.items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 400)                       # refused without reading a huge body

    def test_rejected_post_does_not_desync_the_connection(self):
        """A 401 on a POST with a body must not leave the body to be parsed as the next request."""
        c = http.client.HTTPConnection("127.0.0.1", self.dash.port)
        smuggled = f"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{self.dash.port}\r\nX-RCB-Token: {TOKEN}\r\n\r\n"
        c.request("POST", "/api/pause", body=smuggled,
                  headers={"Host": f"127.0.0.1:{self.dash.port}", "Content-Type": "text/plain"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 401)
        c.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{self.dash.port}"})
        r2 = c.getresponse()                                   # next request on the same socket
        r2.read()
        self.assertEqual(r2.status, 401)                       # a plain, unauthenticated GET - not the smuggled one

    # ---- discovery file for the menu bar app / `dashboard` command
    def test_service_json(self):
        info = rcb.read_service_info(self.tmp)
        self.assertEqual((info["port"], info["token"]), (self.dash.port, TOKEN))
        self.assertIn(f"127.0.0.1:{self.dash.port}", info["dashboard"])
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(self.tmp / "service.json").st_mode)
            self.assertEqual(mode, 0o600)
        self.dash.stop()
        self.assertIsNone(rcb.read_service_info(self.tmp))

    def test_token_persists(self):
        p = self.tmp / "tok"
        a = rcb.load_or_create_token(p)
        self.assertEqual(rcb.load_or_create_token(p), a)
        self.assertGreaterEqual(len(a), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
