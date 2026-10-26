from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

os.environ["RCB_NO_KEYCHAIN"] = "1"  # never touch the real keychain
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import frameio  # noqa: E402
import project_setup as ps  # noqa: E402
import resolve_cloud_backup as rcb  # noqa: E402

rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
_fake_prefs = tempfile.mkdtemp()  # and never edit Resolve's real preferences
rcb.resolve_prefs_dir = lambda: rcb.Path(_fake_prefs)
rcb.start_watchdog = lambda cmd: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_setup import FIELDS, Rig  # noqa: E402

ACC, WS = "acc-1", "ws-1"


class FakeFrameIO:
    """Adobe sign-in plus the bits of the Frame.io V4 API the app uses."""

    def __init__(self):
        self.calls = []
        self.codes = {"good"}
        self.refresh_ok = True
        self.n = 0
        self.projects = {}   # id -> {name, root_folder_id}
        self.folders = {}    # id -> (parent id, name, project id)
        self.fail_folder_at = None
        self.fail_delete = False
        self.throttle_once = False
        self.reject_token_once = False

    def _id(self, kind):
        self.n += 1
        return f"{kind}-{self.n}"

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        self.calls.append((method, u.path, headers, body))
        if url.startswith(frameio.TOKEN_URL):
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form.get("grant_type") == "authorization_code" and form.get("code") in self.codes:
                self.codes.discard(form["code"])
                return 200, {}, {"access_token": self._id("at"), "refresh_token": "rt-1", "expires_in": 3600}
            if form.get("grant_type") == "refresh_token" and self.refresh_ok and form.get("refresh_token") == "rt-1":
                return 200, {}, {"access_token": self._id("at"), "expires_in": 3600}
            return 400, {}, {"error": "invalid_grant", "error_description": "nope"}
        if u.netloc != "api.frame.io":
            raise AssertionError(url)
        if self.reject_token_once:
            self.reject_token_once = False
            return 401, {}, {"errors": [{"detail": "expired"}]}
        if self.throttle_once:
            self.throttle_once = False
            return 429, {"Retry-After": "3"}, {"errors": [{"detail": "slow down"}]}
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        data = json.loads(body) if body else None
        if p == "/v4/me":
            return 200, {}, {"data": {"name": "Jon", "email": "jon@example.com"}}
        if p == "/v4/accounts":
            return 200, {}, {"data": [{"id": ACC, "display_name": "Wash"}], "links": {"next": None}}
        if p == f"/v4/accounts/{ACC}/workspaces":  # two pages
            if q.get("after"):
                return 200, {}, {"data": [{"id": "ws-2", "name": "Archive"}], "links": {"next": None}}
            return 200, {}, {"data": [{"id": WS, "name": "Jobs"}],
                             "links": {"next": f"/v4/accounts/{ACC}/workspaces?after=xyz"}}
        if p == f"/v4/accounts/{ACC}/workspaces/{WS}/projects":
            if method == "GET":
                return 200, {}, {"data": [{"id": k, "name": v["name"], "view_url": f"https://frame/{k}"}
                                          for k, v in self.projects.items()], "links": {"next": None}}
            pid, root = self._id("p"), self._id("f")
            self.projects[pid] = {"name": data["data"]["name"], "root_folder_id": root}
            return 201, {}, {"data": {"id": pid, "root_folder_id": root, "name": data["data"]["name"],
                                      "view_url": f"https://frame/{pid}"}}
        if p.startswith(f"/v4/accounts/{ACC}/folders/") and method == "POST":
            parent = p.split("/")[5]
            proj = next((k for k, v in self.projects.items() if v["root_folder_id"] == parent), None) or \
                (self.folders.get(parent) or (None, None, None))[2]
            if proj is None:
                return 404, {}, {"errors": [{"detail": "no such folder"}]}
            if self.fail_folder_at is not None and len(self.folders) == self.fail_folder_at:
                return 422, {}, {"errors": [{"detail": "folder refused"}]}
            fid = self._id("f")
            self.folders[fid] = (parent, data["data"]["name"], proj)
            return 201, {}, {"data": {"id": fid}}
        if p.startswith(f"/v4/accounts/{ACC}/projects/") and method == "DELETE":
            if self.fail_delete:
                return 500, {}, {"errors": [{"detail": "boom"}]}
            pid = p.rsplit("/", 1)[1]
            self.projects.pop(pid, None)
            self.folders = {k: v for k, v in self.folders.items() if v[2] != pid}
            return 204, {}, None
        return 404, {}, {"errors": [{"detail": "not found"}]}

    def tree(self, pid):
        """The project's folders as paths."""
        root = self.projects[pid]["root_folder_id"]
        names = {}

        def path(fid):
            parent, name, _ = self.folders[fid]
            return name if parent == root else path(parent) + "/" + name
        for fid, (_, _, proj) in self.folders.items():
            if proj == pid:
                names[path(fid)] = fid
        return sorted(names)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(d: Path, fake: FakeFrameIO, cfg: dict | None = None, secret: str | None = "s3cret"):
    cfg = cfg if cfg is not None else {"project_setup": {"frameio": {
        "enabled": True, "client_id": "abcdef123456", "account_id": ACC, "workspace_id": WS, "workspace": "Jobs"}}}
    kc = frameio.Keychain()
    if secret:
        kc.set("client_secret", secret)
    slept = []
    f = frameio.FrameIO(lambda: cfg, d, keychain=kc, http=fake, clock=Clock(), sleep=slept.append)
    f.slept = slept
    return f


