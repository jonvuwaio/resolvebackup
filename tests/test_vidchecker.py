from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

os.environ["RCB_NO_KEYCHAIN"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import resolve_cloud_backup as rcb  # noqa: E402
import vidchecker as vc  # noqa: E402

rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
_fake_prefs = tempfile.mkdtemp()
rcb.resolve_prefs_dir = lambda: rcb.Path(_fake_prefs)
rcb.start_watchdog = lambda cmd: None

TASK = ("<Task><Id>{id}</Id><Filename>{fn}</Filename><FileSize>100</FileSize><Status>{st}</Status>"
        "<StartedDate>2026-10-15T13:47:43</StartedDate><CompletedDate>2026-10-15T13:48:19</CompletedDate>"
        "<Template>**** Wash - ProRes Check </Template><CheckResult>{res}</CheckResult><PercentComplete>{pc}</PercentComplete>"
        "<AlertCount>3</AlertCount></Task>")


class FakeServer:
    """The bits of Vidchecker's SOAP API the app uses."""

    def __init__(self):
        self.files = {r"W:\Deliverables\Projects\WC105_A_B\Online"}  # folders and files the server can see
        self.started = []
        self.tasks = [(5, r"W:\Deliverables\Projects\WC105_A_B\Online\old.mov", "Complete", "Passed", 100),
                      (6, r"W:\Deliverables\Projects\WC105_A_B\Online\new.mov", "Processing", "", 40)]
        self.down = False

    def __call__(self, url, headers, body, timeout):
        if self.down:
            raise OSError("no route to host")
        action = headers["SOAPAction"].strip('"').rsplit("/", 1)[1]
        b = body.decode()
        wrap = lambda inner: ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                              f'<{action}Response xmlns="http://www.vidcheck.com/services">{inner}</{action}Response>'
                              "</s:Body></s:Envelope>").encode()
        if action == "ListTemplates":
            return 200, wrap("<ListTemplatesResult>" + "".join(
                f"<TemplateRef><Id>{i}</Id><Name>{n}</Name></TemplateRef>"
                for i, n in ((117, "**** Wash - ProRes PSE"), (116, "**** Wash - MXF PSE"), (1, "-  File Info"))) + "</ListTemplatesResult>")
        if action == "GetRoots":
            return 200, wrap("<GetRootsResult>" + "".join(
                f"<File><Filename>{d}</Filename><Info>{i}</Info></File>" for d, i in
                ((r"C:\\", ""), ("Q:\\", r"\\10.0.0.152\WashDeliverables"), ("W:\\", r"\\10.0.0.152\WashDeliverables"),
                 ("U:\\", r"\\10.0.0.20\Colour-5-Raid"))) + "</GetRootsResult>")
        if action == "ListFiles":
            d = re.search(r"<vc:Filename>(.*?)</vc:Filename>", b).group(1).replace("&amp;", "&")
            if d not in self.files:
                return 500, ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
                             "<faultcode>s:x</faultcode><faultstring>File or directory does not exist.</faultstring>"
                             "</s:Fault></s:Body></s:Envelope>").encode()
            kids = [f for f in self.files if f.rsplit("\\", 1)[0] == d]
            return 200, wrap("<ListFilesResult>" + "".join(f"<File><Filename>{f.replace('&', '&amp;')}</Filename></File>" for f in kids) + "</ListFilesResult>")
        if action == "NewTaskInternal":
            self.started.append((re.search(r"<vc:Filename>(.*?)</vc:Filename>", b).group(1),
                                 int(re.search(r"<vc:TemplateId>(\d+)</vc:TemplateId>", b).group(1))))
            return 200, wrap("")
        if action == "GetPaged2":
            return 200, wrap("<GetPaged2Result><Tasks>" + "".join(
                TASK.format(id=i, fn=f, st=s, res=r, pc=p) for i, f, s, r, p in self.tasks) + "</Tasks></GetPaged2Result>")
        raise AssertionError(action)


