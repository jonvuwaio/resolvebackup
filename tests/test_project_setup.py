import datetime as dt
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os  # noqa: E402
os.environ.setdefault("RCB_NO_KEYCHAIN", "1")  # never touch the real keychain
import project_setup as ps  # noqa: E402
import resolve_cloud_backup as rcb  # noqa: E402

rcb.HiddenResolve.available = staticmethod(lambda cfg: False)
_fake_prefs = __import__("tempfile").mkdtemp()  # and never edit Resolve's real preferences
rcb.resolve_prefs_dir = lambda: rcb.Path(_fake_prefs)
rcb.start_watchdog = lambda cmd: None  # nor start a real watchdog process

FIELDS = {"job_no": "WC105", "client": "Saatchi", "client_folder": "SaatchiSaatchi", "title": "Plusnet_Plusso"}
TODAY = dt.datetime(2026, 10, 25, 14, 0)


class Rig:
    """A fake /Volumes with two drives and a templates folder."""

    def __init__(self, d: Path):
        self.vol = d / "Volumes"
        for v in ("Colour-3-Raid/Projects/AaronWheeler/WC095_AaronWheeler_ByFeel", "WashDeliverables/Deliverables/Projects",
                  "WashDeliverables/FolderStructures/Project/GRADE/SOURCE", "WashDeliverables/FolderStructures/Project/ONLINE",
                  "WashDeliverables/FolderStructures/Project/.hidden", "WashDeliverables/FolderStructures/Deliverables/WIPs",
                  "WashDeliverables/FolderStructures/Project/GRADE/{job}_REFS", "Wash-Raid-6/Projects"):
            (self.vol / v).mkdir(parents=True)
        (self.vol / "WashDeliverables/FolderStructures/.DS_Store").write_text("")
        self.cfg = {"project_setup": {
            "templates_dir": str(self.vol / "WashDeliverables/FolderStructures"),
            "drives": [{"name": "Colour-3-Raid", "template": "Project",
                        "path": str(self.vol / "Colour-3-Raid/Projects/{client_folder}/{job}")},
                       {"name": "Colour-9-Raid", "template": "Project",
                        "path": str(self.vol / "Colour-9-Raid/Projects/{client_folder}/{job}")}],
            "extras": [{"name": "Deliverables", "template": "Deliverables", "default_on": False,
                        "path": str(self.vol / "WashDeliverables/Deliverables/Projects/{job}")}]}}
        self.names = ["db_x::LadBible/WC084_Ladbible_GoogleGemini/WC084_Ladbible_GoogleGemini_CV1",
                      "db_x::WSMLondon/WC051_WSM_Varley/WC051_WSM_Varley_GV1",
                      "db_x::WSMLondon/WB993_WSM_Old/WB993_WSM_Old_GV1"]
        self.setup = ps.ProjectSetup(lambda: self.cfg, d, known_names=lambda: self.names, volumes=self.vol,
                                     clock=lambda: TODAY)