class KeychainTests(unittest.TestCase):
    def test_values_never_go_on_the_command_line(self):
        runs = []

        class R:
            returncode, stdout = 0, "tok.en-1"

        kc = frameio.Keychain(run=lambda args, **kw: runs.append((args, kw)) or R())
        kc._mem = None  # as on a real Mac, but with security(1) faked
        self.assertTrue(kc.set("refresh_token", "tok.en-1"))
        args, kw = runs[0]
        self.assertEqual(args, ["/usr/bin/security", "-i"])
        self.assertIn('-w "tok.en-1"', kw["input"])
        self.assertNotIn("tok.en-1", " ".join(a for a, _ in runs for a in a if a != "-w"))
        with self.assertRaises(ValueError):
            kc.set("client_secret", 'a" ; delete-keychain x')

    def test_tests_use_memory(self):
        kc = frameio.Keychain()
        self.assertIsNotNone(kc._mem)


class SignInTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)
        self.fake = FakeFrameIO()

    def test_login_url_and_finish(self):
        f = make(self.d, self.fake)
        url = f.login_url("http://localhost:8765/frameio/callback")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertTrue(url.startswith(frameio.AUTHORIZE_URL))
        self.assertEqual(q["client_id"], "abcdef123456")
        self.assertEqual(q["redirect_uri"], "http://localhost:8765/frameio/callback")
        self.assertIn("offline_access", q["scope"])
        self.assertNotIn("code_challenge", q)  # a Web App credential uses its secret instead
        with self.assertRaises(frameio.FrameIOError):
            f.finish_login("good", "wrong-state")
        self.assertEqual(f.finish_login("good", q["state"]), "Jon (jon@example.com)")
        form = dict(urllib.parse.parse_qsl(self.fake.calls[0][3].decode()))
        self.assertEqual(form["client_secret"], "s3cret")
        self.assertEqual(form["redirect_uri"], "http://localhost:8765/frameio/callback")
        self.assertTrue(f.connected())
        st = f.status()
        self.assertEqual((st["connected"], st["user"], st["secret"]), (True, "Jon (jon@example.com)", True))
        self.assertNotIn("rt-1", (self.d / "frameio.json").read_text())  # the token is only in the keychain
        with self.assertRaises(frameio.FrameIOError):  # each sign-in link works once
            f.finish_login("good", q["state"])

    def test_requests_name_the_app(self):
        seen = {}

        class R:
            status, headers = 200, {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def urlopen(req, **kw):
            seen.update(req.header_items())
            return R()
        with mock.patch("urllib.request.urlopen", urlopen):
            frameio.http_request("GET", "https://api.frame.io/v4/me", {"Accept": "application/json"}, None)
        self.assertEqual(seen.get("User-agent"), frameio.USER_AGENT)  # Python's default is refused with a 403

    def test_without_a_secret_it_wont_start(self):
        f = make(self.d, self.fake, secret=None)
        with self.assertRaisesRegex(frameio.FrameIOError, "Client Secret"):
            f.login_url("http://localhost:1/cb")

    def test_expired_sign_in_link(self):
        f = make(self.d, self.fake)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(f.login_url("http://localhost:1/cb")).query))
        f.clock.t += frameio.LOGIN_TTL + 1
        with self.assertRaises(frameio.FrameIOError):
            f.finish_login("good", q["state"])

    def test_refresh_and_signed_out(self):
        f = make(self.d, self.fake)
        f.keychain.set("refresh_token", "rt-1")
        f.workspaces()
        f.workspaces()
        refreshes = [c for c in self.fake.calls if c[1] == "/ims/token/v3"]
        self.assertEqual(len(refreshes), 1)  # the access token is reused while it's valid
        f.clock.t += 3600
        f.workspaces()
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/ims/token/v3"]), 2)
        f.clock.t += 3600
        self.fake.refresh_ok = False
        with self.assertRaises(frameio.NotConnected):
            f.workspaces()
        self.assertFalse(f.connected())

    def test_not_configured_does_not_touch_the_keychain(self):
        class Boom(frameio.Keychain):
            def get(self, name):
                raise AssertionError("keychain used")
        f = frameio.FrameIO(lambda: {}, self.d, keychain=Boom(), http=self.fake)
        self.assertFalse(f.status()["connected"])


class ApiCallTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.fake = FakeFrameIO()
        self.f = make(Path(t.name), self.fake)
        self.f.keychain.set("refresh_token", "rt-1")

    def test_workspaces_follow_pages(self):
        self.assertEqual([w["workspace"] for w in self.f.workspaces()], ["Jobs", "Archive"])
        pages = [c for c in self.fake.calls if "workspaces" in c[1]]
        self.assertEqual(len(pages), 2)

    def test_a_revoked_access_token_is_refreshed_once(self):
        self.f.workspaces()
        self.fake.reject_token_once = True
        self.f.workspaces()
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/ims/token/v3"]), 2)

    def test_throttling_waits_and_retries(self):
        self.fake.throttle_once = True
        self.f.workspaces()
        self.assertIn(3.0, self.f.slept)

    def test_make_project_with_folders(self):
        tree = ["Grade", "Online", "VFX", "VFX/FROM_VFX", "VFX/TO_VFX"]
        r = self.f.make_project("WC105_A_B", tree)
        self.assertEqual(r["folders"], 5)
        self.assertEqual(r["url"], f"https://frame/{r['id']}")
        self.assertEqual(self.fake.tree(r["id"]), sorted(tree))
        self.assertTrue(all(s == frameio.FOLDER_GAP for s in self.f.slept))  # kept under 3 a second
        with self.assertRaises(frameio.FrameIOError):
            self.f.make_project("WC105_A_B", tree)  # never a second project with the same name

    def test_failure_part_way_removes_the_project(self):
        self.fake.fail_folder_at = 2
        with self.assertRaises(frameio.FrameIOError) as e:
            self.f.make_project("WC105_A_B", ["Grade", "Online", "VFX"])
        self.assertIn("folder refused", str(e.exception))
        self.assertEqual(self.fake.projects, {})

    def test_failure_that_cant_be_taken_back_says_so(self):
        self.fake.fail_folder_at = 0
        self.fake.fail_delete = True
        with self.assertRaises(frameio.FrameIOError) as e:
            self.f.make_project("WC105_A_B", ["Grade"])
        self.assertIn("delete it in Frame.io", str(e.exception))


class FrameIOConfigTests(unittest.TestCase):
    def test_valid_and_problems(self):
        clean, errs = ps.validate_config({"frameio": {"enabled": True, "client_id": "abcdef123456", "account_id": ACC,
                                                      "workspace_id": WS, "workspace": "Jobs"}})
        self.assertEqual(errs, {})
        self.assertEqual((clean["frameio"]["template"], clean["frameio"]["project"]), ("FrameIO", "{job}"))
        _, errs = ps.validate_config({"frameio": {"enabled": True, "client_id": "x y", "project": "{nope}/a"}})
        self.assertEqual(set(errs), {"project_setup.frameio.client_id", "project_setup.frameio.workspace_id",
                                     "project_setup.frameio.project"})
        clean, errs = ps.validate_config({"frameio": {"enabled": False, "client_id": "abcdef123456"}})
        self.assertEqual(errs, {})  # saving the Client ID before connecting


class SetupWithFrameIOTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)
        self.rig = Rig(self.d)
        for x in ("Grade", "Online", "VFX/TO_VFX", "VFX/FROM_VFX"):
            (self.rig.vol / "WashDeliverables/FolderStructures/FrameIO" / x).mkdir(parents=True)
        self.rig.cfg["project_setup"]["frameio"] = {"enabled": True, "client_id": "abcdef123456", "account_id": ACC,
                                                    "workspace_id": WS, "workspace": "Jobs"}
        self.fake = FakeFrameIO()
        self.f = make(self.d, self.fake, cfg=self.rig.cfg)
        self.s = self.rig.setup
        self.s.frameio = self.f
        self.job_dir = self.rig.vol / "Colour-3-Raid/Projects/SaatchiSaatchi/WC105_Saatchi_Plusnet_Plusso"

    def row(self, plan):
        return next(r for r in plan["locations"] if r["kind"] == "frameio")

    def test_not_connected_blocks_the_job(self):
        p = self.s.plan(FIELDS, 0)
        self.assertEqual(self.row(p)["status"], "not_connected")
        self.assertFalse(p["can_create"])
        self.assertFalse(self.s.create(FIELDS, 0)["ok"])
        self.assertFalse(self.job_dir.exists())

    def test_no_row_when_off(self):
        self.rig.cfg["project_setup"]["frameio"]["enabled"] = False
        self.assertFalse(any(r["kind"] == "frameio" for r in self.s.plan(FIELDS, 0)["locations"]))

    def test_found_under_another_name(self):
        self.f.keychain.set("refresh_token", "rt-1")
        self.fake.projects["p-old"] = {"name": "wc105 Saatchi Plusso", "root_folder_id": "r-old"}
        self.fake.projects["p-other"] = {"name": "WC1050_Other", "root_folder_id": "r-x"}  # another number
        p = self.s.plan(FIELDS, 0)
        row = self.row(p)
        self.assertEqual((row["status"], row["path"]), ("found", "Jobs / wc105 Saatchi Plusso"))
        self.assertTrue(p["can_create"])  # the drive folders are still made
        self.s.create(FIELDS, 0)
        self.assertEqual(len(self.fake.projects), 2)  # no second Frame.io project
        del self.fake.projects["p-old"]
        self.f._cache.clear()
        self.s._scan_cache = None
        self.assertEqual(self.row(self.s.plan({**FIELDS, "job": "WC106_Saatchi_New"}, 0))["status"], "ready")

    def test_plan_and_create(self):
        self.f.keychain.set("refresh_token", "rt-1")
        p = self.s.plan(FIELDS, 0)
        row = self.row(p)
        self.assertEqual((row["status"], row["folders"], row["path"]), ("ready", 5, "Jobs / WC105_Saatchi_Plusnet_Plusso"))
        self.assertTrue(p["can_create"])
        r = self.s.create(FIELDS, 0)
        self.assertTrue(r["ok"], r)
        fio = r["results"][-1]
        self.assertEqual((fio["kind"], fio["ok"], fio["folders"]), ("frameio", True, 5))
        pid = fio["id"]
        self.assertEqual(self.fake.projects[pid]["name"], "WC105_Saatchi_Plusnet_Plusso")
        self.assertEqual(self.fake.tree(pid), ["Grade", "Online", "VFX", "VFX/FROM_VFX", "VFX/TO_VFX"])
        self.assertTrue(self.job_dir.is_dir())
        self.assertEqual(self.s.recent(1)[0]["locations"][-1]["url"], f"https://frame/{pid}")
        p = self.s.plan(FIELDS, 0)  # made: shows as already there, with a link
        self.assertEqual(self.row(p)["status"], "exists")
        self.assertEqual(self.row(p)["url"], f"https://frame/{pid}")

    def test_existing_frameio_project_is_left_alone(self):
        self.f.keychain.set("refresh_token", "rt-1")
        self.fake.projects["p-0"] = {"name": "WC105_Saatchi_Plusnet_Plusso", "root_folder_id": "f-0"}
        r = self.s.create(FIELDS, 0)
        self.assertTrue(r["ok"], r)
        self.assertFalse(any(x["kind"] == "frameio" for x in r["results"]))
        self.assertEqual(len(self.fake.projects), 1)
        self.assertTrue(self.job_dir.is_dir())

    def test_frameio_failure_takes_back_the_folders(self):
        self.f.keychain.set("refresh_token", "rt-1")
        self.rig.cfg["project_setup"]["resolve"] = {"enabled": True, "library": "WashOrgSrvrDB_1",
                                                    "template": "Project_Template/ProjectTemplate"}
        self.fake.fail_folder_at = 1
        r = self.s.create(FIELDS, 0)
        self.assertFalse(r["ok"])
        self.assertIn("folder refused", r["results"][-1]["error"])
        self.assertTrue(all("another part failed" in x["error"] for x in r["results"][:-1]))
        self.assertFalse(self.job_dir.exists())
        self.assertEqual(self.fake.projects, {})
        self.assertIsNone(r["resolve"])  # and no Resolve project
        self.assertFalse(self.s.has_pending())

    def test_folder_failure_never_reaches_frameio(self):
        self.f.keychain.set("refresh_token", "rt-1")
        real = self.s.mkdir
        self.s.mkdir = lambda p: (_ for _ in ()).throw(PermissionError()) if "Deliverables" in str(p) else real(p)
        r = self.s.create(FIELDS, 0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["results"][-1]["error"], "Not made, because another part failed.")
        self.assertFalse([c for c in self.fake.calls if c[0] == "POST" and c[1].startswith("/v4/")])

    def test_frameio_unreachable_blocks(self):
        self.f.keychain.set("refresh_token", "rt-1")

        def down(*a):
            raise OSError("no network")
        self.f.http = down
        p = self.s.plan(FIELDS, 0)
        self.assertEqual(self.row(p)["status"], "unreachable")
        self.assertIn("Couldn't reach", self.row(p)["message"])
        self.assertFalse(p["can_create"])