MOUNTS = {"/Volumes/WashDeliverables": ("washdeliverysvr._smb._tcp.local", "WashDeliverables"),
          "/Volumes/Colour-5-Raid": ("10.0.0.20", "Colour-5-Raid"), "/Volumes/Colour-3-Raid": ("10.0.0.10", "Colour-3-Raid")}


def make(cfg=None, server=None):
    server = server or FakeServer()
    c = cfg if cfg is not None else {"vidchecker": {"url": "http://vc:51060", "templates": [117, 116]}}
    return vc.Vidchecker(lambda: c, http=server, mount_table=lambda: MOUNTS), server


class PathTests(unittest.TestCase):
    def test_mac_paths_become_server_paths(self):
        v, _ = make()
        self.assertEqual(v.server_path("/Volumes/WashDeliverables/Deliverables/Projects/WC105_A_B/x.mov"),
                         r"W:\Deliverables\Projects\WC105_A_B\x.mov")  # of Q: and W:, the same one every time
        self.assertEqual(v.server_path("/Volumes/Colour-5-Raid/Projects/a b.mov"), r"U:\Projects\a b.mov")
        with self.assertRaises(vc.VidcheckerError) as e:
            v.server_path("/Volumes/Colour-3-Raid/Projects/x.mov")
        self.assertIn("doesn't have Colour-3-Raid", str(e.exception))
        with self.assertRaises(vc.VidcheckerError):
            v.server_path("/Users/me/x.mov")

    def test_mount_table(self):
        class R:
            stdout = ("//wash@10.0.0.241/Colour-1-Raid on /Volumes/Colour-1-Raid (smbfs, nodev, nosuid, mounted by wash)\n"
                      "//wash@WashDeliverySvr._smb._tcp.local/Wash%20Transfers on /Volumes/Wash Transfers (smbfs, nodev)\n"
                      "/dev/disk1s1 on /System/Volumes/Data (apfs, local, journaled)\n")
        m = vc.mounts(run=lambda *a, **k: R())
        self.assertEqual(m["/Volumes/Colour-1-Raid"], ("10.0.0.241", "Colour-1-Raid"))
        self.assertEqual(m["/Volumes/Wash Transfers"], ("washdeliverysvr._smb._tcp.local", "Wash Transfers"))
        self.assertEqual(len(m), 2)


class CheckTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)
        self.v, self.server = make()
        # the file has to exist here too: stand in for the share with a real folder
        self.file = self.d / "new & final.mov"
        self.file.write_bytes(b"x")
        self.v.server_path = lambda p: r"W:\Deliverables\Projects\WC105_A_B\Online" + "\\" + Path(p).name

    def test_start(self):
        self.server.files.add(r"W:\Deliverables\Projects\WC105_A_B\Online\new & final.mov")
        r = self.v.start(self.file, 117)
        self.assertEqual(r["template"], "**** Wash - ProRes PSE")
        self.assertEqual(self.server.started, [(r"W:\Deliverables\Projects\WC105_A_B\Online\new &amp; final.mov", 117)])

    def test_only_the_assigned_checks(self):
        self.server.files.add(r"W:\Deliverables\Projects\WC105_A_B\Online\new & final.mov")
        with self.assertRaises(vc.VidcheckerError):
            self.v.start(self.file, 1)
        self.assertEqual(self.server.started, [])

    def test_never_started_on_a_file_the_server_cannot_see(self):
        with self.assertRaises(vc.VidcheckerError) as e:
            self.v.start(self.file, 117)
        self.assertIn("can't see", str(e.exception))
        self.assertEqual(self.server.started, [])

    def test_missing_file(self):
        with self.assertRaises(vc.VidcheckerError):
            self.v.start(self.d / "gone.mov", 117)

    def test_tasks_and_status(self):
        v, _ = make()
        ts = v.tasks()
        self.assertEqual([t["id"] for t in ts], [6, 5])
        self.assertEqual((ts[0]["status"], ts[0]["percent"], ts[1]["result"]), ("Processing", 40, "Passed"))
        self.assertEqual(ts[1]["file"], "/Volumes/WashDeliverables/Deliverables/Projects/WC105_A_B/Online/old.mov")
        st = v.status()
        self.assertTrue(st["ok"])
        self.assertEqual([a["id"] for a in st["assigned"]], [117, 116])

    def test_server_down(self):
        server = FakeServer()
        server.down = True
        v, _ = make(server=server)
        st = v.status()
        self.assertFalse(st["ok"])
        self.assertIn("Couldn't reach Vidchecker", st["problem"])
        v2, _ = make(cfg={})
        self.assertFalse(v2.status()["configured"])

    def test_settings(self):
        clean, errs = vc.validate_config({"url": " http://10.0.0.66:51060/ ", "templates": [117, 117, 116]})
        self.assertEqual((clean, errs), ({"url": "http://10.0.0.66:51060", "templates": [117, 116]}, {}))
        _, errs = vc.validate_config({"url": "ftp://x", "templates": ["117"], "x": 1})
        self.assertEqual(set(errs), {"vidchecker.url", "vidchecker.templates", "vidchecker.x"})

    def test_media_files(self):
        job = self.d / "WC105_A_B"
        (job / "Deliverables/Online").mkdir(parents=True)
        (job / "Deliverables/Online/a.mov").write_bytes(b"1")
        (job / "Deliverables/Online/._a.mov").write_bytes(b"1")
        (job / "Deliverables/Online/notes.txt").write_bytes(b"1")
        self.assertEqual([f["rel"] for f in vc.media_files(job)], ["Deliverables/Online/a.mov"])


