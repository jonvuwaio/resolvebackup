"""New project set-up: make a job's folder structure on the chosen drives, copied from template folders.

Each folder inside `templates_dir` is a template (for example FolderStructures/Project). A job goes on one
project drive (each workstation has its own RAID, picked from a list) plus any extra locations that are ticked.
Each says which template to copy where, with the job's details filled into its path:

    "drives": [{"name": "Colour-3-Raid", "template": "Project",
                "path": "/Volumes/Colour-3-Raid/Projects/{client_folder}/{job}"}],
    "extras": [{"name": "Deliverables", "template": "Deliverables",
                "path": "/Volumes/WashDeliverables/Deliverables/Projects/{job}", "default_on": false}]

Placeholders: {job} (WC105_Client_Title), {job_no}, {client}, {client_folder}, {title}, {yyyy}, {mm}, {dd}.
They also work in template folder names.

Kept apart from the backup engine on purpose: nothing here talks to Resolve, and a failure here can't stop backups.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path

PLACEHOLDERS = ("job", "job_no", "client", "client_folder", "title", "yyyy", "mm", "dd")
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}
MAX_LOCATIONS = 20
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_JOB_RE = re.compile(r"^([A-Z]{1,3})(\d{3,5})_")
_JOB_NO_RE = re.compile(r"^[A-Z]{1,3}\d{3,5}$")
_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

PERMISSION_HELP = ("macOS didn't let Resolve Backup write there. Allow it under System Settings > Privacy & Security "
                   "> Files and Folders (Network Volumes or Removable Volumes), then try again.")


def _junk(name: str) -> bool:
    return name in JUNK or name.startswith("._") or name.startswith(".")


def fill(text: str, values: dict) -> str:
    """Put the job's details into a path or folder name. Unknown {words} are left alone."""
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def job_values(fields: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    v = {k: (fields.get(k) or "").strip() for k in ("job_no", "client", "client_folder", "title")}
    v["job"] = "_".join(x for x in (v["job_no"], v["client"], v["title"]) if x)
    v.update(yyyy=f"{today.year:04d}", mm=f"{today.month:02d}", dd=f"{today.day:02d}")
    return v


def validate_fields(fields: dict) -> dict:
    """Errors by field name for the job's details. Empty when they're fine."""
    errs = {}
    v = {k: fields.get(k) for k in ("job_no", "client", "client_folder", "title")}
    for k, val in v.items():
        if val is not None and not isinstance(val, str):
            errs[k] = "Enter text."
    if errs:
        return errs
    v = {k: (val or "").strip() for k, val in v.items()}
    if not v["job_no"]:
        errs["job_no"] = "Enter a job number."
    elif not _JOB_NO_RE.match(v["job_no"]):
        errs["job_no"] = "Use capital letters then digits, for example WC105."
    if not v["client"]:
        errs["client"] = "Enter the client's name as it goes in the job name."
    elif not _PART_RE.match(v["client"]) or len(v["client"]) > 60:
        errs["client"] = "Use letters, numbers and hyphens only, with no spaces or underscores."
    if not v["title"]:
        errs["title"] = "Enter the job title."
    elif not _TITLE_RE.match(v["title"]) or len(v["title"]) > 100:
        errs["title"] = "Use letters, numbers, hyphens and underscores only, with no spaces."
    cf = v["client_folder"]
    if not cf:
        errs["client_folder"] = "Enter the client folder."
    elif (len(cf) > 100 or cf.startswith(".") or any(c in cf for c in "/:\\") or
          any(ord(c) < 32 for c in cf)):
        errs["client_folder"] = "Folder names can't contain / or : or start with a dot."
    return errs


def _validate_places(locs, key: str, errs: dict, extras: bool) -> list:
    if not isinstance(locs, list) or len(locs) > MAX_LOCATIONS:
        errs[key] = f"Add up to {MAX_LOCATIONS}."
        return []
    out, names = [], set()
    for i, loc in enumerate(locs):
        k = f"{key}.{i}"
        if not isinstance(loc, dict):
            errs[k] = "Invalid entry."
            continue
        name, tpl, path = (loc.get(x) for x in ("name", "template", "path"))
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            errs[k + ".name"] = "Give it a name."
        elif name.strip().lower() in names:
            errs[k + ".name"] = "Two entries have this name."
        else:
            names.add(name.strip().lower())
        if not isinstance(tpl, str) or not tpl.strip() or "/" in tpl:
            errs[k + ".template"] = "Choose a template."
        if not isinstance(path, str) or not path.strip():
            errs[k + ".path"] = "Enter where the folders go."
        else:
            p = path.strip().rstrip("/")
            unknown = sorted({m for m in _PLACEHOLDER_RE.findall(p) if m not in PLACEHOLDERS})
            if not p.startswith("/"):
                errs[k + ".path"] = "Use a full path starting with /Volumes/."
            elif "{job}" not in p:
                errs[k + ".path"] = "Include {job} so each job gets its own folder."
            elif unknown:
                errs[k + ".path"] = "Unknown: " + ", ".join("{" + u + "}" for u in unknown) + "."
            elif any(seg in ("..", ".") for seg in p.split("/")):
                errs[k + ".path"] = "The path can't contain . or .. folders."
        clean = {}
        if extras:
            default_on = loc.get("default_on", False)
            if not isinstance(default_on, bool):
                errs[k + ".default_on"] = "Choose on or off."
            clean["default_on"] = default_on
        if not any(e == k or e.startswith(k + ".") for e in errs):
            out.append({"name": name.strip(), "template": tpl.strip(), "path": path.strip().rstrip("/"), **clean})
    return out


def validate_config(inc) -> tuple[dict, dict]:
    """Validate the project_setup settings from the dashboard. Returns (clean, errors-by-field)."""
    errs: dict = {}
    if not isinstance(inc, dict):
        return {}, {"project_setup": "Invalid project set-up settings."}
    clean: dict = {}
    if "templates_dir" in inc:
        t = inc["templates_dir"]
        if t is None or (isinstance(t, str) and not t.strip()):
            clean["templates_dir"] = None
        elif not isinstance(t, str) or not Path(os.path.expanduser(t.strip())).is_absolute():
            errs["project_setup.templates_dir"] = "Use a full path, for example /Volumes/Drive/FolderStructures."
        else:
            clean["templates_dir"] = os.path.expanduser(t.strip()).rstrip("/") or "/"
    for key in ("drives", "extras"):
        if key in inc:
            clean[key] = _validate_places(inc[key], f"project_setup.{key}", errs, key == "extras")
    for k in inc:
        if k not in ("templates_dir", "drives", "extras"):
            errs[f"project_setup.{k}"] = "Unknown setting."
    return clean, errs


def next_job_number(names) -> str | None:
    """The job number after the highest one seen, in the most common style (WC104 -> WC105, WB999 -> WC000)."""
    seen: dict[tuple[int, int], list[tuple[str, int]]] = {}
    for n in names:
        m = _JOB_RE.match(n)
        if m:
            seen.setdefault((len(m.group(1)), len(m.group(2))), []).append((m.group(1), int(m.group(2))))
    if not seen:
        return None
    shape = max(seen, key=lambda k: len(seen[k]))
    letters, num = max(seen[shape])
    width = shape[1]
    num += 1
    if num >= 10 ** width:
        num = 0
        chars = list(letters)
        i = len(chars) - 1
        while i >= 0 and chars[i] == "Z":
            chars[i] = "A"
            i -= 1
        if i < 0:
            return None
        chars[i] = chr(ord(chars[i]) + 1)
        letters = "".join(chars)
    return f"{letters}{num:0{width}d}"


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _volume_missing(path: Path, volumes: Path) -> str | None:
    """The drive's name if `path` is on a drive under /Volumes that isn't connected."""
    try:
        rel = path.relative_to(volumes)
    except ValueError:
        return None
    if not rel.parts:
        return None
    vol = volumes / rel.parts[0]
    return None if vol.is_dir() else rel.parts[0]


class ProjectSetup:
    """Creates job folders. One per service; safe to call from the dashboard's threads."""

    def __init__(self, get_cfg, state_dir: Path, known_names=lambda: (), volumes: Path = Path("/Volumes"),
                 mkdir=None, clock=None):
        self.get_cfg = get_cfg
        self.manifest = Path(state_dir) / "created_projects.json"
        self.known_names = known_names
        self.volumes = Path(volumes)
        self.mkdir = mkdir or (lambda p: p.mkdir())
        self.clock = clock or dt.datetime.now
        self._lock = threading.Lock()
        self._scan_cache: tuple[float, tuple, dict] | None = None

    # ---- config
    def cfg(self) -> dict:
        c = self.get_cfg().get("project_setup") or {}
        return {"templates_dir": c.get("templates_dir"), "drives": list(c.get("drives") or []),
                "extras": list(c.get("extras") or [])}

    def places(self) -> list[dict]:
        c = self.cfg()
        return c["drives"] + c["extras"]

    def volumes_list(self) -> list[dict]:
        """Connected drives that aren't set up yet, for "Add a drive"."""
        used = set()
        for p in self.places():
            try:
                used.add(Path(p["path"].split("{", 1)[0]).relative_to(self.volumes).parts[0])
            except (ValueError, IndexError):
                pass
        out = []
        try:
            for v in sorted(self.volumes.iterdir(), key=lambda p: p.name.lower()):
                if v.is_dir() and not _junk(v.name) and v.name not in used:
                    out.append({"name": v.name, "path": str(v), "has_projects": (v / "Projects").is_dir()})
        except OSError:
            pass
        return out

    def templates(self) -> tuple[dict[str, list[str]], str | None]:
        """{template name: [folder paths inside it, parents first]}, plus a problem to show, if any."""
        d = self.cfg()["templates_dir"]
        if not d:
            return {}, "Choose the folder that holds your folder templates."
        root = Path(os.path.expanduser(d))
        missing = _volume_missing(root, self.volumes)
        if missing:
            return {}, f"The drive {missing} isn't connected, so the templates can't be read."
        if not root.is_dir():
            return {}, f"{root} doesn't exist."
        out = {}
        try:
            for t in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if t.is_dir() and not _junk(t.name):
                    out[t.name] = self._tree(t)
        except OSError as e:
            return {}, f"Couldn't read {root}: {e.strerror or e}"
        return out, None if out else f"There are no template folders in {root}."

    @staticmethod
    def _tree(root: Path) -> list[str]:
        dirs = []
        for here, subdirs, _files in os.walk(root):
            subdirs[:] = sorted(s for s in subdirs if not _junk(s))
            rel = Path(here).relative_to(root)
            dirs += [(rel / s).as_posix() for s in subdirs]
        return dirs

    # ---- what already exists
    def _scan(self) -> dict:
        """Job and client names from the backup record and from the drives, cached for a minute."""
        locs = self.places()
        key = tuple(loc.get("path", "") for loc in locs)
        if self._scan_cache and time.monotonic() - self._scan_cache[0] < 60 and self._scan_cache[1] == key:
            return self._scan_cache[2]
        jobs: dict[str, str] = {}        # job_no -> a job name using it
        clients: dict[str, dict] = {}    # client folder -> {"client": name used in jobs, "last_job": ...}

        def note(folder: str | None, job: str) -> None:
            m = _JOB_RE.match(job)
            if not m:
                return
            jobs.setdefault(m.group(1) + m.group(2), job)
            if folder:
                c = clients.setdefault(folder, {"client": None, "last_job": None})
                parts = job.split("_")
                if not c["last_job"] or job > c["last_job"]:
                    c["last_job"] = job
                    if len(parts) > 2:
                        c["client"] = parts[1]

        for full in self.known_names():
            path = full.partition("::")[2] or full
            segs = path.split("/")
            for i, s in enumerate(segs[:-1] or segs):
                note(segs[0] if i == 1 else None, s)
            if len(segs) > 1:
                clients.setdefault(segs[0], {"client": None, "last_job": None})
        for loc in locs:
            pattern = loc.get("path", "")
            head = pattern.split("{", 1)[0]
            if not head.endswith("/"):
                continue
            base = Path(head)
            rest = pattern[len(head):]
            try:
                if rest.startswith("{client_folder}/{job}"):
                    for c in base.iterdir():
                        if c.is_dir() and not _junk(c.name):
                            clients.setdefault(c.name, {"client": None, "last_job": None})
                            for j in c.iterdir():
                                note(c.name, j.name)
                elif rest.startswith("{job}"):
                    for j in base.iterdir():
                        note(None, j.name)
            except OSError:
                continue
        val = {"jobs": jobs, "clients": clients}
        self._scan_cache = (time.monotonic(), key, val)
        return val

    def payload(self) -> dict:
        templates, problem = self.templates()
        scan = self._scan()
        clients = [{"folder": k, "client": v["client"] or (k if _PART_RE.match(k) else None), "last_job": v["last_job"]}
                   for k, v in sorted(scan["clients"].items(), key=lambda kv: kv[0].lower())]
        return {"config": self.cfg(), "templates": templates, "templates_problem": problem,
                "next_job_no": next_job_number(scan["jobs"].values()), "clients": clients,
                "placeholders": list(PLACEHOLDERS), "recent": self.recent(10), "volumes": self.volumes_list(),
                "last_drive": self.last_drive()}

    def last_drive(self) -> str | None:
        """The project drive used for the most recent job, so the list starts there."""
        names = {d["name"] for d in self.cfg()["drives"]}
        for item in self.recent(50):
            for r in item.get("locations", []):
                if r.get("kind") == "drive" and r.get("name") in names:
                    return r["name"]
        return None

    def recent(self, n: int = 10) -> list:
        try:
            return json.loads(self.manifest.read_text(encoding="utf-8"))[-n:][::-1]
        except (OSError, ValueError):
            return []

    # ---- preview
    def plan(self, fields: dict, drive=None, extras=None) -> dict:
        """What Create would do, without touching anything.

        drive: index into drives (None = the most recently used, else the first). extras: indexes of the extra
        locations to include (None = the ones on by default).
        """
        c = self.cfg()
        errs = validate_fields(fields)
        if c["drives"]:
            if drive is None:
                last = self.last_drive()
                drive = next((i for i, d in enumerate(c["drives"]) if d["name"] == last), 0)
            if not isinstance(drive, int) or isinstance(drive, bool) or not 0 <= drive < len(c["drives"]):
                errs["drive"] = "Choose a project drive."
                drive = None
        values = job_values(fields, self.clock().date())
        templates, problem = self.templates()
        warnings = []
        if not errs:
            scan = self._scan()
            used = scan["jobs"].get(values["job_no"])
            if used and used != values["job"]:
                warnings.append(f"{values['job_no']} is already used by {used}.")
            if values["client_folder"] not in scan["clients"]:
                warnings.append(f"{values['client_folder']} is a new client folder.")
        rows = []
        if drive is not None:
            rows.append(("drive", drive, c["drives"][drive], True))
        for i, loc in enumerate(c["extras"]):
            rows.append(("extra", i, loc, bool(loc.get("default_on")) if extras is None else i in extras))
        locs = []
        for kind, i, loc, on in rows:
            row = {"kind": kind, "index": i, "name": loc.get("name"), "template": loc.get("template"), "on": on,
                   "path": None, "status": "ready", "message": None, "folders": 0}
            tpl = templates.get(loc.get("template"))
            if errs:
                row.update(status="waiting", message="Fill in the job details.")
            else:
                dest = Path(fill(loc.get("path", ""), values))
                row["path"] = str(dest)
                gone = _volume_missing(dest, self.volumes)
                if gone:
                    row.update(status="drive_missing", message=f"{gone} isn't connected.")
                elif tpl is None:
                    row.update(status="template_missing",
                               message=problem or f"There's no template called {loc.get('template')}.")
                elif dest.exists():
                    row.update(status="exists", message="Already exists. Nothing will be changed there.")
                else:
                    row["folders"] = len(tpl)
                    anc = dest.parent
                    while not anc.exists() and anc != anc.parent:
                        anc = anc.parent
                    if not os.access(anc, os.W_OK):
                        row.update(status="not_writable", message=f"Can't write to {anc}.")
            locs.append(row)
        ready = [r for r in locs if r["on"] and r["status"] == "ready"]
        return {"job": values["job"], "errors": errs, "warnings": warnings, "locations": locs,
                "can_create": not errs and bool(ready)}

    # ---- create
    def create(self, fields: dict, drive, extras) -> dict:
        if not isinstance(extras, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in extras):
            return {"ok": False, "error": "Choose where to create the folders."}
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "Another project is being created. Try again in a moment."}
        try:
            p = self.plan(fields, drive, set(extras))
            if not p["can_create"]:
                return {"ok": False, "error": "Nothing to create. Check the details and locations.", "plan": p}
            values = job_values(fields, self.clock().date())
            templates, _ = self.templates()
            results = []
            for row in p["locations"]:
                if not row["on"]:
                    continue
                res = {"kind": row["kind"], "name": row["name"], "path": row["path"], "ok": False, "folders": 0,
                       "error": None}
                if row["status"] != "ready":
                    res["error"] = row["message"]
                else:
                    try:
                        res["folders"] = self._make(Path(row["path"]), templates[row["template"]], values)
                        res["ok"] = True
                    except OSError as e:
                        res["error"] = PERMISSION_HELP if isinstance(e, PermissionError) else (
                            "Already exists. Nothing was changed there." if isinstance(e, FileExistsError)
                            else f"Couldn't create the folders: {e.strerror or e}. Nothing was left half made.")
                results.append(res)
            self._record(values, fields, results)
            self._scan_cache = None
            return {"ok": all(r["ok"] for r in results), "job": values["job"], "results": results}
        finally:
            self._lock.release()

    def _make(self, dest: Path, tree: list[str], values: dict) -> int:
        """Create dest and the template's folders inside it. On any failure, remove what this call made."""
        made: list[Path] = []
        missing = []
        anc = dest.parent
        while not anc.exists():
            missing.append(anc)
            anc = anc.parent
        try:
            for d in reversed(missing):  # new client folder, etc.
                self.mkdir(d)
                made.append(d)
            self.mkdir(dest)  # fails if it appeared since the preview: never merge into an existing job
            made.append(dest)
            for rel in tree:
                d = dest / fill(rel, values)
                self.mkdir(d)
                made.append(d)
        except OSError:
            for d in reversed(made):
                try:
                    d.rmdir()  # only ever removes empty folders this call created
                except OSError:
                    pass
            raise
        return len(tree)

    def _record(self, values: dict, fields: dict, results: list) -> None:
        try:
            items = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                items = []
        except (OSError, ValueError):
            items = []
        items.append({"at": self.clock().isoformat(timespec="seconds"), "job": values["job"],
                      "fields": {k: values[k] for k in ("job_no", "client", "client_folder", "title")},
                      "locations": results})
        try:
            _write_json(self.manifest, items[-500:])
        except OSError:
            pass
