"""New project set-up: make a job's folder structure on the chosen drives, copied from template folders.

Each folder inside `templates_dir` is a template (for example FolderStructures/Project). A job goes on one
project drive (each workstation has its own RAID, picked from a list) and also on every extra location (for example
the deliverables drive). Each says which template to copy where, with the job's details filled into its path:

    "drives": [{"name": "Colour-3-Raid", "template": "Project",
                "path": "/Volumes/Colour-3-Raid/Projects/{client_folder}/{job}"}],
    "extras": [{"name": "Deliverables", "template": "Deliverables",
                "path": "/Volumes/WashDeliverables/Deliverables/Projects/{job}"}]

Placeholders: {job} (the job name, WC105_Client_Title), {client_folder}, {yyyy}, {mm}, {dd}, and the parts of the
job name: {job_no} (WC105), {client} (Client: up to the second underscore) and {title} (the rest).
They also work in template folder names.

The job can also get a DaVinci Resolve project: a copy of a template project, made in the job's folder:

    "resolve": {"enabled": true, "library": "WashOrgSrvrDB_1", "template": "Project_Template/ProjectTemplate",
                "folder": "{client_folder}/{job}", "project": "{job}_CV1"}

That part needs Resolve, so it's queued here and done by the service's own thread (see Service._run_setup_jobs).

And a Frame.io project with the folders of a template (see frameio.py), made while you wait:

    "frameio": {"enabled": true, "client_id": "…", "account_id": "…", "workspace_id": "…", "workspace": "Wash",
                "template": "FrameIO", "project": "{job}"}

Kept apart from the backup engine on purpose: nothing here talks to Resolve, and a failure here can't stop backups.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import csv
import io
import re
import secrets
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PLACEHOLDERS = ("job", "job_no", "client", "client_folder", "title", "yyyy", "mm", "dd")
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}
MAX_LOCATIONS = 20
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_JOB_RE = re.compile(r"^([A-Z]{1,3})(\d{3,5})_")
_JOB_NO_RE = re.compile(r"^[A-Z]{1,3}\d{3,5}$")
_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_JOB_NAME_RE = re.compile(r"^([A-Z]{1,3}\d{3,5})_([A-Za-z0-9][A-Za-z0-9_-]*)$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

JOB_LIST_TTL = 120  # seconds the job list from the sheet is kept before it's fetched again

PERMISSION_HELP = ("macOS didn't let Colour Project Manager write there. Allow it under System Settings > Privacy & Security "
                   "> Files and Folders (Network Volumes or Removable Volumes), then try again.")


def _junk(name: str) -> bool:
    return name in JUNK or name.startswith("._") or name.startswith(".")


def fill(text: str, values: dict) -> str:
    """Put the job's details into a path or folder name. Unknown {words} are left alone."""
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def job_values(fields: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    job = fields.get("job") if isinstance(fields.get("job"), str) else ""
    job = job.strip()
    m = _JOB_NAME_RE.match(job)
    client, _, title = (m.group(2) if m else "").partition("_")
    cf = fields.get("client_folder") if isinstance(fields.get("client_folder"), str) else ""
    v = {"job": job, "job_no": m.group(1) if m else "", "client": client, "title": title, "client_folder": cf.strip()}
    v.update(yyyy=f"{today.year:04d}", mm=f"{today.month:02d}", dd=f"{today.day:02d}")
    return v


def validate_fields(fields: dict) -> dict:
    """Errors by field name for the job's details. Empty when they're fine."""
    errs = {}
    v = {k: fields.get(k) for k in ("job", "client_folder")}
    for k, val in v.items():
        if val is not None and not isinstance(val, str):
            errs[k] = "Enter text."
    if errs:
        return errs
    v = {k: (val or "").strip() for k, val in v.items()}
    job = v["job"]
    if not job or _JOB_NO_RE.match(job.rstrip("_")):
        errs["job"] = "Enter the job name, for example WC105_WSM_Varley_Spring26."
    elif not re.match(r"^[A-Z]{1,3}\d{3,5}_", job):
        errs["job"] = "Start with the job number and an underscore, for example WC105_."
    elif not _JOB_NAME_RE.match(job) or job.endswith("_") or "__" in job:
        errs["job"] = "Use letters, numbers, hyphens and single underscores only, with no spaces."
    elif len(job) > 150:
        errs["job"] = "That's too long."
    cf = v["client_folder"]
    if not cf:
        errs["client_folder"] = "Enter the client folder."
    elif (len(cf) > 100 or cf.startswith(".") or any(c in cf for c in "/:\\") or
          any(ord(c) < 32 for c in cf)):
        errs["client_folder"] = "Folder names can't contain / or : or start with a dot."
    return errs


def _validate_places(locs, key: str, errs: dict) -> list:
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
        if not any(e == k or e.startswith(k + ".") for e in errs):
            out.append({"name": name.strip(), "template": tpl.strip(), "path": path.strip().rstrip("/")})
    return out


def _validate_resolve(r, errs: dict) -> dict:
    key = "project_setup.resolve"
    if not isinstance(r, dict):
        errs[key] = "Invalid Resolve settings."
        return {}
    out = {}
    if not isinstance(r.get("enabled", False), bool):
        errs[key + ".enabled"] = "Choose on or off."
    out["enabled"] = r.get("enabled", False) is True
    for k, label in (("library", "Choose the library."), ("template", "Enter the template project's folder and name."),
                     ("folder", "Enter the folder the project goes in."), ("project", "Enter the project's name.")):
        v = r.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            if out["enabled"]:
                errs[f"{key}.{k}"] = label
            out[k] = None
            continue
        if not isinstance(v, str) or len(v) > 300:
            errs[f"{key}.{k}"] = label
            continue
        v = v.strip().strip("/")
        segs = v.split("/")
        unknown = sorted({m for m in _PLACEHOLDER_RE.findall(v) if m not in PLACEHOLDERS})
        if k in ("folder", "project") and unknown:
            errs[f"{key}.{k}"] = "Unknown: " + ", ".join("{" + u + "}" for u in unknown) + "."
        elif k in ("library", "project") and len(segs) > 1:
            errs[f"{key}.{k}"] = "This can't contain /."
        elif any(not x.strip() for x in segs):
            errs[f"{key}.{k}"] = "Remove the empty folder name (//)."
        out[k] = v
    return out


def _validate_frameio(f, errs: dict) -> dict:
    key = "project_setup.frameio"
    if not isinstance(f, dict):
        errs[key] = "Invalid Frame.io settings."
        return {}
    out = {"enabled": f.get("enabled", False) is True}
    if not isinstance(f.get("enabled", False), bool):
        errs[key + ".enabled"] = "Choose on or off."

    def text(k):
        v = f.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v.strip() if isinstance(v, str) else v

    cid = text("client_id")
    if cid is not None and (not isinstance(cid, str) or not _CLIENT_ID_RE.match(cid)):
        errs[key + ".client_id"] = "Paste the Client ID from the Adobe Developer Console."
    elif cid is None and out["enabled"]:
        errs[key + ".client_id"] = "Paste the Client ID from the Adobe Developer Console."
    out["client_id"] = cid
    for k in ("account_id", "workspace_id"):
        v = text(k)
        if v is not None and (not isinstance(v, str) or not _ID_RE.match(v)):
            errs[key + ".workspace_id"] = "Choose the workspace again."
        out[k] = v
    if out["enabled"] and not (out["account_id"] and out["workspace_id"]):
        errs[key + ".workspace_id"] = "Connect Frame.io, then choose the workspace."
    ws = text("workspace")
    out["workspace"] = ws[:200] if isinstance(ws, str) else None
    tpl = text("template") or "FrameIO"
    if not isinstance(tpl, str) or "/" in tpl or len(tpl) > 100:
        errs[key + ".template"] = "Choose a template."
    out["template"] = tpl
    proj = text("project") or "{job}"
    unknown = sorted({m for m in _PLACEHOLDER_RE.findall(proj) if m not in PLACEHOLDERS}) if isinstance(proj, str) else []
    if not isinstance(proj, str) or "/" in proj or len(proj) > 200:
        errs[key + ".project"] = "Enter the project's name, without /."
    elif unknown:
        errs[key + ".project"] = "Unknown: " + ", ".join("{" + u + "}" for u in unknown) + "."
    out["project"] = proj
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
            clean[key] = _validate_places(inc[key], f"project_setup.{key}", errs)
    if "resolve" in inc:
        clean["resolve"] = _validate_resolve(inc["resolve"], errs)
    if "frameio" in inc:
        clean["frameio"] = _validate_frameio(inc["frameio"], errs)
    if "job_list_url" in inc:
        clean["job_list_url"] = _validate_job_list_url(inc["job_list_url"], errs)
    for k in inc:
        if k not in ("templates_dir", "drives", "extras", "resolve", "frameio", "job_list_url"):
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


def _ssl_context() -> ssl.SSLContext:
    try:  # the built app has no system certificate file; certifi is bundled with it
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_text(url: str, timeout: float = 10) -> str:
    """GET a published sheet. Raises OSError (or ValueError for a page that isn't CSV) when it can't."""
    req = urllib.request.Request(url, headers={"Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read(5_000_000).decode("utf-8-sig", errors="replace")
    if "html" in ctype.lower():
        raise ValueError("That link gives a web page, not CSV. In Google Sheets use File > Share > Publish to web, "
                         "choose the sheet and Comma-separated values (.csv), and copy that link.")
    return body


def parse_job_list(text: str) -> list[str]:
    """Job names from the published sheet: every cell that looks like one (WB249_Client_Title), in order, once."""
    out, seen = [], set()
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            name = cell.strip()
            if _JOB_NAME_RE.match(name) and "__" not in name and not name.endswith("_") and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _validate_job_list_url(v, errs: dict):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if not isinstance(v, str) or len(v) > 2000 or not v.strip().startswith("https://"):
        errs["project_setup.job_list_url"] = "Paste the https:// link from Publish to web."
        return None
    return v.strip()


_DF_RE = re.compile(r"^.*?\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s+/.*$")


def disk_usage(path: Path, run=subprocess.run, timeout: float = 3) -> tuple[int, int, int]:
    """(total, used, free) in bytes. Uses df where there is one: Python's own figures wrap around on macOS for network
    shares bigger than 16 TB."""
    if Path("/bin/df").exists() or run is not subprocess.run:
        r = run(["/bin/df", "-k", "-P", str(path)], capture_output=True, text=True, timeout=timeout)
        m = _DF_RE.match((r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else "")
        if r.returncode == 0 and m:
            total, used, free = (int(x) * 1024 for x in m.groups())
            return total, used, free
        raise OSError((r.stderr or "").strip() or "df gave no figures")
    u = shutil.disk_usage(path)
    return u.total, u.used, u.free


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
                 mkdir=None, clock=None, libraries=lambda: [], frameio=None, fetch=None, library_names=lambda: {},
                 usage=disk_usage):
        self.get_cfg = get_cfg
        self.usage = usage
        self.fetch = fetch          # url -> text, for the job list; None = never fetch
        self.library_names = library_names  # {database id: library name}, to offer template projects
        self._jobs_cache: tuple[float, str, list] | None = None
        self._jobs_problem: str | None = None
        self.frameio = frameio  # a frameio.FrameIO, or None
        self.manifest = Path(state_dir) / "created_projects.json"
        self.known_names = known_names
        self.libraries = libraries
        self.volumes = Path(volumes)
        self.mkdir = mkdir or (lambda p: p.mkdir())
        self.clock = clock or dt.datetime.now
        self._lock = threading.Lock()
        self._mlock = threading.RLock()  # the record file
        self._queue: list[str] = []      # record ids whose Resolve project is still to be made
        self._scan_cache: tuple[float, tuple, dict] | None = None
        self._forget_unfinished()

    # ---- config
    def cfg(self) -> dict:
        c = self.get_cfg().get("project_setup") or {}
        r = c.get("resolve") or {}
        f = c.get("frameio") or {}
        return {"templates_dir": c.get("templates_dir"), "job_list_url": c.get("job_list_url"),
                "drives": list(c.get("drives") or []),
                "extras": list(c.get("extras") or []),
                "resolve": {"enabled": bool(r.get("enabled")), "library": r.get("library"), "template": r.get("template"),
                            "folder": r.get("folder") or "{client_folder}/{job}", "project": r.get("project") or "{job}_CV1"},
                "frameio": {"enabled": bool(f.get("enabled")), "client_id": f.get("client_id"),
                            "account_id": f.get("account_id"), "workspace_id": f.get("workspace_id"),
                            "workspace": f.get("workspace"), "template": f.get("template") or "FrameIO",
                            "project": f.get("project") or "{job}"}}

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
        names: set[str] = set()   # every job name seen, to tell which jobs in the list are already set up
        for full in self.known_names():
            names.update((full.partition("::")[2] or full).split("/"))
        for loc in locs:
            head = loc.get("path", "").split("{", 1)[0]
            try:
                if head.endswith("/") and Path(head).is_dir():
                    for c in Path(head).iterdir():
                        names.add(c.name)
                        if c.is_dir() and loc.get("path", "")[len(head):].startswith("{client_folder}/"):
                            names.update(j.name for j in c.iterdir())
            except OSError:
                continue
        val = {"jobs": jobs, "clients": clients, "names": names}
        self._scan_cache = (time.monotonic(), key, val)
        return val

    def job_list(self, fresh: bool = False) -> dict:
        """The job names from the published sheet, newest number first, each marked if it's already set up."""
        url = self.cfg()["job_list_url"]
        if not url or not self.fetch:
            return {"configured": bool(url), "names": [], "problem": None}
        hit = self._jobs_cache
        if fresh or not hit or hit[1] != url or time.monotonic() - hit[0] > JOB_LIST_TTL:
            try:
                names = parse_job_list(self.fetch(url))
                self._jobs_cache, self._jobs_problem = (time.monotonic(), url, names), None
                if not names:
                    self._jobs_problem = "The published sheet has no job names in it."
            except (OSError, ValueError) as e:
                why = str(e) if isinstance(e, ValueError) else f"Couldn't read the job list: {getattr(e, 'reason', None) or e}."
                self._jobs_problem = why
                if not hit or hit[1] != url:
                    return {"configured": True, "names": [], "problem": why}
                self._jobs_cache = (time.monotonic() - JOB_LIST_TTL + 30, url, hit[2])  # try again in 30 s
        known = self._scan()["names"] | {r.get("job") for r in self.recent(500) if any(x.get("ok") for x in r.get("locations", []))}

        def number(n):
            m = _JOB_RE.match(n)
            return (m.group(1), int(m.group(2))) if m else ("", 0)
        rows = [{"job": n, "done": n in known} for n in self._jobs_cache[2]]
        rows.sort(key=lambda r: number(r["job"]), reverse=True)
        return {"configured": True, "names": rows, "problem": self._jobs_problem}

    def drives_status(self, timeout: float = 3) -> list[dict]:
        """Every connected drive with its size and free space, and what this app uses it for. Drives that are set up
        here but not connected are listed too. A drive that doesn't answer in time (a hung network share) is shown as
        such rather than holding the page up."""
        c, full = self.cfg(), self.get_cfg()
        roles: dict[str, list[str]] = {}

        def note(path, role):
            if not path:
                return
            try:
                rel = Path(str(path).split("{", 1)[0]).relative_to(self.volumes)
            except ValueError:
                return
            if rel.parts:
                roles.setdefault(rel.parts[0], [])
                if role not in roles[rel.parts[0]]:
                    roles[rel.parts[0]].append(role)
        for d in c["drives"]:
            note(d.get("path"), "Projects")
        for d in c["extras"]:
            note(d.get("path"), d.get("name") or "Extra")
        note(full.get("backup_dir"), "Resolve backups")
        note(c["templates_dir"], "Folder templates")
        try:
            vols = sorted((v for v in self.volumes.iterdir() if not _junk(v.name)), key=lambda v: v.name.lower())
        except OSError:
            vols = []
        results: dict[str, object] = {}

        def measure(v: Path):
            try:
                results[v.name] = self.usage(v)
            except (OSError, subprocess.SubprocessError) as e:
                results[v.name] = e if isinstance(e, OSError) else OSError("it didn't answer")
        vols = [v for v in vols if not (v.is_symlink() and os.path.realpath(v) == "/")]  # this Mac's own disk
        threads = [threading.Thread(target=measure, args=(v,), daemon=True) for v in vols]
        for t in threads:
            t.start()
        end = time.monotonic() + timeout
        for t in threads:
            t.join(max(0, end - time.monotonic()))
        out = []
        for v in vols:
            u = results.get(v.name)
            row = {"name": v.name, "roles": roles.get(v.name, []), "connected": True, "total": None, "free": None,
                   "problem": None}
            if u is None:
                row["problem"] = "Didn't answer in time."
            elif isinstance(u, OSError):
                row["problem"] = f"Couldn't read it: {u.strerror or u}."
            else:
                row.update(total=u[0], free=u[2], used=u[1])
            out.append(row)
        # shares on the same server space (identical figures): say so, so the space isn't counted twice
        for row in out:
            if row["total"]:
                twins = [o["name"] for o in out if o is not row and o["total"] == row["total"] and o.get("used") == row.get("used")]
                if twins:
                    row["shares_with"] = twins
        seen = {v.name for v in vols}
        out += [{"name": n, "roles": r, "connected": False, "total": None, "free": None, "problem": "Not connected."} for n, r in sorted(roles.items()) if n not in seen]
        return out

    def library_projects(self) -> dict:
        """{library name: ["Folder/Project", …]} from the backup record, to pick the Resolve template from."""
        names = self.library_names()
        out: dict[str, list[str]] = {}
        for key in self.known_names():
            db, _, path = key.partition("::")
            if path:
                out.setdefault(names.get(db, db), []).append(path)
        return {k: sorted(v, key=str.lower) for k, v in out.items()}

    def payload(self, fresh: bool = False) -> dict:
        templates, problem = self.templates()
        scan = self._scan()
        clients = [{"folder": k, "client": v["client"] or (k if _PART_RE.match(k) else None), "last_job": v["last_job"]}
                   for k, v in sorted(scan["clients"].items(), key=lambda kv: kv[0].lower())]
        return {"config": self.cfg(), "templates": templates, "templates_problem": problem,
                "next_job_no": next_job_number(scan["jobs"].values()), "clients": clients,
                "placeholders": list(PLACEHOLDERS), "recent": self.recent(10), "volumes": self.volumes_list(),
                "last_drive": self.last_drive(), "libraries": sorted(self.libraries(), key=str.lower),
                "frameio": self.frameio.status() if self.frameio else None,
                "job_list": self.job_list(fresh), "library_projects": self.library_projects()}

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
    def plan(self, fields: dict, drive=None) -> dict:
        """What Create would do, without touching anything. Every job gets all of it: its folders on the chosen
        project drive (drive: index into drives; None = the most recently used, else the first) and on every extra
        location, and its Resolve project if that's set up.
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
            rows.append(("extra", i, loc, True))
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
        rv = c["resolve"]
        if rv["enabled"] and rv["library"] and rv["template"]:
            row = {"kind": "resolve", "index": 0, "name": "DaVinci Resolve", "template": rv["template"],
                   "library": rv["library"], "on": True, "path": None, "folder": None, "project": None,
                   "status": "ready", "message": None, "folders": 0}
            if errs:
                row.update(status="waiting", message="Fill in the job details.")
            else:
                folder, project = fill(rv["folder"], values), fill(rv["project"], values)
                row.update(folder=folder, project=project, path=f"{rv['library']} / {folder} / {project}")
                if any(not x.strip() for x in folder.split("/")) or "/" in project:
                    row.update(status="bad_name", message="The Resolve folder or project name came out empty.")
                elif any(k.endswith(f"::{folder}/{project}") for k in self.known_names()):
                    row.update(status="exists", message="Already in Resolve. Nothing will be changed there.")
            locs.append(row)
        fio = c["frameio"]
        if fio["enabled"]:
            locs.append(self._plan_frameio(fio, values, templates, problem, errs))
        # all or nothing: never leave a job half set up. Parts that already exist are left as they are.
        ready = [r for r in locs if r["status"] == "ready"]
        blocked = [r for r in locs if r["status"] not in ("ready", "exists")]
        return {"job": values["job"], "errors": errs, "warnings": warnings, "locations": locs,
                "can_create": not errs and bool(ready) and not blocked}

    def _plan_frameio(self, fio: dict, values: dict, templates: dict, problem, errs: dict) -> dict:
        row = {"kind": "frameio", "index": 0, "name": "Frame.io", "template": fio["template"], "on": True,
               "path": None, "project": None, "status": "ready", "message": None, "folders": 0}
        tpl = templates.get(fio["template"])
        if errs:
            return {**row, "status": "waiting", "message": "Fill in the job details."}
        name = fill(fio["project"], values)
        row.update(project=name, path=f"{fio['workspace'] or 'Frame.io'} / {name}")
        if not self.frameio or not self.frameio.connected():
            return {**row, "status": "not_connected", "message": "Frame.io isn't connected. Connect it in "
                                                                 "Settings."}
        if not (fio["account_id"] and fio["workspace_id"]):
            return {**row, "status": "not_set_up", "message": "Choose the Frame.io workspace in "
                                                              "Settings."}
        if tpl is None:
            return {**row, "status": "template_missing",
                    "message": problem or f"There's no template called {fio['template']}."}
        try:
            found = self.frameio.find_project(name)
        except Exception as e:  # noqa: BLE001 (frameio.FrameIOError, or anything unexpected: still blocks)
            return {**row, "status": "unreachable", "message": str(e) or "Couldn't reach Frame.io."}
        if found:
            return {**row, "status": "exists", "message": "Already in Frame.io. Nothing will be changed there.",
                    "url": found.get("view_url")}
        return {**row, "folders": len(tpl)}

    # ---- create
    def create(self, fields: dict, drive=None) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "Another project is being created. Try again in a moment."}
        try:
            p = self.plan(fields, drive)
            if not p["can_create"]:
                return {"ok": False, "error": "Not everything can be made yet, so nothing was. Check the list.", "plan": p}
            values = job_values(fields, self.clock().date())
            templates, _ = self.templates()
            results, made_here, failed = [], [], False
            job = None
            for row in p["locations"]:
                if row["status"] != "ready":
                    continue  # made before ("exists"): left as it is
                if row["kind"] == "frameio":
                    continue  # last, once everything on the drives is made
                if row["kind"] == "resolve":
                    tpl = row["template"].split("/")
                    job = {"state": "queued", "library": row["library"], "template_path": tpl[:-1],
                           "template": tpl[-1], "folder": row["folder"].split("/"), "project": row["project"],
                           "detail": "Waiting its turn", "message": None}
                    continue
                res = {"kind": row["kind"], "name": row["name"], "path": row["path"], "ok": False, "folders": 0,
                       "error": None}
                results.append(res)
                if failed:
                    res["error"] = "Not made, because another part failed."
                    continue
                try:
                    made = self._make(Path(row["path"]), templates[row["template"]], values)
                    made_here.append((res, made))
                    res.update(ok=True, folders=len(templates[row["template"]]))
                except OSError as e:
                    failed = True
                    res["error"] = PERMISSION_HELP if isinstance(e, PermissionError) else (
                        "Already exists. Nothing was changed there." if isinstance(e, FileExistsError)
                        else f"Couldn't create the folders: {e.strerror or e}.")
            fio = next((r for r in p["locations"] if r["kind"] == "frameio" and r["status"] == "ready"), None)
            if fio:
                res = {"kind": "frameio", "name": "Frame.io", "path": fio["path"], "ok": False, "folders": 0,
                       "error": None, "url": None}
                results.append(res)
                if failed:
                    res["error"] = "Not made, because another part failed."
                else:
                    tree = [fill(rel, values) for rel in templates[fio["template"]]]
                    try:
                        made = self.frameio.make_project(fio["project"], tree)
                        res.update(ok=True, folders=made["folders"], url=made.get("url"), id=made.get("id"))
                    except Exception as e:  # noqa: BLE001 (it takes its own half-made project back)
                        failed = True
                        res["error"] = str(e) or "Frame.io failed."
            if failed:  # all or nothing: take back what this attempt made elsewhere, and don't make the project
                for res, made in reversed(made_here):
                    self._remove(made)
                    res.update(ok=False, folders=0, error="Taken back, because another part failed.")
                job = None
            rid = self._record(values, fields, results, job)
            if job and job["state"] == "queued":
                with self._mlock:
                    self._queue.append(rid)
            self._scan_cache = None
            return {"ok": not failed, "id": rid, "job": values["job"], "results": results, "resolve": job}
        finally:
            self._lock.release()

    @staticmethod
    def _remove(made: list[Path]) -> None:
        for d in reversed(made):
            try:
                d.rmdir()  # only ever removes empty folders this app just created
            except OSError:
                pass

    def _make(self, dest: Path, tree: list[str], values: dict) -> list[Path]:
        """Create dest and the template's folders inside it; returns what was made. On failure, removes it again."""
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
            self._remove(made)
            raise
        return made

    def _load(self) -> list:
        try:
            items = json.loads(self.manifest.read_text(encoding="utf-8"))
            return items if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items: list) -> None:
        try:
            _write_json(self.manifest, items[-500:])
        except OSError:
            pass

    def _record(self, values: dict, fields: dict, results: list, resolve: dict | None) -> str:
        rid = secrets.token_hex(6)
        with self._mlock:
            items = self._load()
            items.append({"id": rid, "at": self.clock().isoformat(timespec="seconds"), "job": values["job"],
                          "fields": {k: values[k] for k in ("job", "client_folder")},
                          "locations": results, "resolve": resolve})
            self._save(items)
        return rid

    def _forget_unfinished(self) -> None:
        """Resolve projects still queued when the app last stopped weren't made; say so, so they can be retried."""
        with self._mlock:
            items = self._load()
            changed = False
            for it in items:
                r = it.get("resolve") or {}
                if r.get("state") in ("queued", "running"):
                    r.update(state="failed", detail=None, message="Not made: the app stopped first. Press Retry.")
                    changed = True
            if changed:
                self._save(items)

    # ---- the Resolve project queue (run by the service thread)
    def has_pending(self) -> bool:
        return bool(self._queue)

    def next_resolve_job(self) -> dict | None:
        with self._mlock:
            while self._queue:
                rid = self._queue[0]
                it = next((x for x in self._load() if x.get("id") == rid), None)
                if it and (it.get("resolve") or {}).get("state") == "queued":
                    return {"id": rid, "job": it["job"], **it["resolve"]}
                self._queue.pop(0)
            return None

    def update_job(self, rid: str, **kw) -> None:
        with self._mlock:
            items = self._load()
            for it in items:
                if it.get("id") == rid and it.get("resolve"):
                    it["resolve"].update(kw)
                    if kw.get("state") not in (None, "queued", "running") and rid in self._queue:
                        self._queue.remove(rid)
                    self._save(items)
                    return

    def retry(self, rid) -> dict:
        with self._mlock:
            items = self._load()
            it = next((x for x in items if x.get("id") == rid), None)
            r = (it or {}).get("resolve") or {}
            if r.get("state") != "failed":
                return {"ok": False, "error": "There's nothing to retry for that job."}
            r.update(state="queued", detail="Waiting its turn", message=None)
            self._save(items)
            if rid not in self._queue:
                self._queue.append(rid)
            return {"ok": True}