class NamingTests(unittest.TestCase):
    def test_next_job_number(self):
        self.assertEqual(ps.next_job_number(["WC104_A_B", "WB999_C_D", "WA001_x"]), "WC105")
        self.assertEqual(ps.next_job_number(["WB999_A_B"]), "WC000")
        self.assertEqual(ps.next_job_number(["Promo", "Default Project"]), None)
        self.assertEqual(ps.next_job_number(["WC104_A", "X1_odd"]), "WC105")

    def test_fields_are_checked(self):
        self.assertEqual(ps.validate_fields(FIELDS), {})
        bad = ps.validate_fields({"job_no": "wc105", "client": "Saatchi Saatchi", "client_folder": "a/b", "title": ""})
        self.assertEqual(set(bad), {"job_no", "client", "client_folder", "title"})
        self.assertIn("client", ps.validate_fields({**FIELDS, "client": "Saatchi_Saatchi"}))
        self.assertEqual(ps.validate_fields({**FIELDS, "client_folder": "Ana Projects & Co"}), {})

    def test_placeholders(self):
        v = ps.job_values(FIELDS, dt.date(2026, 3, 4))
        self.assertEqual(v["job"], "WC105_Saatchi_Plusnet_Plusso")
        self.assertEqual(ps.fill("/x/{client_folder}/{job}/{yyyy}{mm}{dd}_{nope}", v),
                         "/x/SaatchiSaatchi/WC105_Saatchi_Plusnet_Plusso/20260304_{nope}")


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        clean, errs = ps.validate_config({"templates_dir": "/Volumes/D/FolderStructures/",
                                          "drives": [{"name": " C3 ", "template": "Project",
                                                      "path": "/Volumes/C3/Projects/{client_folder}/{job}/"}],
                                          "extras": []})
        self.assertEqual(errs, {})
        self.assertEqual(clean["templates_dir"], "/Volumes/D/FolderStructures")
        self.assertEqual(clean["drives"], [{"name": "C3", "template": "Project",
                                            "path": "/Volumes/C3/Projects/{client_folder}/{job}"}])

    def test_problems(self):
        _, errs = ps.validate_config({"templates_dir": "relative", "drives": [
            {"name": "A", "template": "Project", "path": "Projects/{job}"},
            {"name": "B", "template": "Project", "path": "/Volumes/B/Projects"},
            {"name": "C", "template": "Project", "path": "/Volumes/C/{client}/{oops}/{job}"},
            {"name": "c", "template": "", "path": "/Volumes/C/../{job}"}],
            "extras": [{"name": "E", "template": "D", "path": "/Volumes/E/{job}"}], "x": 1})
        self.assertEqual(set(errs), {"project_setup.templates_dir", "project_setup.drives.0.path",
                                     "project_setup.drives.1.path", "project_setup.drives.2.path",
                                     "project_setup.drives.3.name", "project_setup.drives.3.template",
                                     "project_setup.drives.3.path", "project_setup.x"})
        self.assertIn("{oops}", errs["project_setup.drives.2.path"])

    def test_dashboard_settings_save_it(self):
        clean, errs = rcb.validate_settings({"project_setup": {"drives": []}})
        self.assertEqual((clean, errs), ({"project_setup": {"drives": []}}, {}))
        _, errs = rcb.validate_settings({"project_setup": {"drives": [{}]}})
        self.assertIn("project_setup.drives.0.name", errs)


class ResolveConfigTests(unittest.TestCase):
    def test_valid_and_defaults(self):
        clean, errs = ps.validate_config({"resolve": {"enabled": True, "library": "WashOrgSrvrDB_1",
                                                      "template": "/Project_Template/ProjectTemplate/",
                                                      "folder": "{client_folder}/{job}", "project": "{job}_CV1"}})
        self.assertEqual(errs, {})
        self.assertEqual(clean["resolve"]["template"], "Project_Template/ProjectTemplate")
        clean, errs = ps.validate_config({"resolve": {"enabled": False}})
        self.assertEqual(errs, {})

    def test_problems(self):
        _, errs = ps.validate_config({"resolve": {"enabled": True, "library": "", "template": "A//B",
                                                  "folder": "{client_folder}/{nope}", "project": "x/{job}"}})
        self.assertEqual(set(errs), {"project_setup.resolve.library", "project_setup.resolve.template",
                                     "project_setup.resolve.folder", "project_setup.resolve.project"})


class SetupTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name)
        self.rig = Rig(self.d)
        self.s = self.rig.setup

    def test_templates_skip_hidden_and_junk(self):
        t, problem = self.s.templates()
        self.assertIsNone(problem)
        self.assertEqual(sorted(t), ["Deliverables", "Project"])
        self.assertEqual(t["Project"], ["GRADE", "ONLINE", "GRADE/SOURCE", "GRADE/{job}_REFS"])

    def test_missing_templates_drive_is_explained(self):
        self.rig.cfg["project_setup"]["templates_dir"] = str(self.rig.vol / "Gone/FolderStructures")
        self.assertIn("Gone isn't connected", self.s.templates()[1])

    def test_payload_suggests_number_clients_and_volumes(self):
        p = self.s.payload()
        self.assertEqual(p["next_job_no"], "WC096")  # WC095 on the drive beats WC084 in the backups
        clients = {c["folder"]: c for c in p["clients"]}
        self.assertEqual(clients["WSMLondon"]["client"], "WSM")  # name used in its newest job
        self.assertEqual(clients["LadBible"]["client"], "Ladbible")
        self.assertEqual(clients["AaronWheeler"]["last_job"], "WC095_AaronWheeler_ByFeel")
        self.assertEqual([v["name"] for v in p["volumes"]], ["Wash-Raid-6"])  # set-up drives aren't offered
        self.assertTrue(p["volumes"][0]["has_projects"])

    def test_plan(self):
        p = self.s.plan(FIELDS)
        self.assertEqual(p["errors"], {})
        self.assertTrue(p["can_create"])
        self.assertEqual(p["job"], "WC105_Saatchi_Plusnet_Plusso")
        drive, extra = p["locations"]
        self.assertEqual((drive["kind"], drive["name"], drive["status"], drive["folders"], drive["on"]),
                         ("drive", "Colour-3-Raid", "ready", 4, True))
        self.assertEqual((extra["kind"], extra["on"], extra["status"]), ("extra", True, "ready"))  # every job gets it
        self.assertIn("SaatchiSaatchi is a new client folder.", p["warnings"])
        self.assertEqual(self.s.plan({**FIELDS, "job_no": "WC095"})["warnings"][0],
                         "WC095 is already used by WC095_AaronWheeler_ByFeel.")
        missing = self.s.plan(FIELDS, drive=1)
        self.assertEqual(missing["locations"][0]["status"], "drive_missing")
        self.assertFalse(missing["can_create"])  # never half a job
        self.assertIn("drive", self.s.plan(FIELDS, drive=7)["errors"])
        self.assertFalse(self.s.plan({**FIELDS, "title": ""})["can_create"])
        self.assertFalse(any(self.d.joinpath("Volumes/Colour-3-Raid/Projects").glob("Saatchi*")))  # touched nothing

    def test_create_makes_the_tree_and_records_it(self):
        r = self.s.create(FIELDS, 0)
        self.assertTrue(r["ok"], r)
        job = self.rig.vol / "Colour-3-Raid/Projects/SaatchiSaatchi/WC105_Saatchi_Plusnet_Plusso"
        self.assertTrue((job / "GRADE/SOURCE").is_dir())
        self.assertTrue((job / "GRADE/WC105_Saatchi_Plusnet_Plusso_REFS").is_dir())
        self.assertFalse((job / ".hidden").exists())
        self.assertTrue((self.rig.vol / "WashDeliverables/Deliverables/Projects/WC105_Saatchi_Plusnet_Plusso/WIPs").is_dir())
        rec = json.loads((self.d / "created_projects.json").read_text())
        self.assertEqual(rec[-1]["job"], "WC105_Saatchi_Plusnet_Plusso")
        self.assertEqual([x["kind"] for x in rec[-1]["locations"]], ["drive", "extra"])
        self.assertEqual(self.s.payload()["last_drive"], "Colour-3-Raid")
        self.assertEqual(self.s.payload()["next_job_no"], "WC106")
        # a second go never merges into the existing job
        again = self.s.create(FIELDS, 0)
        self.assertFalse(again["ok"])
        self.assertEqual([x["status"] for x in self.s.plan(FIELDS)["locations"]], ["exists", "exists"])

    def test_a_missing_part_blocks_the_whole_job(self):
        self.rig.cfg["project_setup"]["extras"][0]["path"] = str(self.rig.vol / "Gone/Projects/{job}")
        r = self.s.create(FIELDS, 0)
        self.assertFalse(r["ok"])
        self.assertFalse((self.rig.vol / "Colour-3-Raid/Projects/SaatchiSaatchi").exists())

    def test_a_part_that_exists_is_left_alone_and_the_rest_made(self):
        (self.rig.vol / "WashDeliverables/Deliverables/Projects/WC105_Saatchi_Plusnet_Plusso").mkdir()
        r = self.s.create(FIELDS, 0)
        self.assertTrue(r["ok"], r)
        self.assertEqual([x["kind"] for x in r["results"]], ["drive"])
        self.assertFalse(any((self.rig.vol / "WashDeliverables/Deliverables/Projects/WC105_Saatchi_Plusnet_Plusso").iterdir()))

    def test_failure_in_a_later_part_takes_back_the_earlier_ones(self):
        self.rig.cfg["project_setup"]["resolve"] = {"enabled": True, "library": "L", "template": "T/Tpl"}

        def mkdir(p):
            if "Deliverables/Projects/WC105" in str(p) and p.name == "WIPs":
                raise OSError(5, "Input/output error")
            p.mkdir()
        self.s.mkdir = mkdir
        r = self.s.create(FIELDS, 0)
        self.assertFalse(r["ok"])
        self.assertIn("Taken back", r["results"][0]["error"])
        self.assertIn("Input/output error", r["results"][1]["error"])
        self.assertFalse((self.rig.vol / "Colour-3-Raid/Projects/SaatchiSaatchi").exists())
        self.assertFalse((self.rig.vol / "WashDeliverables/Deliverables/Projects/WC105_Saatchi_Plusnet_Plusso").exists())
        self.assertIsNone(r["resolve"])
        self.assertFalse(self.s.has_pending())

    def test_failure_part_way_leaves_nothing_behind(self):
        calls = []

        def mkdir(p):
            calls.append(p)
            if len(calls) == 4:
                raise OSError(28, "No space left on device")
            p.mkdir()
        self.s.mkdir = mkdir
        r = self.s.create(FIELDS, 0)
        self.assertFalse(r["ok"])
        self.assertIn("No space left", r["results"][0]["error"])
        self.assertIn("another part failed", r["results"][1]["error"])
        self.assertFalse((self.rig.vol / "Colour-3-Raid/Projects/SaatchiSaatchi").exists())  # new client folder gone too
        self.assertTrue((self.rig.vol / "Colour-3-Raid/Projects/AaronWheeler").is_dir())

    def test_permission_problem_is_explained(self):
        def mkdir(p):
            raise PermissionError(1, "Operation not permitted")
        self.s.mkdir = mkdir
        self.assertIn("Privacy & Security", self.s.create(FIELDS, 0)["results"][0]["error"])

    def test_resolve_row(self):
        self.rig.cfg["project_setup"]["resolve"] = {"enabled": True, "library": "WashOrgSrvrDB_1",
                                                    "template": "Project_Template/ProjectTemplate"}
        row = self.s.plan(FIELDS)["locations"][-1]
        self.assertEqual((row["kind"], row["status"], row["on"]), ("resolve", "ready", True))
        self.assertEqual((row["folder"], row["project"]), ("SaatchiSaatchi/WC105_Saatchi_Plusnet_Plusso",
                                                           "WC105_Saatchi_Plusnet_Plusso_CV1"))
        self.rig.names.append("db_x::SaatchiSaatchi/WC105_Saatchi_Plusnet_Plusso/WC105_Saatchi_Plusnet_Plusso_CV1")
        self.assertEqual(self.s.plan(FIELDS)["locations"][-1]["status"], "exists")
        r = self.s.create(FIELDS, 0)
        self.assertIsNone(r["resolve"])  # already in Resolve: left alone, folders still made
        self.assertTrue(r["results"][0]["ok"])
        self.assertFalse(self.s.has_pending())

    def test_resolve_only_and_queue(self):
        self.rig.cfg["project_setup"].update(drives=[], extras=[], resolve={
            "enabled": True, "library": "L", "template": "T/Tpl", "project": "{job}_CV1"})
        r = self.s.create(FIELDS)
        self.assertTrue(r["ok"], r)
        job = self.s.next_resolve_job()
        self.assertEqual((job["template_path"], job["template"], job["folder"]),
                         (["T"], "Tpl", ["SaatchiSaatchi", "WC105_Saatchi_Plusnet_Plusso"]))
        self.s.update_job(job["id"], state="running")
        self.assertTrue(self.s.has_pending())
        self.s.update_job(job["id"], state="done", message="ok")
        self.assertIsNone(self.s.next_resolve_job())
        self.assertEqual(self.s.recent(1)[0]["resolve"]["state"], "done")

    def test_bad_requests(self):
        self.assertFalse(self.s.create({**FIELDS, "client": "a b"}, 0)["ok"])
        self.assertFalse(self.s.create(FIELDS, "first")["ok"])