class FrameIOEndpointTests(unittest.TestCase):
    def test_sign_in_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = {"backup_dir": str(d / "out"), "min_free_gb": 0,
                   "project_setup": {"frameio": {"enabled": False, "client_id": "abcdef123456"}}}
            (d / "config.json").write_text(json.dumps(cfg))
            svc = rcb.Service(d / "config.json", connect=lambda c: None, state_dir=d)
            fake = FakeFrameIO()
            svc.frameio.http = fake
            dash = rcb.DashboardServer(svc, port=0, state_dir=d)
            dash.start()
            base = f"http://127.0.0.1:{dash.port}"
            try:
                def call(path, body=None, token=True):
                    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                                 headers={"X-RCB-Token": dash.token if token else "",
                                                          "Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as r:
                        return json.loads(r.read())
                self.assertEqual(call("/api/project-setup")["frameio_redirect"],
                                 f"http://localhost:{dash.port}/frameio/callback")
                self.assertTrue(call("/api/frameio/secret", {"secret": "s3cret"})["ok"])
                self.assertFalse(call("/api/frameio/secret", {"secret": "has space"})["ok"])
                self.assertEqual(svc.frameio.keychain.get("client_secret"), "s3cret")
                with self.assertRaises(urllib.error.HTTPError) as e:  # the dashboard's token is still needed
                    call("/api/frameio/login", {}, token=False)
                self.assertEqual(e.exception.code, 401)
                url = call("/api/frameio/login", {})["url"]
                state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
                with self.assertRaises(urllib.error.HTTPError) as e:  # a made-up state is refused
                    urllib.request.urlopen(f"{base}/frameio/callback?code=good&state=forged")
                self.assertEqual(e.exception.code, 400)
                self.assertFalse(svc.frameio.connected())
                req = urllib.request.Request(f"{base}/frameio/callback?code=good&state={state}",
                                             headers={"Host": f"localhost:{dash.port}"})
                with urllib.request.urlopen(req) as r:
                    page = r.read().decode()
                self.assertIn("connected as Jon", page)
                st = call("/api/project-setup")["frameio"]
                self.assertTrue(st["connected"])
                ws = call("/api/frameio/workspaces")
                self.assertEqual([w["workspace"] for w in ws["workspaces"]], ["Jobs", "Archive"])
                req = urllib.request.Request(f"{base}/frameio/callback?code=good&state=x",
                                             headers={"Host": "evil.example:80"})
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(req)
                self.assertEqual(e.exception.code, 403)
                self.assertTrue(call("/api/frameio/disconnect", {})["ok"])
                self.assertFalse(call("/api/project-setup")["frameio"]["connected"])
            finally:
                dash.stop()


if __name__ == "__main__":
    unittest.main()