class EndpointTests(unittest.TestCase):
    def test_qc_pages_and_guards(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            deliv = d / "Deliverables/Projects"
            (deliv / "WC105_A_B/Online").mkdir(parents=True)
            (deliv / "WC105_A_B/Online/a.mov").write_bytes(b"1")
            (d / "elsewhere.mov").write_bytes(b"1")
            cfg = {"backup_dir": str(d / "out"), "min_free_gb": 0,
                   "vidchecker": {"url": "http://vc:51060", "templates": [117]},
                   "project_setup": {"extras": [{"name": "Deliverables", "template": "Deliverables",
                                                 "path": str(deliv) + "/{job}"}]}}
            (d / "config.json").write_text(json.dumps(cfg))
            svc = rcb.Service(d / "config.json", connect=lambda c: None, state_dir=d)
            server = FakeServer()
            svc.vidchecker.http = server
            svc.vidchecker.mount_table = lambda: {str(d): ("10.0.0.152", "WashDeliverables")}
            dash = rcb.DashboardServer(svc, port=0, state_dir=d)
            dash.start()
            try:
                def call(path, body=None):
                    req = urllib.request.Request(f"http://127.0.0.1:{dash.port}{path}",
                                                 data=None if body is None else json.dumps(body).encode(),
                                                 headers={"X-RCB-Token": dash.token, "Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as r:
                        return json.loads(r.read())
                st = call("/api/vidchecker")
                self.assertEqual((st["ok"], st["jobs"]), (True, ["WC105_A_B"]))
                files = call("/api/vidchecker/files?job=WC105_A_B")["files"]
                self.assertEqual([f["rel"] for f in files], ["Online/a.mov"])
                self.assertEqual(call("/api/vidchecker/files?job=../x")["files"], [])
                server.files.add(r"W:\Deliverables\Projects\WC105_A_B\Online")
                server.files.add(r"W:\Deliverables\Projects\WC105_A_B\Online\a.mov")
                r = call("/api/vidchecker/start", {"files": [files[0]["path"], str(d / "elsewhere.mov")], "template": 117})
                self.assertEqual([x["ok"] for x in r["results"]], [True, False], r)  # only exports in a job folder
                self.assertEqual(server.started, [(r"W:\Deliverables\Projects\WC105_A_B\Online\a.mov", 117)])
                req = urllib.request.Request(f"http://127.0.0.1:{dash.port}/qc?t={dash.token}")
                with urllib.request.urlopen(req) as resp:
                    self.assertIn(b'id="qcPage"', resp.read())
            finally:
                dash.stop()


if __name__ == "__main__":
    unittest.main()