class ApiTests(unittest.TestCase):
    def test_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rig = Rig(d)
            (d / "config.json").write_text(json.dumps({"backup_dir": str(d / "out"), "min_free_gb": 0,
                                                       **rig.cfg}))
            svc = rcb.Service(d / "config.json", connect=lambda c: None, state_dir=d)
            svc.setup.volumes = rig.vol
            dash = rcb.DashboardServer(svc, port=0, state_dir=d)
            dash.start()
            try:
                def call(path, body=None):
                    req = urllib.request.Request(f"http://127.0.0.1:{dash.port}{path}",
                                                 data=None if body is None else json.dumps(body).encode(),
                                                 headers={"X-RCB-Token": dash.token,
                                                          "Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as r:
                        return json.loads(r.read())
                p = call("/api/project-setup")
                self.assertEqual([x["name"] for x in p["config"]["drives"]], ["Colour-3-Raid", "Colour-9-Raid"])
                self.assertIn("can_choose_folder", p)
                pv = call("/api/project-setup/preview", {"fields": FIELDS, "drive": 0, "extras": []})
                self.assertTrue(pv["can_create"])
                r = call("/api/project-setup/create", {"fields": FIELDS, "drive": 0, "extras": []})
                self.assertTrue(r["ok"], r)
                self.assertTrue((rig.vol / "Colour-3-Raid/Projects/SaatchiSaatchi/WC105_Saatchi_Plusnet_Plusso").is_dir())
                saved = call("/api/settings", {"project_setup": {"extras": []}})
                self.assertTrue(saved["ok"])
                cfg = json.loads((d / "config.json").read_text())
                self.assertEqual(cfg["project_setup"]["extras"], [])
                self.assertEqual(len(cfg["project_setup"]["drives"]), 2)  # untouched
            finally:
                dash.stop()


if __name__ == "__main__":
    unittest.main()
