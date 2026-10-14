import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import menubar as mb  # noqa: E402  (must import without rumps installed)


def status(state="idle", **kw):
    d = {"state": state, "paused": state == "paused", "cycle": None, "next_run_at": "2026-10-13T16:08:00",
         "last_cycle": {"at": "2026-10-13T15:37:34", "status": "ok", "exported": 7, "failed": 0, "failing": 0},
         "resolve": {"connected": True}, "config": {"active_hours": None, "backup_dir": "/x"}}
    d.update(kw)
    return d


class SummarizeTests(unittest.TestCase):
    def test_up_to_date(self):
        s = mb.summarize(status())
        self.assertEqual((s["key"], s["colour"], s["title"]), ("ok", "ok", "Up to date"))
        self.assertIn("7 backed up", s["detail"])
        self.assertIn("next at 16:08", s["detail"])

    def test_offline_and_denied(self):
        self.assertEqual(mb.summarize(None, offline=True)["key"], "off")
        self.assertEqual(mb.summarize(None, offline=True)["can_run"], False)
        self.assertEqual(mb.summarize(None, denied=True)["key"], "denied")

    def test_running_shows_current_project_and_progress(self):
        s = mb.summarize(status("running", cycle={"current": "Promo Cut", "seen": 3, "expected": 9, "phase": "exporting"}))
        self.assertEqual((s["key"], s["colour"]), ("run", "run"))
        self.assertIn("Promo Cut", s["detail"])
        self.assertIn("3 of 9", s["detail"])
        self.assertFalse(s["can_run"])
        self.assertTrue(s["can_pause"])

    def test_paused_waiting_resting(self):
        self.assertEqual(mb.summarize(status("paused"))["title"], "Paused")
        self.assertTrue(mb.summarize(status("paused"))["paused"])
        w = mb.summarize(status("waiting_for_resolve"))
        self.assertEqual(w["colour"], "wait")
        self.assertIn("16:08", w["detail"])
        r = mb.summarize(status("outside_active_hours", config={"active_hours": ["07:00", "22:00"]}))
        self.assertEqual(r["title"], "Resting until 07:00")

    def test_failures_are_shown(self):
        last = {"at": "2026-10-13T15:37:34", "status": "ok", "exported": 7, "failed": 1, "failing": 1}
        s = mb.summarize(status(last_cycle=last))
        self.assertEqual((s["key"], s["colour"]), ("bad", "bad"))
        self.assertEqual(s["title"], "1 project couldn't be backed up")
        self.assertEqual(s["bad_cycle"], "2026-10-13T15:37:34")

    def test_project_in_backoff_still_counts_as_failing(self):
        # this cycle skipped it (retry back-off) so `failed` is 0 but `failing` is not
        last = {"at": "2026-10-13T15:39:00", "status": "ok", "exported": 0, "failed": 0, "failing": 2}
        s = mb.summarize(status(last_cycle=last))
        self.assertEqual(s["key"], "bad")
        self.assertEqual(s["title"], "2 projects couldn't be backed up")
        self.assertIsNone(s["bad_cycle"])  # not a new failure, so no repeat notification

    def test_resolve_unreachable_is_not_an_alarm(self):
        last = {"at": "2026-10-13T15:37:34", "status": "resolve_unavailable", "failed": 0}
        self.assertEqual(mb.summarize(status(last_cycle=last))["key"], "ok")

    def test_disk_problem_is_shown_by_name(self):
        last = {"at": "2026-10-13T15:37:34", "status": "low_disk", "failed": 0}
        s = mb.summarize(status(last_cycle=last))
        self.assertEqual(s["key"], "bad")
        self.assertEqual(s["title"], "Backup disk almost full")

    def test_starting(self):
        self.assertEqual(mb.summarize(status("starting", last_cycle=None))["key"], "start")


class AlertTests(unittest.TestCase):
    def bad(self, at):
        return mb.summarize(status(last_cycle={"at": at, "status": "ok", "failed": 1, "failing": 1}))

    def test_first_status_never_alerts(self):
        a = mb.Alerts()
        self.assertIsNone(a.check(self.bad("t1")))

    def test_alerts_once_per_new_bad_cycle(self):
        a = mb.Alerts()
        a.check(mb.summarize(status()))
        self.assertIsNotNone(a.check(self.bad("t2")))
        self.assertIsNone(a.check(self.bad("t2")))
        self.assertIsNotNone(a.check(self.bad("t3")))

    def test_running_state_does_not_reset(self):
        a = mb.Alerts()
        a.check(mb.summarize(status()))
        a.check(self.bad("t2"))
        a.check(mb.summarize(status("running", cycle={})))
        self.assertIsNone(a.check(self.bad("t2")))


class IconTests(unittest.TestCase):
    def decode(self, png):
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        pos, idat, hdr = 8, b"", None
        while pos < len(png):
            n, tag = struct.unpack(">I4s", png[pos:pos + 8])
            body = png[pos + 8:pos + 8 + n]
            crc = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0]
            self.assertEqual(crc, zlib.crc32(tag + body) & 0xFFFFFFFF)
            if tag == b"IHDR":
                hdr = struct.unpack(">IIBBBBB", body)
            elif tag == b"IDAT":
                idat += body
            pos += 12 + n
        return hdr, zlib.decompress(idat)

    def test_png_is_valid_rgba_circle(self):
        hdr, raw = self.decode(mb.icon_png((10, 20, 30), 22))
        self.assertEqual(hdr[:2], (22, 22))
        self.assertEqual(hdr[3], 6)
        stride = 1 + 22 * 4
        px = lambda x, y: raw[y * stride + 1 + x * 4: y * stride + 1 + x * 4 + 4]
        self.assertEqual(px(11, 11), bytes((10, 20, 30, 255)))  # centre solid
        self.assertEqual(px(0, 0)[3], 0)                        # corner transparent

    def test_write_icons_makes_1x_and_2x_for_every_colour(self):
        with tempfile.TemporaryDirectory() as d:
            paths = mb.write_icons(Path(d))
            self.assertEqual(set(paths), set(mb.COLOURS))
            for name, p in paths.items():
                self.assertTrue(p.exists())
                self.assertTrue((Path(d) / f"dot-{name}@2x.png").exists())
            self.assertEqual({mb.summarize(status(s))["colour"] for s in ("running", "paused", "waiting_for_resolve")}
                             <= set(mb.COLOURS), True)


class ClientTests(unittest.TestCase):
    def test_missing_service_json_is_offline(self):
        with tempfile.TemporaryDirectory() as d:
            st, off, den = mb.ServiceClient(Path(d)).poll()
            self.assertEqual((st, off, den), (None, True, False))
            self.assertIsNone(mb.ServiceClient(Path(d)).dashboard_url())

    def test_talks_to_the_real_dashboard(self):
        import datetime as dt
        sys.path.insert(0, str(ROOT / "tests"))
        from test_backup import FakePM, FakeResolve, FMT
        import resolve_cloud_backup as rcb
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps({"backup_dir": str(d / "out"), "min_free_gb": 0,
                                                       "dashboard": {"enabled": True, "port": 8795}}))
            pm = FakePM()
            pm.add([], "A", dt.datetime.now().strftime(FMT))
            svc = rcb.Service(d / "config.json", connect=lambda c: FakeResolve(pm), state_dir=d)
            dash = rcb.DashboardServer(svc, port=8795, state_dir=d)
            dash.start()
            try:
                c = mb.ServiceClient(d)
                self.assertTrue(c.dashboard_url().startswith("http://127.0.0.1:"))
                st, off, den = c.poll()
                self.assertEqual((off, den), (False, False))
                self.assertIn(mb.summarize(st)["key"], ("start",))
                self.assertTrue(c.post("/api/pause", {"paused": True})["paused"])
                self.assertEqual(mb.summarize(c.poll()[0])["key"], "paused")
                # wrong token is reported as denied, not offline
                info = json.loads((d / "service.json").read_text())
                info["token"] = "nope"
                (d / "service.json").write_text(json.dumps(info))
                self.assertEqual(c.poll(), (None, False, True))
            finally:
                dash.stop()


class DashboardHtmlTests(unittest.TestCase):
    html = (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_no_external_resources_or_unsafe_dom(self):
        self.assertNotRegex(self.html, r"(src|href)=[\"']https?://")
        self.assertNotIn("innerHTML", self.html)
        self.assertNotIn("eval(", self.html)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_inline_script_parses(self):
        js = re.findall(r"<script>(.*?)</script>", self.html, re.S)[-1]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "dash.js"
            f.write_text(js)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
