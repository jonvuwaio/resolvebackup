#!/usr/bin/env python3
"""
resolve-cloud-backup
====================

Background service that incrementally backs up recently modified DaVinci Resolve
projects (including Blackmagic Cloud projects) as .drp files, throughout the day.

How it works
------------
Resolve's scripting API is the only supported way to read cloud projects, so this
talks to a *running* DaVinci Resolve (Studio) over that API:

  1. Walk the project library (all folders) of the chosen database.
  2. Ask Resolve for each project's ``lastModifiedDate``.
  3. Export only projects whose modified date changed since their last backup,
     using ``ProjectManager.ExportProject`` -> timestamped ``.drp`` files.
  4. Prune old copies (keep everything recent, then one per day).

Runs on macOS, Windows and Linux with only the Python standard library.

Commands
--------
  run     Run forever, one backup cycle every ``interval_minutes``  (the service)
  once    Run a single cycle and exit
  list    Print databases/projects exactly as Resolve reports them (for setup)
  probe   Test which export routes work for one project (e.g. while open elsewhere)
  dashboard  Open the live dashboard of the running service in your browser
  status  Show the outcome of the last cycle and per-project state
  init    Write a default config file
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import copy
import datetime as dt
import fnmatch
import hashlib
import html
import http.server
import json
import logging
import logging.handlers
import os
import platform
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import frameio
import project_setup
import resolve_worker
import vidchecker
from resolve_worker import ResolveNotAnswering

APP = "resolve-cloud-backup"
VERSION = "1.30.0"
SYSTEM = platform.system()  # "Darwin" | "Windows" | "Linux"
log = logging.getLogger(APP)

DEFAULTS = {
    # Where .drp backups are written.
    "backup_dir": "~/ResolveCloudBackups",
    # Minutes between backup cycles. 0 = continuously (a minute's gap between cycles).
    "interval_minutes": 30,
    # Minutes to wait before retrying when Resolve isn't running / reachable.
    "retry_minutes": 5,
    # Optional ["HH:MM", "HH:MM"] window; cycles only run inside it (may wrap midnight).
    "active_hours": None,
    # Outside active_hours, check this often instead. 0 = not at all.
    "off_hours_interval_minutes": 0,
    # macOS: while DaVinci Resolve isn't open, start it hidden (-nogui) for each cycle and quit it afterwards,
    # so nothing appears on screen. Open Resolve while it runs hidden and it is quit and opened normally.
    "background_resolve": True,
    # With background_resolve: each hidden check opens the next of your cloud libraries in turn (the one checked
    # longest ago), so every library is backed up without switching in Resolve. Your own choice is put back
    # as soon as the hidden Resolve quits. skip_libraries: library names to leave out.
    "rotate_libraries": True,
    "skip_libraries": [],
    # Which Resolve database to back up. name=None means "whichever is current".
    # type is optional ("Cloud", "PostgreSQL", "Disk" ...) - run `list` to see what
    # Resolve reports for yours.
    "database": {"name": None, "type": None},
    # Switching databases closes any open project, so it is opt-in and is only
    # ever done when no project is open.
    "switch_database": False,
    # First run only: ignore projects not modified within this many days.
    "initial_lookback_days": 7,
    # Glob / folder-prefix filters on "Folder/Sub/Project" paths. Empty = everything.
    "include": [],
    "exclude": [],
    # Pass-through to ExportProject.
    "with_stills_and_luts": True,
    # Export is always attempted directly first (works on the saved database state,
    # so it doesn't need the project to be free). If that fails:
    # "never":  give up (retry later).
    # "auto":   load (read-only is fine) -> export -> close, for any project.   [default]
    # "collab": same, but only for projects in collaboration mode. Use this if a scripted
    #           load of a locked project ever raises Resolve's "project in use" dialog.
    # "always": always load -> export -> close, even when direct export would work.
    # Loading only ever happens when no project is open in this Resolve instance.
    "load_to_export": "auto",
    # Failing projects are retried with backoff: 5, 10, 20 ... up to this many minutes.
    "retry_backoff_max_minutes": 120,
    # Shell command run once when a project reaches this many consecutive failures
    # (e.g. send yourself an email). Gets RCB_PROJECT, RCB_DATABASE, RCB_FAILURES,
    # RCB_REASON in its environment. null = log only.
    "alert_command": None,
    "alert_after_failures": 3,
    # Live dashboard served by the service on this machine only (127.0.0.1).
    # lan_hostnames: names other machines may use, e.g. ["colour.local"]. When set, the dashboard also listens on
    # the network, accepts those names and, on a Mac, advertises each .local name by Bonjour. It is served on
    # lan_web_port as well (80 = plain http://colour.local, null = off) if that port is free.
    # lan_token: false lets anyone on the network in through those names without the access token.
    # Changing any dashboard setting needs a service restart; everything else reloads by itself.
    "dashboard": {"enabled": True, "port": 8765, "lan_hostnames": [], "lan_web_port": 80, "lan_token": True},
    # Save the currently open project before exporting (off: don't touch open work).
    "save_open_project": False,
    # Safety valves.
    "max_exports_per_cycle": 50,
    "pause_between_exports_seconds": 2,
    "min_free_gb": 5,
    # If Resolve doesn't report a modified date, re-export at most this often.
    "fallback_min_interval_minutes": 120,
    # Retention: keep every backup newer than keep_all_hours, then the newest one
    # per day for keep_daily_days. The newest backup of a project is always kept.
    # max_backups: never keep more than this many backups of a project (the newest ones). 0 = no limit.
    "retention": {"keep_all_hours": 24, "keep_daily_days": 30, "max_backups": 0},
    # Override auto-detected Resolve scripting locations if you installed elsewhere.
    "resolve_script_api": None,
    "resolve_script_lib": None,
    # Resolve's executable, for background_resolve. null = the standard install in /Applications.
    "resolve_app": None,
    # Minutes one project's backup may take in a Resolve you have open (the hidden one gets 3). Past that the app
    # stops waiting, counts the project as failed for now and carries on; your Resolve itself is never stopped.
    "own_resolve_export_limit_minutes": 15,
    # Warn (dashboard, menu bar, alert_command) when the Resolve you have open uses more memory than this, in GB.
    # A Resolve left open for days can grow until every backup takes many minutes. 0 = never warn.
    "resolve_memory_warn_gb": 24,
    # New project set-up (dashboard > New project): folder templates, the workstation drives a job can go on,
    # and extra locations that can also get folders. See project_setup.py for the format.
    "project_setup": {"templates_dir": None, "drives": [], "extras": []},
    # Vidchecker QC server, and the checks (template ids) offered on the QC page. See vidchecker.py.
    "vidchecker": {"url": None, "templates": []},
}

RESERVED_WIN = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}


# --------------------------------------------------------------------------- paths
def resource_path(name: str) -> Path:
    """A file shipped next to this program (works from source and inside the packaged app)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / name if base else Path(__file__).resolve().with_name(name)


def app_dir() -> Path:
    if SYSTEM == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif SYSTEM == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP


def resolve_log_dirs() -> list[Path]:
    """Where DaVinci Resolve writes its own logs (ResolveDebug.txt)."""
    if SYSTEM == "Darwin":
        return [Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/logs"]
    if SYSTEM == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return [base / "Blackmagic Design/DaVinci Resolve/Support/logs", base / "Blackmagic Design/DaVinci Resolve/logs"]
    return [Path.home() / ".local/share/DaVinciResolve/logs"]


# Resolve logs every Blackmagic Cloud library the signed-in user can see, e.g.
#   BlackmagicID: PL(c31fb696-ac7b-4ad7-90cf-73708407fb40:'WashOrgSrvrDB_1':D)
# and the scripting API reports the same library as DbName "db_c31fb696ac7b4ad790cf73708407fb40".
_LIBRARY_RE = re.compile(rb"PL\(([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}):'([^'\r\n]{1,200})':")
_log_cache: dict = {}


def cloud_library_names(dirs: list[Path] | None = None) -> dict[str, str]:
    """{"db_<hex>": "Library name"} for the cloud libraries named in Resolve's logs (newest log wins).
    Resolve's scripting API has no call for a library's name, so this is the only source."""
    files = []
    for d in dirs if dirs is not None else resolve_log_dirs():
        for pattern in ("ResolveDebug*.txt", "LogArchive/ResolveDebug*.txt"):
            files += [f for f in d.glob(pattern) if f.is_file()]
    names: dict[str, str] = {}
    for f in sorted(files, key=lambda f: f.stat().st_mtime):
        try:
            st = f.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if _log_cache.get(f, (None,))[0] != sig:
                found = {}
                with open(f, "rb") as fh:
                    for m in _LIBRARY_RE.finditer(fh.read()):
                        found["db_" + m.group(1).decode().replace("-", "").lower()] = m.group(2).decode("utf-8", "replace")
                _log_cache[f] = (sig, found)
            names.update(_log_cache[f][1])
        except OSError:
            continue
    return names


def default_config_path() -> Path:
    return app_dir() / "config.json"


def default_script_paths() -> tuple[str, str]:
    if SYSTEM == "Darwin":
        return (
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
        )
    if SYSTEM == "Windows":
        pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return (
            os.path.join(pd, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting"),
            r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
        )
    return ("/opt/resolve/Developer/Scripting", "/opt/resolve/libs/Fusion/fusionscript.so")


# ------------------------------------------------------------------------- config
def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    p = Path(path) if path else default_config_path()
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            cfg = _merge(cfg, json.load(fh))
    cfg["backup_dir"] = os.path.expanduser(str(cfg["backup_dir"]))
    return cfg


# ----------------------------------------------------------------------- helpers
def parse_ts(value) -> dt.datetime | None:
    """Best-effort parse of Resolve's lastModifiedDate into a naive local datetime."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{9,13}(\.\d+)?", str(value).strip()):
            f = float(value)
            if f > 1e11:  # milliseconds
                f /= 1000.0
            return dt.datetime.fromtimestamp(f)
    except (ValueError, OSError, OverflowError):
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None


def truthy(v) -> bool:
    """Resolve attribute values may be bool, int or string; interpret them leniently."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(v)


def safe_name(name: str) -> str:
    """Filesystem-safe name that stays unique if sanitising changed it."""
    s = re.sub(r"[^\w\-. ()]", "_", name, flags=re.UNICODE).strip(" .") or "_"
    if s.upper() in RESERVED_WIN:
        s = "_" + s
    s = s[:100]
    if s != name:
        s += "-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return s


def in_active_hours(window, now: dt.datetime) -> bool:
    if not window:
        return True
    try:
        a = dt.datetime.strptime(window[0], "%H:%M").time()
        b = dt.datetime.strptime(window[1], "%H:%M").time()
    except (ValueError, IndexError, TypeError):
        log.warning("Ignoring invalid active_hours %r", window)
        return True
    t = now.time()
    return (a <= t < b) if a <= b else (t >= a or t < b)


_STAMP_RE = re.compile(r"__(\d{8}-\d{6})(?:-\d+)?\.drp$")


def backup_time(p: Path) -> dt.datetime:
    m = _STAMP_RE.search(p.name)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(p.stat().st_mtime)


def select_deletions(files: list[tuple[Path, dt.datetime]], now: dt.datetime,
                     keep_all_hours: float, keep_daily_days: float, max_backups: int = 0) -> list[Path]:
    """Pure retention policy. files = [(path, timestamp)]. Returns paths to delete.
    max_backups > 0 caps how many are kept (the newest of those the time rules keep)."""
    if not files:
        return []
    files = sorted(files, key=lambda x: x[1], reverse=True)  # newest first
    keep = {files[0][0]}  # never delete the newest
    recent_cut = now - dt.timedelta(hours=keep_all_hours)
    daily_cut = now - dt.timedelta(days=keep_daily_days)
    seen_days: set = set()
    for path, ts in files:
        if ts >= recent_cut:
            keep.add(path)
        elif ts >= daily_cut:
            day = ts.date()
            if day not in seen_days:  # newest of that day (list is newest-first)
                seen_days.add(day)
                keep.add(path)
    if max_backups > 0:
        keep = set([p for p, _ in files if p in keep][:max_backups])  # files is newest-first
    return [p for p, _ in files if p not in keep]


class SingleInstance:
    """Cross-platform advisory file lock."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def release(self):
        if not self.fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


# ------------------------------------------------------------------ Resolve access
def script_paths(cfg: dict) -> tuple[str, str]:
    """Where Resolve's scripting module and library are: config, then environment, then the standard install."""
    api, lib = default_script_paths()
    return (cfg.get("resolve_script_api") or os.environ.get("RESOLVE_SCRIPT_API") or api,
            cfg.get("resolve_script_lib") or os.environ.get("RESOLVE_SCRIPT_LIB") or lib)


def connect_resolve(cfg: dict):
    """Return the Resolve scripting object, or None if Resolve isn't reachable. In this process: for the command
    line tools; the service goes through a resolve_worker.Worker so a stuck call can't freeze it."""
    api, lib = script_paths(cfg)
    os.environ["RESOLVE_SCRIPT_API"] = api
    os.environ["RESOLVE_SCRIPT_LIB"] = lib
    modules = os.path.join(api, "Modules")
    if modules not in sys.path:
        sys.path.append(modules)
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except Exception as e:  # noqa: BLE001
        log.error("Cannot import DaVinciResolveScript (looked in %s): %s", modules, e)
        return None
    try:
        return dvr.scriptapp("Resolve")
    except Exception as e:  # noqa: BLE001
        log.error("Resolve scripting call failed: %s", e)
        return None


def _same_db(a: dict, b: dict) -> bool:
    return all((a or {}).get(k) == (b or {}).get(k) for k in ("DbType", "DbName", "IpAddress"))


def current_folder_path(pm) -> list[str]:
    """Names from root (exclusive) down to the current folder."""
    names = []
    for _ in range(50):
        names.append(pm.GetCurrentFolder())
        if not pm.GotoParentFolder():
            break
    return list(reversed(names))[1:]


def goto_path(pm, path: list[str]) -> None:
    pm.GotoRootFolder()
    for n in path:
        if not pm.OpenFolder(n):
            break


def open_path(pm, path: list[str]) -> bool:
    """Go to a folder from the root. False if any part of it isn't there."""
    pm.GotoRootFolder()
    return all(pm.OpenFolder(n) for n in path)


def make_resolve_project(pm, job: dict, tmp_dir: Path, template_drp: Path | None = None) -> dict:
    """Copy the template project into job["folder"] as job["project"], by exporting the template and importing it
    under the new name. Nothing is ever opened, so this works in the hidden Resolve and never touches the project
    someone has open. Missing folders are made; if the project can't be made, folders made here are removed again.
    template_drp: an export of the template to use instead, when it's in another database.
    """
    orig = current_folder_path(pm)
    made: list[list[str]] = []
    drp = Path(template_drp) if template_drp else Path(tmp_dir) / "template.drp"
    result = None

    def fail(msg):
        return {"state": "failed", "message": msg}
    try:
        tpl = "/".join(job["template_path"] + [job["template"]])
        if template_drp:
            if not drp.exists():
                result = fail(f"The backup of the template project {tpl} is missing.")
                return result
        elif not open_path(pm, job["template_path"]) or job["template"] not in (pm.GetProjectListInCurrentFolder() or []):
            result = fail(f"The template project {tpl} isn't in {job['library']}.")
            return result
        elif not pm.ExportProject(job["template"], str(drp), True) or not drp.exists():
            result = fail(f"Resolve couldn't export the template project {tpl}.")
            return result
        pm.GotoRootFolder()
        for i, name in enumerate(job["folder"]):
            if name not in (pm.GetFolderListInCurrentFolder() or []):
                if not pm.CreateFolder(name):
                    result = fail(f"Resolve couldn't make the folder {name}.")
                    return result
                made.append(job["folder"][:i + 1])
            if not pm.OpenFolder(name):
                result = fail(f"Resolve couldn't open the folder {name}.")
                return result
        if job["project"] in (pm.GetProjectListInCurrentFolder() or []):
            result = {"state": "exists", "message": "A project with this name is already there. Nothing was changed."}
            return result
        if not pm.ImportProject(str(drp), job["project"]) or job["project"] not in (pm.GetProjectListInCurrentFolder() or []):
            result = fail("Resolve didn't import the copy of the template.")
            return result
        result = {"state": "done", "message": f"Made {job['project']} in {'/'.join(job['folder'])}."}
        return result
    finally:
        if result and result["state"] == "failed":
            for path in reversed(made):  # only folders made here, and only while still empty
                if open_path(pm, path) and not (pm.GetFolderListInCurrentFolder() or pm.GetProjectListInCurrentFolder()):
                    pm.GotoParentFolder()
                    pm.DeleteFolder(path[-1])
        goto_path(pm, orig)
        try:
            drp.unlink()
        except OSError:
            pass


# ------------------------------------------------------------- hidden Resolve
RESOLVE_EXE = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
_RESOLVE_PROC_RE = re.compile(r"DaVinci Resolve\.app/Contents/MacOS/Resolve(?: |$)")


class CycleInterrupted(Exception):
    """Raised inside a cycle to stop it early (service stopping, or Resolve wanted by the user)."""


def resolve_processes(run=subprocess.run) -> list[tuple[int, str]]:
    """(pid, command line) of every running DaVinci Resolve, leaving out its crash-report helper."""
    try:
        out = run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and _RESOLVE_PROC_RE.search(args) and " -reportCrash" not in args:
            procs.append((int(pid), args.strip()))
    return procs


def front_app_pid(run=subprocess.run) -> int | None:
    """pid of the app in front on macOS (the one the user last clicked or opened)."""
    try:
        out = run(["lsappinfo", "info", "-only", "pid", "front"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"pid"=(\d+)', out or "")
    return int(m.group(1)) if m else None


def process_memory_gb(pid: int, run=subprocess.run) -> float | None:
    """Resident memory of a process, in GB (1e9 bytes)."""
    try:
        out = run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip()) * 1024 / 1e9
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def parse_etime(s: str) -> int | None:
    """ps's elapsed time, "[[dd-]hh:]mm:ss", in seconds."""
    m = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", (s or "").strip())
    if not m:
        return None
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def process_age(pid: int, run=subprocess.run) -> int | None:
    """Seconds since a process started."""
    try:
        return parse_etime(run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=10).stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def login_age(run=subprocess.run) -> int | None:
    """Seconds since this user logged in on macOS: the age of their Dock, which starts with the session."""
    try:
        out = run(["ps", "-x", "-o", "etime=,comm="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out or "").splitlines():
        etime, _, comm = line.strip().partition(" ")
        if comm.strip() == "/System/Library/CoreServices/Dock.app/Contents/MacOS/Dock":
            return parse_etime(etime)
    return None


def boot_id(run=subprocess.run) -> str | None:
    """Changes every time the Mac starts (kern.boottime)."""
    try:
        out = run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"sec = (\d+)", out or "")
    return m.group(1) if m else None


def resolve_prefs_dir() -> Path:
    return Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve"


def library_uuid(db_name: str) -> str | None:
    """"db_c31fb696ac7b4ad790cf73708407fb40" -> "c31fb696-ac7b-4ad7-90cf-73708407fb40" (a cloud library's id)."""
    m = re.fullmatch(r"db_([0-9a-f]{32})", db_name or "")
    if not m:
        return None
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace ``path`` with ``text`` in one step, flushed to disk, so a crash or power cut leaves either the old
    file or the new one, never half of one."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.rcb-tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)  # not narrowed by the umask: Resolve's own files are rw for everyone
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class ActiveLibrary:
    """Resolve's note of which library to open when it starts (``activedb.conf``), e.g.

        cloud*:c31fb696-ac7b-4ad7-90cf-73708407fb40      <- the * marks the one Resolve opens
        disk:Local Database

    Scripts can't switch into a cloud library, but a Resolve started after this file is changed opens the library
    it names. ``point_at`` keeps the original in ``saved`` until ``restore`` puts it back, so the user's own Resolve
    opens where they left it, even if this app stopped part way.
    """

    def __init__(self, path: Path, saved: Path):
        self.path, self.saved = Path(path), Path(saved)
        self._written: str | None = None

    def _read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _mode(self) -> int:
        try:
            return self.path.stat().st_mode & 0o777
        except OSError:
            return 0o666

    def current(self) -> str | None:
        """Id of the cloud library Resolve will open, or None if it will open something else."""
        m = re.search(r"^cloud\*:([0-9a-fA-F-]{36})\s*$", self._read() or "", re.M)
        return m.group(1).lower() if m else None

    def point_at(self, uuid: str) -> bool:
        text = self._read()
        if text is None or not re.search(r"^cloud\*:", text, re.M):
            return False  # only ever swap one cloud library for another
        new = re.sub(r"^cloud\*:.*$", f"cloud*:{uuid}", text, count=1, flags=re.M)
        if new == text:
            return True
        try:
            if not self.saved.exists():
                self.saved.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(self.saved, text)
            write_atomic(self.path, new, self._mode())
        except OSError as e:
            log.warning("Could not choose the library for the hidden Resolve: %s", e)
            return False
        self._written = new
        return True

    def restore(self) -> None:
        try:
            orig = self.saved.read_text(encoding="utf-8")
        except OSError:
            return
        now = self._read()
        if self._written is not None and now is not None and now != self._written:
            log.warning("Resolve's library choice changed while a hidden check ran; leaving it as it is")
        else:
            try:
                write_atomic(self.path, orig, self._mode())
            except OSError as e:
                log.error("Could not put back Resolve's library choice (%s). Its original is in %s", e, self.saved)
                return
        self._written = None
        try:
            self.saved.unlink()
        except OSError:
            pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


WATCHDOG_FLAG = "--rcb-watchdog"
DEADLINE_FILE = "hidden_deadline.json"
PROJECT_LIMIT = 60    # seconds the hidden Resolve gets to make a new project (normally about 15)
EXPORT_LIMIT = 180    # ...and to back up one project (normally well under a minute)


class TimeLimit:
    """A time limit on a call into the hidden Resolve that holds even if the call never comes back. Resolve's
    scripting library keeps Python's lock while it waits, so a stuck call freezes this whole app, timers and
    dashboard included. The limit is written to a file instead and the watchdog, a separate process, stops the
    hidden Resolve once it passes; the stuck call then returns."""

    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.fired = False

    @contextlib.contextmanager
    def __call__(self, pid: int | None, seconds: float, what: str):
        if pid is None:  # the user's own Resolve: never stopped by us
            yield self
            return
        self.fired = False
        try:
            write_atomic(self.path, json.dumps({"pid": pid, "until": self.clock() + seconds,
                                                     "seconds": seconds, "what": what}))
        except OSError as e:
            log.warning("Could not set a time limit for %s: %s", what, e)
        try:
            yield self
        finally:
            self.fired = bool((self.read() or {}).get("fired"))
            self.path.unlink(missing_ok=True)

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def overdue(self, pid: int) -> dict | None:
        """The limit for this Resolve, if it has passed and hasn't been acted on yet; marks it acted on."""
        d = self.read()
        if not d or d.get("pid") != pid or d.get("fired") or self.clock() < d.get("until", float("inf")):
            return None
        d["fired"] = True
        try:
            write_atomic(self.path, json.dumps(d))
        except OSError:
            pass
        return d


def watchdog_command(app_pid: int, resolve_pid: int, cfg_path: Path, conf: Path, saved: Path) -> list[str]:
    """How to start the watchdog: this same program (the packaged app's executable, or this script)."""
    arg = json.dumps({"app": app_pid, "resolve": resolve_pid, "config": str(cfg_path), "conf": str(conf),
                      "saved": str(saved), "deadline": str(Path(saved).parent / DEADLINE_FILE)})
    if getattr(sys, "frozen", False):
        return [sys.executable, WATCHDOG_FLAG, arg]
    return [sys.executable, str(Path(__file__).resolve()), WATCHDOG_FLAG, arg]


def start_watchdog(cmd: list[str]) -> None:
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError as e:
        log.warning("Could not start the watchdog for the hidden DaVinci Resolve: %s", e)


def run_watchdog(arg: str, alive=pid_alive, sleep=time.sleep, hidden_factory=None, clock=time.time) -> int:
    """Outlives this app for as long as a hidden Resolve it started runs. If the app stops (crash, force-quit)
    first, quit the hidden Resolve properly, put back the user's library choice and, if they had tried to open
    Resolve meanwhile, open it for them. If a call into the hidden Resolve runs past its TimeLimit, stop that
    Resolve so the app (frozen by the call) carries on. Otherwise it does nothing: the app tidies up itself."""
    a = json.loads(arg)
    app_pid, resolve_pid = int(a["app"]), int(a["resolve"])
    lib = ActiveLibrary(Path(a["conf"]), Path(a["saved"]))
    limit = TimeLimit(a["deadline"], clock=clock) if a.get("deadline") else None

    def hidden():
        try:
            cfg = load_config(Path(a["config"]))
        except (OSError, ValueError):
            cfg = copy.deepcopy(DEFAULTS)
        h = (hidden_factory or HiddenResolve)(cfg)
        h.adopt(resolve_pid)
        return h
    while True:
        if not alive(resolve_pid):
            if not alive(app_pid):
                lib.restore()
            return 0
        if not alive(app_pid):
            break
        over = limit.overdue(resolve_pid) if limit else None
        if over:
            log.warning("The hidden DaVinci Resolve stopped answering while %s (over %d s); stopping it",
                        over.get("what", "working"), over.get("seconds", 0))
            h = hidden()
            h._signal(signal.SIGTERM)
            if not h._wait_exit(20):
                h._signal(signal.SIGKILL)
                h._wait_exit(10)
            continue  # the app sees it gone and tidies up as usual
        sleep(1)
    log.warning("Colour Project Manager stopped while a hidden DaVinci Resolve was running; quitting it")
    h = hidden()
    h.on_exit = lib.restore
    h.quit()
    return 0


class HiddenResolve:
    """DaVinci Resolve started with -nogui for one backup cycle, so nothing flicks up on screen (macOS).

    Resolve runs one copy at a time, so this is only used while the user doesn't have Resolve open. Opening
    Resolve from the Dock or Finder while the hidden copy runs just brings the hidden copy to the front, with
    no window. ``wanted()`` notices that, so the cycle can stop, quit the hidden copy and open Resolve properly.
    """

    START_TIMEOUT = 300  # seconds for Resolve to start and answer scripting calls
    QUIT_TIMEOUT = 90

    def __init__(self, cfg: dict, connect=None, popen=subprocess.Popen, run=subprocess.run,
                 sleep=time.sleep, clock=time.monotonic, kill=os.kill):
        self.cfg = cfg
        self.exe = cfg.get("resolve_app") or RESOLVE_EXE
        self.connect_fn = connect or connect_resolve
        self.popen, self.run, self.sleep, self.clock, self.kill = popen, run, sleep, clock, kill
        self.proc = None
        self.pid: int | None = None
        self.resolve = None
        self.handed_over = False
        self._last_look = None
        self.on_exit = lambda: None   # runs once Resolve has quit, before it is reopened for the user
        self.library: str | None = None  # DbName this session asked Resolve to open, if any

    @staticmethod
    def available(cfg: dict) -> bool:
        return SYSTEM == "Darwin" and bool(cfg.get("background_resolve"))

    # ---- lifecycle
    def launch(self) -> bool:
        try:
            self.proc = self.popen([self.exe, "-nogui"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            log.error("Could not start DaVinci Resolve hidden (%s): %s", self.exe, e)
            return False
        self.pid = self.proc.pid
        log.info("Started DaVinci Resolve hidden for this backup (pid %s)", self.pid)
        return True

    def adopt(self, pid: int) -> None:
        """Take charge of a hidden Resolve left running earlier (for example if this app was force-quit)."""
        self.pid = pid
        log.info("Using the hidden DaVinci Resolve that is already running (pid %s)", pid)

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.pid is None:
            return False
        try:
            self.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def connect(self, cfg: dict, stopping=lambda: False):
        """Wait for the hidden Resolve to answer scripting calls with a library open. None if it never does."""
        start = self.clock()
        while self.clock() - start < self.START_TIMEOUT and not stopping():
            if not self.alive():
                log.error("Hidden DaVinci Resolve exited while starting up")
                return None
            try:
                r = self.connect_fn(cfg)
                pm = r.GetProjectManager() if r else None
                ready = bool(pm and pm.GetCurrentDatabase())
            except ResolveNotAnswering as e:  # still starting up; the worker has been replaced, try again
                log.info("Hidden DaVinci Resolve not answering yet: %s", e)
                ready = False
            if ready:
                self.resolve = r
                log.info("Hidden DaVinci Resolve ready after %.0f s", self.clock() - start)
                return r
            self.sleep(3)
        if not stopping():
            log.error("Hidden DaVinci Resolve didn't answer within %d s", self.START_TIMEOUT)
        return None

    def wanted(self) -> bool:
        """True once the user has brought the hidden Resolve to the front (they want to use Resolve)."""
        if self.handed_over:
            return True
        now = self.clock()
        if self._last_look is not None and now - self._last_look < 1:
            return False
        self._last_look = now
        if self.pid is not None and front_app_pid(self.run) == self.pid:
            log.info("DaVinci Resolve was opened while running hidden; handing it over")
            self.handed_over = True
        return self.handed_over

    def quit(self) -> None:
        """Quit the hidden Resolve; if the user asked for Resolve meanwhile, open it normally for them."""
        if self.alive():
            self._last_look = None
            self.wanted()  # one last look: the user may have opened Resolve during the last project
        if self.resolve is None and self.alive():
            # Still starting up. Killing it now makes Resolve show its crash reporter, so give it a minute
            # to answer scripting calls and ask it to quit properly.
            end = self.clock() + 60
            while self.resolve is None and self.alive() and self.clock() < end:
                self.resolve = self.connect_fn(self.cfg)
                if self.resolve is None:
                    self.sleep(3)
        if self.resolve is not None:
            try:
                self.resolve.Quit()
            except Exception as e:  # noqa: BLE001
                log.debug("Resolve.Quit() raised: %s", e)
        if not self._wait_exit(self.QUIT_TIMEOUT):
            log.warning("Hidden DaVinci Resolve didn't quit; stopping it")
            self._signal(signal.SIGTERM)
            if not self._wait_exit(20):
                self._signal(signal.SIGKILL)
                self._wait_exit(10)
        log.info("Quit hidden DaVinci Resolve")
        try:
            self.on_exit()
        except Exception:  # noqa: BLE001
            log.exception("Could not tidy up after the hidden DaVinci Resolve")
        if self.handed_over:
            app = self.exe.split("/Contents/MacOS/")[0]
            try:
                self.run(["open", "-a", app], timeout=30, check=False)
                log.info("Opened DaVinci Resolve for you")
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("Could not open DaVinci Resolve: %s", e)

    def _signal(self, sig) -> None:
        if self.pid is None:
            return
        try:
            self.kill(self.pid, sig)
        except OSError:
            pass

    def _wait_exit(self, seconds: float) -> bool:
        end = self.clock() + seconds
        while self.alive():
            if self.clock() >= end:
                return False
            self.sleep(0.5)
        return True


# ------------------------------------------------------------------ live status
class LiveStatus:
    """Thread-safe picture of what the service is doing right now.

    Written by the backup loop, read by the dashboard and the menu bar app.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._events = collections.deque(maxlen=300)
        self._d = {"state": "starting", "paused": False, "cycle": None, "last_cycle": None,
                   "next_run_at": None, "resolve": {"connected": False}}
        self._touched = time.monotonic()  # last time the service loop reported anything; see quiet_for()

    def set(self, **kw) -> None:
        with self._lock:
            self._d.update(kw)
            self._touched = time.monotonic()

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self._d.get(key))

    def update_cycle(self, **kw) -> None:
        with self._lock:
            self._touched = time.monotonic()
            if self._d["cycle"] is not None:
                self._d["cycle"] = {**self._d["cycle"], **kw}

    def quiet_for(self) -> float:
        """Seconds since the service loop last reported anything (it does every second when idle)."""
        with self._lock:
            return time.monotonic() - self._touched

    def event(self, level: str, msg: str) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "level": level, "msg": msg,
                                 "at": dt.datetime.now().isoformat(timespec="seconds")})

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            d = copy.deepcopy(self._d)
            d["events"] = [e for e in self._events if e["seq"] > since]
            d["event_seq"] = self._seq
        return d


class EventHandler(logging.Handler):
    """Feeds log lines into LiveStatus so the dashboard can show them live."""

    def __init__(self, status: LiveStatus):
        super().__init__(level=logging.INFO)
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.status.event(record.levelname, record.getMessage())
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------- settings from the UI
EDITABLE = ("backup_dir", "interval_minutes", "retry_minutes", "active_hours", "off_hours_interval_minutes",
            "background_resolve", "rotate_libraries", "skip_libraries", "initial_lookback_days",
            "include", "exclude", "load_to_export", "save_open_project", "switch_database", "database",
            "with_stills_and_luts", "max_exports_per_cycle", "min_free_gb", "retention", "project_setup",
            "vidchecker")


def validate_settings(inc: dict) -> tuple[dict, dict]:
    """Validate settings submitted from the dashboard. Returns (clean, errors-by-field)."""
    clean: dict = {}
    errs: dict = {}

    def whole(key, lo, hi, src=None, dest=None, label=None):
        src = inc if src is None else src
        dest = clean if dest is None else dest
        v = src[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or not lo <= v <= hi:
            errs[label or key] = f"Enter a whole number from {lo} to {hi}."
        else:
            dest[key] = int(v)

    for key, lo, hi in (("interval_minutes", 0, 1440), ("off_hours_interval_minutes", 0, 1440),
                        ("retry_minutes", 1, 240),
                        ("initial_lookback_days", 1, 3650), ("max_exports_per_cycle", 1, 1000),
                        ("min_free_gb", 0, 100000)):
        if key in inc:
            whole(key, lo, hi)
    for key in ("save_open_project", "switch_database", "with_stills_and_luts", "background_resolve",
                "rotate_libraries"):
        if key in inc:
            if isinstance(inc[key], bool):
                clean[key] = inc[key]
            else:
                errs[key] = "Choose on or off."
    if "load_to_export" in inc:
        if inc["load_to_export"] in ("never", "collab", "auto", "always"):
            clean["load_to_export"] = inc["load_to_export"]
        else:
            errs["load_to_export"] = "Choose never, collab, auto or always."
    for key in ("include", "exclude", "skip_libraries"):
        if key in inc:
            v = inc[key]
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                clean[key] = [x.strip() for x in v if x.strip()]
            else:
                errs[key] = "Enter one pattern per line."
    if "active_hours" in inc:
        v = inc["active_hours"]
        if v is None:
            clean["active_hours"] = None
        else:
            try:
                if not (isinstance(v, list) and len(v) == 2 and v[0] != v[1]):
                    raise ValueError
                for x in v:
                    dt.datetime.strptime(x, "%H:%M")
                clean["active_hours"] = [v[0], v[1]]
            except (ValueError, TypeError):
                errs["active_hours"] = "Use two different HH:MM times, for example 07:00 and 22:00."
    if "backup_dir" in inc:
        v = inc["backup_dir"]
        if not isinstance(v, str) or not v.strip():
            errs["backup_dir"] = "Choose a folder."
        else:
            p = Path(os.path.expanduser(v.strip()))
            if not p.is_absolute():
                errs["backup_dir"] = "Use a full path, for example /Users/you/ResolveCloudBackups."
            else:
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    probe = p / ".rcb-write-test"
                    probe.write_text("x")
                    probe.unlink()
                    clean["backup_dir"] = str(p)
                except OSError as e:
                    errs["backup_dir"] = f"Can't write to that folder: {e.strerror or e}."
    if "retention" in inc:
        r = inc["retention"]
        if isinstance(r, dict):
            sub: dict = {}
            for key, lo, hi in (("keep_all_hours", 1, 8760), ("keep_daily_days", 0, 3650), ("max_backups", 0, 10000)):
                if key in r:
                    whole(key, lo, hi, src=r, dest=sub, label=f"retention.{key}")
            if sub:
                clean["retention"] = sub
        else:
            errs["retention"] = "Invalid retention settings."
    if "database" in inc:
        d = inc["database"]
        if isinstance(d, dict) and all(d.get(k) is None or isinstance(d.get(k), str) for k in ("name", "type")):
            clean["database"] = {"name": (d.get("name") or "").strip() or None,
                                 "type": (d.get("type") or "").strip() or None}
        else:
            errs["database"] = "Invalid database settings."
    if "vidchecker" in inc:
        sub, sub_errs = vidchecker.validate_config(inc["vidchecker"])
        errs.update(sub_errs)
        if not sub_errs:
            clean["vidchecker"] = sub
    if "project_setup" in inc:
        sub, sub_errs = project_setup.validate_config(inc["project_setup"])
        errs.update(sub_errs)
        if not sub_errs:
            clean["project_setup"] = sub
    for key in inc:
        if key not in EDITABLE:
            errs[key] = "This setting can only be changed in config.json."
    return clean, errs


_CHOOSE_FOLDER_SCRIPT = """
on run argv
    activate
    set msg to item 1 of argv
    if (count of argv) > 1 then
        set f to choose folder with prompt msg default location (POSIX file (item 2 of argv))
    else
        set f to choose folder with prompt msg
    end if
    return POSIX path of f
end run
"""
_choose_lock = threading.Lock()
FOLDER_PROMPTS = {"backup": "Choose where Colour Project Manager saves your backups",
                  "templates": "Choose the folder that holds your folder templates",
                  "projects": "Choose the folder new jobs go in on this drive"}


def free_bytes(path) -> int:
    """Free space, right for big network shares too (Python's own figure wraps around on macOS above 16 TB)."""
    try:
        return project_setup.disk_usage(Path(path))[2]
    except (OSError, subprocess.SubprocessError, IndexError):
        return shutil.disk_usage(path).free


def can_choose_folder() -> bool:
    return SYSTEM == "Darwin" and shutil.which("osascript") is not None


def choose_folder(start: str | None = None, run=subprocess.run,
                  prompt: str = "Choose where Colour Project Manager saves your backups") -> dict:
    """Show the macOS folder picker on this Mac. Returns {"path": ...}, {"cancelled": True} or {"error": ...}."""
    if not can_choose_folder():
        return {"error": "Choosing a folder this way only works on a Mac. Type the path instead."}
    if not _choose_lock.acquire(blocking=False):
        return {"error": "A folder window is already open. Look for it behind your other windows."}
    try:
        args = ["osascript", "-e", _CHOOSE_FOLDER_SCRIPT, prompt]
        if start:
            p = Path(os.path.expanduser(start))
            while not p.is_dir() and p != p.parent:  # start at the nearest folder that exists
                p = p.parent
            if p.is_dir():
                args.append(str(p))
        try:
            r = run(args, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return {"cancelled": True}
        except OSError as e:
            return {"error": f"Couldn't open the folder window: {e}"}
        if r.returncode != 0:
            if "-128" in (r.stderr or ""):  # the user pressed Cancel
                return {"cancelled": True}
            return {"error": f"Couldn't open the folder window: {(r.stderr or '').strip() or r.returncode}"}
        path = (r.stdout or "").strip()
        return {"path": path.rstrip("/") or "/"} if path else {"cancelled": True}
    finally:
        _choose_lock.release()


def apply_settings(path: Path | None, clean: dict) -> None:
    """Merge validated settings into config.json, preserving everything else."""
    p = Path(path) if path else default_config_path()
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        cur = {}
    cur = _merge(cur, clean)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


# ------------------------------------------------------------------------- backup
class Backup:
    def __init__(self, cfg: dict, state_file: Path, connect=None, clock=None, sleep=None, alert=None,
                 status: LiveStatus | None = None, library_names=None):
        self.cfg = cfg
        self.library_names = library_names or cloud_library_names
        self.alert = alert or self._run_alert
        self.state_file = Path(state_file)
        self.connect = connect or connect_resolve
        self.clock = clock or dt.datetime.now
        self.sleep = sleep or (lambda s: threading.Event().wait(s))
        self.limit = Backup.no_limit  # wraps each call into Resolve; see TimeLimit
        self.status = status or LiveStatus()
        self._t0 = time.monotonic()

    @property
    def root(self) -> Path:
        return Path(self.cfg["backup_dir"])

    # ---- live progress
    def _begin(self, now: dt.datetime, state: dict) -> None:
        last = state.get("last_cycle") or {}
        self.status.set(state="running", cycle={
            "started_at": now.isoformat(timespec="seconds"), "phase": "connecting", "current": None,
            "seen": 0, "exported": 0, "unchanged": 0, "skipped_old": 0, "failed": 0, "filtered": 0,
            "expected": last.get("seen") or None})

    def _progress(self, stats: dict, **kw) -> None:
        self.status.update_cycle(seen=stats["seen"], exported=stats["exported"], unchanged=stats["unchanged"],
                                 skipped_old=stats["skipped_old"], failed=stats["failed"],
                                 filtered=stats["filtered"], **kw)

    # ---- state
    def load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        st.setdefault("projects", {})
        return st

    def save_state(self, st: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_file)

    # ---- one cycle
    def cycle(self, connect=None, interrupted=None, hidden: bool = False, expect_db: str | None = None) -> dict:
        """One pass over the open library. ``connect`` overrides how Resolve is reached (the hidden copy);
        ``interrupted()`` returning True stops the pass early, between projects."""
        stats = {"status": "ok", "seen": 0, "exported": 0, "unchanged": 0, "skipped_old": 0,
                 "filtered": 0, "deferred": 0, "failed": 0, "backoff": 0, "files": []}
        now = self.clock()
        self._t0 = time.monotonic()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free_gb = free_bytes(self.root) / 1e9
        except OSError as e:
            log.error("Backup folder %s is not available: %s", self.root, e)
            stats["status"] = "backup_dir_unavailable"
            return self._finish(stats, self.load_state())
        if free_gb < self.cfg["min_free_gb"]:
            log.error("Only %.1f GB free in %s (min_free_gb=%s); skipping cycle",
                      free_gb, self.root, self.cfg["min_free_gb"])
            stats["status"] = "low_disk"
            return self._finish(stats, self.load_state())

        state = self.load_state()
        self._begin(now, state)
        if hidden:
            self.status.update_cycle(phase="starting_resolve")
        resolve = (connect or self.connect)(self.cfg)
        if not resolve:
            log.warning("DaVinci Resolve not reachable (is it running, with external scripting set to Local?)")
            stats["status"] = "resolve_unavailable"
            self.status.set(resolve={"connected": False})
            return self._finish(stats, state)
        try:
            return self._cycle_connected(resolve, stats, state, now, interrupted, hidden, expect_db)
        except ResolveNotAnswering as e:
            log.error("Stopped this check: %s. It carries on next time", e)
            stats["status"] = "resolve_not_answering"
            self.status.set(resolve={"connected": False})
            return self._finish(stats, state)

    def _cycle_connected(self, resolve, stats: dict, state: dict, now: dt.datetime, interrupted, hidden: bool,
                         expect_db: str | None) -> dict:
        pm = resolve.GetProjectManager()
        if not pm:
            stats["status"] = "no_project_manager"
            return self._finish(stats, state)

        orig_db = pm.GetCurrentDatabase() or {}
        cur = pm.GetCurrentProject()
        open_name = cur.GetName() if cur else None
        try:
            version = resolve.GetVersionString()
        except Exception:  # noqa: BLE001
            version = None
        self.status.set(resolve={"connected": True, "hidden": hidden, "version": version,
                                 "database": orig_db.get("DbName"),
                                 "library": self.known_library_names(state).get(orig_db.get("DbName")),
                                 "database_type": orig_db.get("DbType"), "open_project": open_name})
        self.status.update_cycle(phase="scanning")
        if expect_db:
            state.setdefault("library_attempts", {})[expect_db] = now.isoformat(timespec="seconds")
            if orig_db.get("DbName") != expect_db:
                log.warning("Asked the hidden Resolve to open library %s but it opened %s; trying the next one",
                            self.known_library_names(state).get(expect_db, expect_db), orig_db.get("DbName"))
                stats["status"] = "library_not_opened"
                return self._finish(stats, state)

        want = self.cfg["database"]
        target, switched = orig_db, False
        if want.get("name"):
            cands = [d for d in (pm.GetDatabaseList() or [])
                     if d.get("DbName") == want["name"] and (not want.get("type") or d.get("DbType") == want["type"])]
            if not cands:
                log.error("Database %r not found. Resolve reports: %s", want["name"],
                          [(d.get("DbType"), d.get("DbName")) for d in (pm.GetDatabaseList() or [])])
                stats["status"] = "database_not_found"
                return self._finish(stats, state)
            target = cands[0]
            if not _same_db(target, orig_db):
                if not self.cfg["switch_database"]:
                    log.error("Resolve's current database is %r, not %r. Open it in Resolve, or set "
                              "\"switch_database\": true (only switches when no project is open).",
                              orig_db.get("DbName"), want["name"])
                    stats["status"] = "database_not_current"
                    return self._finish(stats, state)
                if open_name:
                    log.warning("Project %r is open; not switching database this cycle", open_name)
                    stats["status"] = "project_open_cannot_switch"
                    return self._finish(stats, state)
                if not pm.SetCurrentDatabase(target):
                    stats["status"] = "switch_failed"
                    return self._finish(stats, state)
                switched = True
                open_name = None

        orig_path = [] if switched else current_folder_path(pm)
        ctx = {"pm": pm, "state": state, "stats": stats, "now": now, "db": target.get("DbName") or "default",
               "folder": self._library_folder(target, state),
               # a hidden (-nogui) Resolve stops answering scripts once it loads a project, so never load there
               "open_name": open_name, "can_load": open_name is None and not hidden, "exports": 0, "touched": set(),
               "interrupted": interrupted or (lambda: False)}
        try:
            if open_name and self.cfg["save_open_project"]:
                pm.SaveProject()
            pm.GotoRootFolder()
            self._walk(pm, [], 0, ctx)
        except CycleInterrupted:
            stats["status"] = "interrupted"
            log.info("Stopped this check part way through; it carries on next time")
        except ResolveNotAnswering:
            ctx["lost"] = True  # nothing more can be asked of Resolve this check
            raise
        finally:
            try:
                if ctx.get("lost"):
                    pass
                elif switched:
                    pm.SetCurrentDatabase(orig_db)
                else:
                    goto_path(pm, orig_path)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not restore Resolve's previous location: %s", e)
            self._retention(ctx["touched"], now, state)
        if stats["status"] == "ok":
            self._remember_database(target, ctx, state)
        return self._finish(stats, state)

    # ---- library names and folders
    @staticmethod
    def _db_key(db: dict) -> str:
        return f"{db.get('DbName') or 'default'}@{db.get('IpAddress') or ''}"

    def known_library_names(self, state: dict) -> dict[str, str]:
        """Names from Resolve's logs, remembered in state so they outlive Resolve's log rotation."""
        names = state.setdefault("library_names", {})
        try:
            names.update(self.library_names())
        except Exception as e:  # noqa: BLE001
            log.debug("Could not read cloud library names from Resolve's logs: %s", e)
        return names

    def _library_folder(self, db: dict, state: dict) -> str:
        """Backup folder for a database: the library's name as shown in Resolve. A folder made earlier under
        another name (the internal db_... name, or before the library was renamed) is renamed to match."""
        db_name = db.get("DbName") or "default"
        name = self.known_library_names(state).get(db_name, db_name)
        recs = state.setdefault("databases", {})
        key = self._db_key(db)
        if any(r.get("folder") == name for k, r in recs.items() if k != key):
            name = f"{name} ({db_name[-6:]})"  # two libraries with the same name
        old = (recs.get(key) or {}).get("folder") or db_name
        if old != name:
            src, dst = self.root / safe_name(old), self.root / safe_name(name)
            if src.is_dir() and not dst.exists():
                try:
                    os.rename(src, dst)
                    log.info("Renamed backup folder %s -> %s", src.name, dst.name)
                    for k, v in state["projects"].items():
                        if k.startswith(f"{db_name}::") and v.get("last_file", "").startswith(str(src) + os.sep):
                            v["last_file"] = str(dst) + v["last_file"][len(str(src)):]
                except OSError as e:
                    log.warning("Could not rename backup folder %s to %s: %s", src, dst, e)
                    name = old
            elif src.is_dir():
                log.warning("Not renaming backup folder %s: %s already exists", src, dst)
                name = old
        recs.setdefault(key, {})["folder"] = name
        return name

    def _remember_database(self, db: dict, ctx: dict, state: dict) -> None:
        """Record each database (cloud library) checked, so the dashboard can show libraries going stale and the
        hidden Resolve can take them in turn."""
        at = ctx["now"].isoformat(timespec="seconds")
        rec = state.setdefault("databases", {}).setdefault(self._db_key(db), {})
        rec.setdefault("first_seen", at)
        rec.update({k: db.get(k) for k in ("DbType", "DbName", "IpAddress")}, last_checked=at,
                   projects=sum(1 for k in state["projects"] if k.startswith(f"{ctx['db']}::")),
                   folders=ctx.get("top_folders", []))

    def _finish(self, stats: dict, state: dict) -> dict:
        at = self.clock().isoformat(timespec="seconds")
        summary = {"at": at, "duration_s": round(time.monotonic() - self._t0, 1),
                   **{k: v for k, v in stats.items() if k != "files"}}
        # projects still failing, including ones sitting out a retry back-off this cycle
        summary["failing"] = sum(1 for v in state.get("projects", {}).values() if (v.get("failures") or 0) > 0)
        state["last_cycle"] = summary
        hist = state.setdefault("history", [])
        hist.append({k: summary[k] for k in ("at", "status", "exported", "failed", "unchanged", "seen",
                                             "duration_s")})
        del hist[:-500]
        self.save_state(state)
        self.status.set(cycle=None, last_cycle=summary)
        log.info("Cycle %s: seen=%d exported=%d unchanged=%d old=%d failed=%d deferred=%d",
                 stats["status"], stats["seen"], stats["exported"], stats["unchanged"],
                 stats["skipped_old"], stats["failed"], stats["deferred"])
        return stats

    # ---- tree walk
    def _walk(self, pm, path: list[str], depth: int, ctx: dict) -> None:
        if depth > 20:
            log.warning("Folder nesting too deep at %s; stopping descent", "/".join(path))
            return
        attrs = {}
        getter = getattr(pm, "GetProjectAttributesInCurrentFolder", None)
        if getter:
            try:
                attrs = getter() or {}
            except ResolveNotAnswering:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("GetProjectAttributesInCurrentFolder failed: %s", e)
        names = list(pm.GetProjectListInCurrentFolder() or [])
        names += [n for n in attrs if n not in names]
        for name in names:
            if ctx["interrupted"]():
                raise CycleInterrupted
            self._handle(pm, path, name, attrs.get(name) or {}, ctx)
        folders = list(pm.GetFolderListInCurrentFolder() or [])
        if depth == 0:
            ctx["top_folders"] = folders[:50]
        for folder in folders:
            if ctx["interrupted"]():
                raise CycleInterrupted
            if not pm.OpenFolder(folder):
                log.warning("Could not open folder %r", folder)
                continue
            try:
                self._walk(pm, path + [folder], depth + 1, ctx)
            finally:
                pm.GotoParentFolder()

    @staticmethod
    def _match(patterns: list[str], full: str) -> bool:
        return any(fnmatch.fnmatch(full, p) or full == p.rstrip("/") or full.startswith(p.rstrip("/") + "/")
                   for p in patterns)

    def _handle(self, pm, path: list[str], name: str, attrs: dict, ctx: dict) -> None:
        stats, state, now = ctx["stats"], ctx["state"], ctx["now"]
        full = "/".join(path + [name])
        key = f"{ctx['db']}::{full}"
        stats["seen"] += 1
        inc, exc = self.cfg["include"], self.cfg["exclude"]
        if (inc and not self._match(inc, full)) or (exc and self._match(exc, full)):
            stats["filtered"] += 1
            self._progress(stats)
            return
        self._progress(stats, current=full)

        raw = attrs.get("lastModifiedDate")
        mod = None if raw in (None, "") else str(raw)
        rec = state["projects"].get(key)

        nxt = parse_ts(rec.get("next_try")) if rec else None
        if nxt and now < nxt:
            stats["backoff"] += 1  # failing project: waiting out its retry backoff
            return
        if rec and mod and rec.get("last_modified") == mod:
            stats["unchanged"] += 1
            return
        if rec is None and mod:
            t = parse_ts(mod)
            if t and (now - t) > dt.timedelta(days=self.cfg["initial_lookback_days"]):
                state["projects"][key] = {"last_modified": mod, "backed_up": False}
                stats["skipped_old"] += 1
                return
        if rec and not mod:
            last = parse_ts(rec.get("last_backup"))
            if last and (now - last) < dt.timedelta(minutes=self.cfg["fallback_min_interval_minutes"]):
                stats["unchanged"] += 1
                return
        if ctx["exports"] >= self.cfg["max_exports_per_cycle"]:
            stats["deferred"] += 1  # picked up next cycle; state not updated
            return

        ctx["exports"] += 1
        collab = truthy(attrs.get("liveCollaborationMode"))
        self._progress(stats, current=full, phase="exporting")
        lost = None
        try:
            out, reason = self._export(pm, path, name, ctx, collab)
        except ResolveNotAnswering as e:  # counted as a failure, so a project that hangs Resolve waits its turn
            out, reason, lost = None, f"DaVinci Resolve stopped answering while backing it up ({e})", e
        entry = state["projects"].setdefault(key, {})
        if out:
            entry.update(last_modified=mod, last_backup=now.isoformat(timespec="seconds"),
                         last_file=str(out), failures=0, backed_up=True)
            for k in ("next_try", "last_error"):
                entry.pop(k, None)
            stats["exported"] += 1
            stats["files"].append(str(out))
            ctx["touched"].add(out.parent)
            log.info("Backed up %s -> %s", full, out.name)
        else:
            n = entry["failures"] = entry.get("failures", 0) + 1
            wait = min(5 * 2 ** (n - 1), self.cfg["retry_backoff_max_minutes"])
            entry["last_error"] = reason
            entry["next_try"] = (now + dt.timedelta(minutes=wait)).isoformat(timespec="seconds")
            stats["failed"] += 1
            (log.error if n >= self.cfg["alert_after_failures"] else log.warning)(
                "Export failed for %s (%d consecutive; retry in %d min): %s", full, n, wait, reason)
            if n == self.cfg["alert_after_failures"]:
                self.alert(full, ctx["folder"], n, reason)
        self._progress(stats, current=full, phase="scanning")
        self.save_state(state)
        if lost:
            raise lost
        if self.cfg["pause_between_exports_seconds"]:
            self.sleep(self.cfg["pause_between_exports_seconds"])

    # ---- export
    def project_dir(self, folder: str, path: list[str], name: str) -> Path:
        return self.root.joinpath(safe_name(folder), *[safe_name(p) for p in path], safe_name(name))

    @staticmethod
    def no_limit(what):
        return contextlib.nullcontext()

    def _direct(self, pm, name: str, tmp: Path) -> bool:
        try:
            with self.limit(f"backing up {name}"):
                pm.ExportProject(name, str(tmp), self.cfg["with_stills_and_luts"])
        except ResolveNotAnswering:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("ExportProject(%r) raised: %s", name, e)
        return tmp.exists() and tmp.stat().st_size > 0

    def _via_load(self, pm, name: str, tmp: Path) -> bool:
        proj = None
        try:
            with self.limit(f"opening {name}"):
                proj = pm.LoadProject(name)
            if not proj:
                return False
            return self._direct(pm, name, tmp)
        except ResolveNotAnswering:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Load-and-export of %r failed: %s", name, e)
            return False
        finally:
            if proj:
                try:
                    pm.CloseProject(proj)
                except Exception:  # noqa: BLE001
                    pass

    def _run_alert(self, project: str, db: str, failures: int, reason: str) -> None:
        cmd = self.cfg.get("alert_command")
        if not cmd:
            return
        env = dict(os.environ, RCB_PROJECT=project, RCB_DATABASE=db,
                   RCB_FAILURES=str(failures), RCB_REASON=reason)
        try:
            subprocess.run(cmd, shell=True, env=env, timeout=60, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("alert_command failed: %s", e)

    def _export(self, pm, path: list[str], name: str, ctx: dict, collab: bool = False):
        """Returns (final_path | None, failure_reason)."""
        dest = self.project_dir(ctx["folder"], path, name)
        dest.mkdir(parents=True, exist_ok=True)
        base = f"{safe_name(name)}__{ctx['now'].strftime('%Y%m%d-%H%M%S')}"
        final, n = dest / f"{base}.drp", 0
        while final.exists():
            n += 1
            final = dest / f"{base}-{n}.drp"
        tmp = dest / f".partial-{final.name}"
        tmp.unlink(missing_ok=True)

        mode = self.cfg["load_to_export"]
        may_load = ctx["can_load"] and (mode in ("auto", "always") or (mode == "collab" and collab))
        ok, reason = False, ""
        if mode == "always" and may_load:
            ok = self._via_load(pm, name, tmp)
            reason = "" if ok else "load-and-export failed"
        else:
            ok = self._direct(pm, name, tmp)
            if not ok:
                if may_load:
                    tmp.unlink(missing_ok=True)
                    ok = self._via_load(pm, name, tmp)
                    reason = "" if ok else "direct export and load-and-export both failed"
                elif not ctx["can_load"] and not ctx["open_name"]:
                    reason = "direct export failed; projects aren't opened in the hidden Resolve, so it's retried later"
                elif not ctx["can_load"]:
                    reason = (f"direct export failed; not loading because {ctx['open_name']!r} is open "
                              "in this Resolve")
                elif mode == "never":
                    reason = "direct export failed (load_to_export=never)"
                else:
                    reason = ("direct export failed; project may be open elsewhere and isn't in "
                              "collaboration mode, so it was not loaded")
        if ok:
            os.replace(tmp, final)
            return final, ""
        tmp.unlink(missing_ok=True)
        return None, reason

    # ---- retention
    def _retention(self, touched: set, now: dt.datetime, state: dict) -> None:
        dirs = set(touched)
        r = self.cfg["retention"]
        # Sweep every project folder once a day, and straight away when the retention settings change.
        sweep = f"{now.date().isoformat()} {json.dumps(r, sort_keys=True)}"
        if state.get("last_full_sweep") != sweep:
            dirs |= {p.parent for p in self.root.rglob("*__*.drp") if not p.name.startswith(".partial-")}
            state["last_full_sweep"] = sweep
        for d in dirs:
            files = [(p, backup_time(p)) for p in d.glob("*.drp") if not p.name.startswith(".partial-")]
            for p in select_deletions(files, now, r["keep_all_hours"], r["keep_daily_days"], r.get("max_backups") or 0):
                try:
                    p.unlink()
                    log.info("Pruned %s", p)
                except OSError as e:
                    log.warning("Could not prune %s: %s", p, e)
            for stale in d.glob(".partial-*.drp"):
                try:
                    if (now - dt.datetime.fromtimestamp(stale.stat().st_mtime)) > dt.timedelta(hours=1):
                        stale.unlink()
                except OSError:
                    pass


# -------------------------------------------------------------------------- CLI
def setup_logging(verbose: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    logdir = app_dir() / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(logdir / "backup.log", maxBytes=1_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def cmd_once(cfg: dict) -> int:
    lock = SingleInstance(app_dir() / "cycle.lock")
    if not lock.acquire():
        log.info("Another backup cycle is running; exiting")
        return 0
    try:
        stats = Backup(cfg, app_dir() / "state.json").cycle()
    finally:
        lock.release()
    return 0 if stats["status"] == "ok" and not stats["failed"] else 1


# ------------------------------------------------------------------ the service
def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


class Service:
    """Scheduling loop. ``tick()`` decides what to do now and returns how long to sleep."""

    def __init__(self, cfg_path: Path | None = None, status: LiveStatus | None = None, connect=None,
                 clock=None, wall=time.time, state_dir: Path | None = None, hidden_factory=None,
                 processes=resolve_processes, active_library: ActiveLibrary | None = None, watchdog=None):
        self.cfg_path = Path(cfg_path) if cfg_path else default_config_path()
        self.dir = Path(state_dir) if state_dir else app_dir()
        self.cfg = load_config(self.cfg_path)
        self._cfg_sig = self._signature()
        self.status = status or LiveStatus()
        self.wall = wall
        self._stop = threading.Event()
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self._run_now = False
        self._cancel_run = threading.Event()  # "Stop" for the check that's running
        self._in_cycle = False
        self.paused = False
        self.next_run = wall()  # first cycle straight away
        # Every call into Resolve goes through a separate process with a time limit, so a slow or stuck Resolve
        # can't freeze the dashboard and menu bar (see resolve_worker). Tests pass their own ``connect``.
        self.worker = resolve_worker.Worker()
        self.isolated = connect is None
        connect = connect or (lambda cfg: self.worker.connect(*script_paths(cfg)))
        self.run = subprocess.run  # for ps and friends; tests swap it
        self.hidden_marker = self.dir / "hidden_resolve.json"
        self._reclaim_checked = False
        self._powering_off = False
        self._memory_warned: int | None = None
        self.hidden_factory = hidden_factory or (lambda cfg: HiddenResolve(cfg, connect=connect))
        self.processes = processes
        self.active_library = active_library or ActiveLibrary(resolve_prefs_dir() / "activedb.conf",
                                                              self.dir / "activedb.conf.saved")
        self.watchdog = watchdog or (lambda pid: start_watchdog(watchdog_command(
            os.getpid(), pid, self.cfg_path, self.active_library.path, self.active_library.saved)))
        self.time_limit = TimeLimit(Path(self.active_library.saved).parent / DEADLINE_FILE)
        self.backup = Backup(self.cfg, self.dir / "state.json", connect=connect, clock=clock,
                             status=self.status, sleep=lambda s: self._stop.wait(s))
        # reads config.json itself, so a drive added from the dashboard works at once, even mid-backup
        self.frameio = frameio.FrameIO(lambda: load_config(self.cfg_path), self.dir)
        self.vidchecker = vidchecker.Vidchecker(lambda: load_config(self.cfg_path))
        self.vidchecker.warm_roots()  # the server takes up to a minute to list its drives
        self.setup = project_setup.ProjectSetup(
            lambda: load_config(self.cfg_path), self.dir,
            known_names=lambda: self.backup.load_state()["projects"].keys(),
            libraries=lambda: list(self.backup.known_library_names(self.backup.load_state()).values()),
            frameio=self.frameio, fetch=project_setup.fetch_text,
            library_names=lambda: self.backup.known_library_names(self.backup.load_state()),
            template_file=self.template_backup)
        self.status.set(paused=False, state="starting")
        last = self.backup.load_state().get("last_cycle")
        if last:
            self.status.set(last_cycle=last)
        if self.active_library.saved.exists() and HiddenResolve.available(self.cfg) and not self.processes():
            log.info("Putting back Resolve's library choice, left changed when this app last stopped")
            self.active_library.restore()

    # ---- controls (called from the dashboard thread)
    def stop(self) -> None:
        self._stop.set()
        self.wake.set()

    def close(self) -> None:
        """After the loop has stopped: let the worker process go."""
        self.worker.close()

    def power_off(self) -> None:
        """The Mac is restarting, shutting down or logging out: stop, and stop a hidden Resolve at once, before
        macOS notes it as an open app to reopen (with its window) at the next login."""
        log.info("The Mac is shutting down or logging out; stopping")
        self._powering_off = True  # keep the hidden_marker: if macOS reopens Resolve anyway, it's taken back
        self.stop()
        for pid, args in self.processes():
            if " -nogui" in args:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_run_now(self) -> bool:
        with self._lock:
            if self.status.get("state") == "running":
                return False
            self._run_now = True
        log.info("Back up now requested")
        self.wake.set()
        return True

    # ---- QC (Vidchecker): the jobs and files that can be checked
    def _qc_bases(self) -> list[Path]:
        """Folders that hold one folder per job (the Deliverables location: exports are checked there)."""
        out = []
        for loc in self.setup.cfg()["extras"]:
            pat = loc.get("path", "")
            if pat.endswith("/{job}") and "{" not in pat[:-len("/{job}")]:
                out.append(Path(pat[:-len("/{job}")]))
        return out

    def qc_jobs(self, limit: int = 300) -> list[str]:
        jobs = []
        for base in self._qc_bases():
            try:
                jobs += [(d.stat().st_mtime, d.name) for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
            except OSError:
                continue
        return [n for _, n in sorted(jobs, reverse=True)][:limit]

    def qc_files(self, job: str) -> list[dict]:
        if not isinstance(job, str) or not job or "/" in job or job.startswith("."):
            return []
        out = []
        for base in self._qc_bases():
            if (base / job).is_dir():
                out += vidchecker.media_files(base / job)
        return out

    def qc_allowed(self, path: str) -> bool:
        """Only exported media inside a job folder can be sent for checking."""
        try:
            p = Path(path).resolve()
        except (OSError, ValueError):
            return False
        return p.suffix.lower() in vidchecker.MEDIA and any(base.resolve() in p.parents for base in self._qc_bases())

    def request_stop_run(self) -> bool:
        """Stop the backup check that's running, between projects. The next one runs as scheduled."""
        if not self._in_cycle:
            return False
        self._cancel_run.set()
        log.info("Stop requested: this backup check ends after the current project")
        return True

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.status.set(paused=paused)
        log.info("Backups paused" if paused else "Backups resumed")
        self.wake.set()

    # ---- config hot reload
    def _signature(self):
        try:
            st = self.cfg_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload_config(self) -> None:
        sig = self._signature()
        if sig == self._cfg_sig:
            return
        self._cfg_sig = sig
        try:
            cfg = load_config(self.cfg_path)
        except (OSError, ValueError) as e:
            log.error("config.json could not be read (%s); keeping the previous settings", e)
            return
        self.cfg = cfg
        self.backup.cfg = cfg
        self.next_run = min(self.next_run, self.wall() + max(cfg["interval_minutes"] * 60, 60))
        log.info("Settings reloaded")

    # ---- schedule
    def _wait_after_cycle(self) -> float:
        """Seconds until the next cycle: interval_minutes in active hours (0 = continuously, a minute apart),
        off_hours_interval_minutes outside them, but never past the start of the next active hours."""
        now = self.backup.clock()
        window = self.cfg["active_hours"]
        if in_active_hours(window, now):
            return max(self.cfg["interval_minutes"] * 60, 60)
        off = self.cfg.get("off_hours_interval_minutes") or 0
        until_open = self._until_window(window, now)
        return max(min(off * 60, until_open) if off else until_open, 60)

    @staticmethod
    def _until_window(window, now: dt.datetime) -> float:
        try:
            a = dt.datetime.strptime(window[0], "%H:%M").time()
        except (ValueError, IndexError, TypeError):
            return 60.0
        start = dt.datetime.combine(now.date(), a)
        if start <= now:
            start += dt.timedelta(days=1)
        return (start - now).total_seconds()

    # ---- hidden Resolve
    def _hidden_session(self):
        """A HiddenResolve to run this cycle in, or None to use the Resolve that's open (or report it missing)."""
        if not HiddenResolve.available(self.cfg):
            return None
        procs = self.processes()
        hidden = [pid for pid, args in procs if " -nogui" in args]
        if procs and len(hidden) < len(procs):
            if not self._reclaim_reopened(procs):
                return None  # the user has Resolve open
            procs, hidden = [], []
        h = self.hidden_factory(self.cfg)
        h.on_exit = self._hidden_exited
        if procs:
            h.adopt(hidden[0])
            self._mark_hidden(h.pid)
            self.watchdog(h.pid)
            return h
        lib = self._next_library() if self.cfg.get("rotate_libraries") else None
        if lib and self.active_library.point_at(library_uuid(lib)):
            h.library = lib
            log.info("Hidden check of library %s", self.backup.known_library_names(self.backup.load_state())
                     .get(lib, lib))
        if h.launch():
            self._mark_hidden(h.pid)
            self.watchdog(h.pid)
            return h
        self.active_library.restore()
        return None

    # A hidden Resolve still running when the Mac restarts or logs out is reopened by macOS at the next login as an
    # ordinary Resolve, window and all; the app then took it for the user's and left it open for days. A note
    # (hidden_marker) says a hidden Resolve is running, so after a restart that one can be told apart and taken back.
    def _mark_hidden(self, pid: int | None) -> None:
        try:
            write_atomic(self.hidden_marker, json.dumps({"pid": pid, "boot": boot_id(self.run)}))
        except OSError as e:
            log.debug("Could not note the hidden Resolve: %s", e)

    def _hidden_exited(self) -> None:
        if not self._powering_off:
            self.hidden_marker.unlink(missing_ok=True)
        self.active_library.restore()

    def _reclaim_reopened(self, procs) -> bool:
        """True after quitting a Resolve that macOS reopened at login because it was running hidden when the Mac
        last stopped. Only when it is plainly not in use: it started with this login, isn't in front and has no
        project open. Checked once per run of the app; anything else is the user's Resolve and is left alone."""
        if self._reclaim_checked:
            return False
        self._reclaim_checked = True
        try:
            note = json.loads(self.hidden_marker.read_text())
        except (OSError, ValueError):
            return False
        self.hidden_marker.unlink(missing_ok=True)  # one look only, whatever is decided
        boot = boot_id(self.run)
        gui = [pid for pid, args in procs if " -nogui" not in args]
        if not boot or note.get("boot") in (None, boot) or len(gui) != 1 or len(procs) != 1:
            return False
        pid = gui[0]
        age, since_login = process_age(pid, self.run), login_age(self.run)
        if age is None or since_login is None or abs(age - since_login) > 300:
            return False  # opened later than the login: someone opened it
        if front_app_pid(self.run) == pid:
            return False
        try:
            r = self.backup.connect(self.cfg)
            pm = r.GetProjectManager() if r else None
            cur = pm.GetCurrentProject() if pm else None
            name = cur.GetName() if cur else None
        except Exception as e:  # noqa: BLE001
            log.info("Left the reopened DaVinci Resolve alone: couldn't see what it has open (%s)", e)
            return False
        if not pm or name not in (None, "", "Untitled Project"):
            return False
        log.warning("DaVinci Resolve was reopened by macOS at login because it was running hidden for backups when "
                    "the Mac stopped. Nothing is open in it, so it's being quit and backups go back to running hidden")
        h = self.hidden_factory(self.cfg)
        h.on_exit = self.active_library.restore  # macOS reopened it on the library the hidden check had chosen
        h.adopt(pid)
        h.resolve = r
        h.quit()
        return not h.alive()

    def _check_own_resolve(self, procs) -> None:
        """Watch the memory of a Resolve the user has open. It is never stopped by this app; past
        resolve_memory_warn_gb the dashboard and menu bar ask for a restart, and alert_command runs once."""
        gui = [pid for pid, args in procs if " -nogui" not in args]
        if not gui:
            self.status.set(resolve_health=None)
            return
        pid, limit = gui[0], self.cfg.get("resolve_memory_warn_gb") or 0
        gb = process_memory_gb(pid, self.run)
        warn = bool(limit) and gb is not None and gb >= limit
        self.status.set(resolve_health={"pid": pid, "memory_gb": None if gb is None else round(gb, 1),
                                        "limit_gb": limit, "warn": warn})
        if warn and self._memory_warned != pid:
            self._memory_warned = pid
            msg = (f"DaVinci Resolve is using {gb:.0f} GB of memory, so backups are slow. "
                   "Quit and reopen Resolve when you can.")
            log.warning(msg)
            self.backup.alert("DaVinci Resolve", "", 0, msg)

    def _next_library(self) -> str | None:
        """The cloud library (DbName) checked longest ago, never-checked ones first; None if none is known."""
        state = self.backup.load_state()
        names = self.backup.known_library_names(state)
        skip = set(self.cfg.get("skip_libraries") or [])
        last: dict[str, str] = {}
        for rec in state.get("databases", {}).values():
            if rec.get("DbName"):
                last[rec["DbName"]] = max(last.get(rec["DbName"], ""), rec.get("last_checked") or "")
        for db, at in (state.get("library_attempts") or {}).items():
            last[db] = max(last.get(db, ""), at or "")
        cands = [db for db, name in names.items() if library_uuid(db) and name not in skip and db not in skip]
        return min(cands, key=lambda db: (last.get(db, ""), names[db].lower())) if cands else None

    # ---- the loop body
    def _run_cycle(self) -> None:
        lock = SingleInstance(self.dir / "cycle.lock")
        if not lock.acquire():
            log.info("Another backup cycle is running; will check again shortly")
            self.next_run = self.wall() + 60
            return
        self._cancel_run.clear()
        self._in_cycle = True
        hidden = None
        try:
            if self.isolated:
                self._check_own_resolve(self.processes())
            hidden = self._hidden_session()
            if hidden:
                self.status.set(resolve_health=None)
                self.backup.limit = lambda what: self._limits(hidden.pid, EXPORT_LIMIT, what)
                status = self.backup.cycle(
                    connect=lambda cfg: hidden.connect(cfg, lambda: self.stopping),
                    interrupted=lambda: self.stopping or hidden.wanted() or self.setup.has_pending()
                    or self._cancel_run.is_set(), hidden=True,
                    expect_db=hidden.library)["status"]
            else:
                self.backup.limit = lambda what: self._limits(None, self.own_export_limit(), what)
                status = self.backup.cycle(interrupted=lambda: self.stopping or self.setup.has_pending()
                                           or self._cancel_run.is_set())["status"]
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error in backup cycle")
            status = "error"
            self.status.set(cycle=None)
        finally:
            try:
                if hidden:
                    hidden.quit()
                    self.status.set(resolve={"connected": False})
            except Exception:  # noqa: BLE001
                log.exception("Could not quit the hidden DaVinci Resolve")
            self.backup.limit = Backup.no_limit
            self._in_cycle = False
            lock.release()
        retry = status in ("resolve_unavailable", "error", "no_project_manager", "backup_dir_unavailable",
                           "resolve_not_answering")
        wait = self.cfg["retry_minutes"] * 60 if retry else self._wait_after_cycle()
        self.next_run = self.wall() + wait * random.uniform(0.95, 1.05)

    def own_export_limit(self) -> float:
        return max(1, self.cfg.get("own_resolve_export_limit_minutes") or 15) * 60

    @contextlib.contextmanager
    def _limits(self, hidden_pid: int | None, seconds: float, what: str):
        """Both time limits on one call into Resolve. For the hidden Resolve, the watchdog stops it after
        ``seconds`` (TimeLimit) and the worker gives up 30 s later if even that didn't free the call; in the
        user's own Resolve only the worker gives up, and Resolve is left running."""
        with self.time_limit(hidden_pid, seconds, what) as t, \
                self.worker.limit(seconds + 30 if hidden_pid else seconds, what):
            yield t

    def _run_setup_jobs(self) -> None:
        lock = SingleInstance(self.dir / "cycle.lock")
        if not lock.acquire():
            return  # a backup run by another process; try again next tick
        try:
            while not self.stopping:
                job = self.setup.next_resolve_job()
                if not job:
                    break
                self.setup.update_job(job["id"], state="running", detail="Connecting to DaVinci Resolve")
                try:
                    res = self._make_project(job)
                except Exception:  # noqa: BLE001
                    log.exception("Making the Resolve project for %s failed", job["job"])
                    res = {"state": "failed", "message": "Unexpected error. See the log."}
                (log.info if res["state"] == "done" else log.warning)("New project %s, Resolve: %s",
                                                                       job["job"], res["message"])
                self.setup.update_job(job["id"], detail=None, **res)
        finally:
            lock.release()

    def template_backup(self, library: str, template: str) -> str | None:
        """The newest backup (.drp) of a project in a library, e.g. the template project, or None."""
        state = self.backup.load_state()
        for db, name in self.backup.known_library_names(state).items():
            if library in (db, name):
                f = (state["projects"].get(f"{db}::{template}") or {}).get("last_file")
                if f and os.path.isfile(f):
                    return f
        return None

    def _make_project(self, job: dict) -> dict:
        def fail(msg):
            return {"state": "failed", "message": msg}
        names = self.backup.known_library_names(self.backup.load_state())
        db = next((d for d, n in names.items() if n == job["library"] or d == job["library"]), None)
        if not db:
            return fail(f"The library {job['library']} hasn't been seen yet. Open it once in Resolve.")
        procs = self.processes()
        hidden_pids = [pid for pid, args in procs if " -nogui" in args]
        h = None
        try:
            if procs and len(hidden_pids) < len(procs):  # the user's own Resolve
                resolve = self.backup.connect(self.cfg)
                if not resolve:
                    return fail("DaVinci Resolve is open but isn't answering. Check Preferences > System > General "
                                "> External scripting using is set to Local, then press Retry.")
            elif HiddenResolve.available(self.cfg):
                if hidden_pids:  # a hidden copy left running: quit it, it may be on another library
                    old = self.hidden_factory(self.cfg)
                    old.on_exit = self._hidden_exited
                    old.adopt(hidden_pids[0])
                    old.quit()
                h = self.hidden_factory(self.cfg)
                h.on_exit = self._hidden_exited
                uuid = library_uuid(db)
                if not uuid or not self.active_library.point_at(uuid):
                    return fail("Couldn't choose the library for the hidden Resolve. Open Resolve, then press Retry.")
                h.library = db
                self.setup.update_job(job["id"], detail="Starting DaVinci Resolve hidden")
                if not h.launch():
                    self.active_library.restore()
                    h = None
                    return fail("DaVinci Resolve didn't start. Press Retry, or open Resolve first.")
                self._mark_hidden(h.pid)
                self.watchdog(h.pid)
                resolve = h.connect(self.cfg, lambda: self.stopping)
                if not resolve:
                    return fail("The hidden DaVinci Resolve didn't answer. Press Retry.")
            else:
                return fail("Open DaVinci Resolve, then press Retry.")
            pm = resolve.GetProjectManager()
            cur = (pm.GetCurrentDatabase() or {}).get("DbName") if pm else None
            if cur != db:
                return fail(f"Resolve has {names.get(cur, cur) or 'another library'} open, not {job['library']}. "
                            "Switch library in Resolve's Project Manager, or close Resolve, then press Retry.")
            self.setup.update_job(job["id"], detail="Making the project")
            with tempfile.TemporaryDirectory() as tmp:
                src = None
                if job.get("template_library") and job["template_library"] != job["library"]:
                    tpl = "/".join(job["template_path"] + [job["template"]])
                    backup = self.template_backup(job["template_library"], tpl)
                    if not backup:
                        return fail(f"There's no backup of the template project {tpl} to copy into {job['library']}.")
                    src = Path(tmp) / "template.drp"
                    shutil.copyfile(backup, src)  # a copy, so Resolve never works on the backup itself
                limit = PROJECT_LIMIT if h else self.own_export_limit()
                with self._limits(h.pid if h else None, limit, f"making {job['project']}") as t:
                    res = make_resolve_project(pm, job, Path(tmp), template_drp=src)
                if t.fired:
                    log.warning("Gave up making %s: the hidden DaVinci Resolve stopped answering", job["project"])
                    return fail(f"DaVinci Resolve stopped answering while making the project, so it was stopped "
                                f"after {PROJECT_LIMIT} seconds. The project may have been made anyway: "
                                "press Retry, which won't make a second copy.")
                return res
        except ResolveNotAnswering as e:
            log.warning("Gave up making %s: %s", job["project"], e)
            return fail("DaVinci Resolve stopped answering while making the project. It may have been made anyway: "
                        "press Retry, which won't make a second copy.")
        finally:
            if h:
                try:
                    h.quit()
                except Exception:  # noqa: BLE001
                    log.exception("Could not quit the hidden DaVinci Resolve")
                self.status.set(resolve={"connected": False})

    def tick(self) -> float:
        self.reload_config()
        if self.setup.has_pending():  # a new project's Resolve part goes before backups, even when paused
            self._run_setup_jobs()
            self.next_run = min(self.next_run, self.wall() + 60)
        with self._lock:
            forced, self._run_now = self._run_now, False
        if self.paused and not forced:
            self.status.set(state="paused", next_run_at=None)
            return 1.0
        in_window = in_active_hours(self.cfg["active_hours"], self.backup.clock())
        off_hours = bool(self.cfg.get("off_hours_interval_minutes"))
        now = self.wall()
        if forced or now >= self.next_run:
            if forced or in_window or off_hours:
                self._run_cycle()
                now = self.wall()
            else:
                self.next_run = now + 60  # look again in a minute
        last = self.status.get("last_cycle") or {}
        if not in_window:
            state, nxt = "outside_active_hours", (_iso(self.next_run) if off_hours else None)
        else:
            state = "waiting_for_resolve" if last.get("status") == "resolve_unavailable" else "idle"
            nxt = _iso(self.next_run)
        self.status.set(state=state, next_run_at=nxt)
        return max(0.05, min(1.0, self.next_run - now))


# --------------------------------------------------------------- self-healing
HEALTH_FLAG = "--rcb-health-check"
HEALTH_STALE = 45 * 60  # a quiet loop longer than this is stuck: longer than the slowest allowed backup of a project
HEALTH_FAILS = 5        # health checks (a minute apart) failed in a row before the app is restarted
HEALTH_FILE = "health.json"


def relaunch_command(executable: str | None = None) -> list[str] | None:
    """How to start the Mac app again (open -a on its bundle); None when not running as the packaged app."""
    if not getattr(sys, "frozen", False) and executable is None:
        return None
    parts = Path(executable or sys.executable).resolve().parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].endswith(".app"):
            return ["/usr/bin/open", "-a", str(Path(*parts[:i + 1]))]
    return None


def _fetch_health(port: int, timeout: float = 20) -> bool:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"Host": f"127.0.0.1:{port}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def run_health_check(directory: Path | None = None, fetch=_fetch_health, alive=pid_alive, kill=os.kill,
                     run=subprocess.run, sleep=time.sleep) -> int:
    """Run by launchd once a minute (see menubar.install_health_check). If the app crashed, start it again; if it
    has stopped answering for HEALTH_FAILS checks in a row, stop it and start it again. A hidden Resolve it was
    using is quit by its own watchdog. An app that was quit on purpose removes service.json and is left alone."""
    d = Path(directory) if directory else app_dir()
    info = read_service_info(d)
    path = d / HEALTH_FILE
    if not info or not info.get("pid"):
        path.unlink(missing_ok=True)
        return 0
    try:
        fails = int(json.loads(path.read_text()).get("fails", 0))
    except (OSError, ValueError, AttributeError):
        fails = 0
    pid = int(info["pid"])

    def restart(why: str) -> None:
        log.warning("%s; starting Colour Project Manager again", why)
        (d / "service.json").unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        cmd = info.get("relaunch")
        if not cmd:
            log.error("Don't know how to start Colour Project Manager again; open it yourself")
            return
        try:
            run(cmd, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.error("Could not start Colour Project Manager again: %s", e)

    if not alive(pid):
        restart("Colour Project Manager stopped without quitting properly")
        return 0
    if fetch(int(info.get("port") or 0)):
        if fails:
            log.info("Colour Project Manager is answering again")
        path.unlink(missing_ok=True)
        return 0
    fails += 1
    log.warning("Colour Project Manager didn't answer its health check (%d of %d)", fails, HEALTH_FAILS)
    if fails < HEALTH_FAILS:
        write_atomic(path, json.dumps({"fails": fails}))
        return 0
    for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            kill(pid, sig)
        except OSError:
            break
        for _ in range(wait * 2):
            if not alive(pid):
                break
            sleep(0.5)
        if not alive(pid):
            break
    restart(f"Colour Project Manager stopped answering for {HEALTH_FAILS} minutes, so it was stopped")
    return 0


# --------------------------------------------------------------- the dashboard
def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def load_or_create_token(path: Path) -> str:
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    _write_private(path, tok)
    return tok


def read_service_info(directory: Path | None = None) -> dict | None:
    """What the menu bar app and `dashboard` command use to find the running service."""
    try:
        return json.loads(((directory or app_dir()) / "service.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def primary_ip() -> str | None:
    """This machine's address on the network with the default route (no packet is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


class BonjourAlias:
    """Publishes name.local -> this machine's current IP with macOS dns-sd, re-publishing when the IP changes."""

    def __init__(self, names: list[str], port: int):
        self.names = [n for n in names if n.endswith(".local")]
        self.port = port
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []

    def start(self) -> None:
        if self.names and SYSTEM == "Darwin" and os.path.exists("/usr/bin/dns-sd"):
            # a copy of this app that quit without stopping (force quit, crash) leaves its dns-sd running
            subprocess.run(["/usr/bin/pkill", "-f", "^/usr/bin/dns-sd -P Colour Project Manager "], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Thread(target=self._loop, name="bonjour", daemon=True).start()

    def _loop(self) -> None:
        ip = None
        while not self._stop.is_set():
            now = primary_ip()
            if now != ip or any(p.poll() is not None for p in self._procs):
                self._kill()
                ip = now
                if ip:
                    for n in self.names:
                        self._procs.append(subprocess.Popen(
                            ["/usr/bin/dns-sd", "-P", f"Colour Project Manager ({n})", "_http._tcp", "local",
                             str(self.port), n, ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    log.info("Dashboard advertised on the network as %s (%s)", ", ".join(self.names), ip)
            self._stop.wait(30)
        self._kill()

    def _kill(self) -> None:
        for p in self._procs:
            p.terminate()
        self._procs = []

    def stop(self) -> None:
        self._stop.set()
        self._kill()


class DashboardServer:
    """Web UI + JSON API on 127.0.0.1, and on the network when lan_hostnames is set.
    Every request needs the token and a Host header naming this dashboard (DNS-rebinding defence)."""

    def __init__(self, service: Service, port: int = 8765, html_path: Path | None = None,
                 token: str | None = None, state_dir: Path | None = None, lan_hostnames=(),
                 lan_web_port: int | None = None, lan_token: bool = True):
        self.svc = service
        self.lan_hostnames = [str(n).strip().lower().rstrip(".") for n in lan_hostnames if str(n).strip()]
        self.lan_web_port = lan_web_port
        self.lan_token = lan_token
        self.web_port = None  # lan_web_port once it is actually listening
        self.httpd_web = None
        self.bonjour = None
        self.base_port = port
        self.dir = Path(state_dir) if state_dir else service.dir
        self.html_path = Path(html_path) if html_path else resource_path("dashboard.html")
        self.token = token or load_or_create_token(self.dir / "dashboard.token")
        self.started = dt.datetime.now()
        self.httpd = None
        self.port = None
        self._storage_cache = None

    # ---- lifecycle
    def start(self) -> None:
        handler = _make_handler(self)
        err = None
        for p in ([0] if self.base_port == 0 else range(self.base_port, self.base_port + 20)):
            try:
                self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0" if self.lan_hostnames else "127.0.0.1", p),
                                                             handler)
                break
            except OSError as e:
                err = e
        else:
            raise err  # type: ignore[misc]
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True).start()
        if self.lan_hostnames and self.lan_web_port is not None:
            try:
                self.httpd_web = http.server.ThreadingHTTPServer(("0.0.0.0", self.lan_web_port), handler)
            except OSError as e:
                log.warning("Dashboard could not also use port %s (%s); use port %d instead",
                            self.lan_web_port, e, self.port)
            else:
                self.httpd_web.daemon_threads = True
                self.web_port = self.httpd_web.server_address[1]
                threading.Thread(target=self.httpd_web.serve_forever, name="dashboard-web", daemon=True).start()
        if self.lan_hostnames:
            self.bonjour = BonjourAlias(self.lan_hostnames, self.web_port or self.port)
            self.bonjour.start()
        _write_private(self.dir / "service.json", json.dumps({
            "pid": os.getpid(), "port": self.port, "token": self.token, "version": VERSION,
            "started_at": self.started.isoformat(timespec="seconds"), "dashboard": self.url(),
            "relaunch": relaunch_command()}))

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def hosts(self, lan: bool = True) -> set[str]:
        """Host header values this dashboard answers to. lan=False: this machine's own names only."""
        names = ["127.0.0.1", "localhost"] + (self.lan_hostnames if lan else [])
        out = {f"{n}:{self.port}" for n in names}
        if lan and self.web_port:
            out |= {f"{n}:{self.web_port}" for n in self.lan_hostnames}
            if self.web_port == 80:  # browsers leave the default port out of Host and Origin
                out |= set(self.lan_hostnames)
        return out

    def lan_url(self, name: str) -> str:
        port = self.web_port or self.port
        return f"http://{name}{'' if port == 80 else f':{port}'}/{'?t=' + self.token if self.lan_token else ''}"

    def open_host(self, host: str) -> bool:
        """True when this Host may skip the token (a LAN name, with lan_token off)."""
        return not self.lan_token and host.rsplit(":", 1)[0] in self.lan_hostnames

    def frameio_redirect(self) -> str:
        """Where Adobe sends the browser back to. Must be listed on the Adobe credential."""
        return f"http://localhost:{self.port}/frameio/callback"

    def stop(self) -> None:
        if self.bonjour:
            self.bonjour.stop()
        if self.httpd_web:
            self.httpd_web.shutdown()
            self.httpd_web.server_close()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        try:
            (self.dir / "service.json").unlink()
        except OSError:
            pass

    def health(self) -> tuple[bool, dict]:
        """Whether the service loop is still making progress. Calls into Resolve have time limits, so a loop that
        has been quiet for HEALTH_STALE seconds is stuck on something else (a hung network drive, a bug)."""
        quiet = self.svc.status.quiet_for()
        ok = quiet < HEALTH_STALE
        return ok, {"ok": ok, "pid": os.getpid(), "version": VERSION, "quiet_s": round(quiet),
                    "resolve_call_s": round(self.svc.worker.busy_for())}

    # ---- payloads
    def status_payload(self, since: int = 0) -> dict:
        d = self.svc.status.snapshot(since)
        cfg = self.svc.cfg
        d["service"] = {"pid": os.getpid(), "version": VERSION, "now": dt.datetime.now().isoformat(timespec="seconds"),
                        "started_at": self.started.isoformat(timespec="seconds")}
        d["config"] = {k: cfg[k] for k in ("interval_minutes", "off_hours_interval_minutes", "active_hours",
                                           "backup_dir", "load_to_export", "background_resolve")}
        return d

    def _storage(self) -> dict:
        if self._storage_cache and time.monotonic() - self._storage_cache[0] < 30:
            return self._storage_cache[1]
        root = self.svc.backup.root
        files = size = 0
        free = None
        try:
            for p in root.rglob("*.drp"):
                if p.name.startswith(".partial-"):
                    continue
                try:
                    size += p.stat().st_size
                    files += 1
                except OSError:
                    pass
            free = free_bytes(root)
        except OSError:
            pass
        val = {"dir": str(root), "files": files, "bytes": size, "free_bytes": free}
        self._storage_cache = (time.monotonic(), val)
        return val

    def library_payload(self) -> dict:
        st = self.svc.backup.load_state()
        rows = []
        for key, v in st["projects"].items():
            db, _, full = key.partition("::")
            folder, _, name = full.rpartition("/")
            if v.get("failures", 0) > 0:
                status = "failing"
            elif v.get("backed_up"):
                status = "ok"
            else:
                status = "not_changed_recently"
            size = None
            if v.get("last_file"):
                try:
                    size = os.stat(v["last_file"]).st_size
                except OSError:
                    pass
            rows.append({"key": key, "database": db, "folder": folder, "name": name, "status": status,
                         "last_backup": v.get("last_backup"), "last_modified": v.get("last_modified"),
                         "failures": v.get("failures", 0), "last_error": v.get("last_error"),
                         "next_try": v.get("next_try"), "size": size})
        names = st.get("library_names", {})
        dbs = [{**{k: v.get(k) for k in ("DbType", "DbName", "IpAddress", "first_seen", "last_checked", "projects",
                                         "folders", "folder")}, "name": names.get(v.get("DbName"), v.get("DbName"))}
               for v in st.get("databases", {}).values() if v.get("DbName")]
        checked = {d["DbName"] for d in dbs}
        # Cloud libraries the signed-in user can see but that haven't been open in Resolve while this app ran.
        dbs += [{"DbType": "Cloud", "DbName": k, "name": n, "last_checked": None} for k, n in names.items()
                if k not in checked]
        return {"projects": rows, "databases": dbs, "history": st.get("history", [])[-400:],
                "storage": self._storage()}

    def settings_payload(self) -> dict:
        cfg = self.svc.cfg
        return {"settings": {k: cfg[k] for k in EDITABLE}, "config_path": str(self.svc.cfg_path),
                "can_choose_folder": can_choose_folder(), "can_run_hidden": SYSTEM == "Darwin",
                "dashboard_port": self.port}


def _make_handler(dash: DashboardServer):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "resolve-cloud-backup"

        def log_message(self, fmt, *args):  # keep the log quiet
            log.debug("dashboard: " + fmt, *args)

        def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8", extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorise(self, header_only: bool):
            """Returns (path, query) or None after having sent an error."""
            host = (self.headers.get("Host") or "").lower()
            if host not in dash.hosts():
                self._send(403, {"error": "Wrong host"})
                return None
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if dash.open_host(host):
                return parsed.path, qs
            tok = self.headers.get("X-RCB-Token") or ("" if header_only else (qs.get("t") or [""])[0])
            if not secrets.compare_digest(tok.encode("utf-8"), dash.token.encode("utf-8")):
                self._send(401, {"error": "Missing or wrong access token. Open the dashboard from the menu bar "
                                          "icon, or run: resolve_cloud_backup.py dashboard"})
                return None
            return parsed.path, qs

        def _frameio_callback(self):
            """Where Adobe sends the browser back after signing in to Frame.io. It can't carry the dashboard's
            token, so it's guarded by the one-time sign-in state instead."""
            host = (self.headers.get("Host") or "").lower()
            if host not in dash.hosts(lan=False):
                self._send(403, {"error": "Wrong host"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            one = lambda k: (qs.get(k) or [""])[0][:4096]  # noqa: E731
            if one("error"):
                ok, text = False, f"Adobe didn't sign in: {one('error_description') or one('error')}."
            else:
                try:
                    who = dash.svc.frameio.finish_login(one("code"), one("state"))
                    ok, text = True, f"Frame.io is connected as {who}."
                    log.info("Frame.io connected as %s", who)
                except frameio.FrameIOError as e:
                    ok, text = False, str(e)
            if not ok:
                log.warning("Frame.io sign-in failed: %s", text)
            page = ("<!doctype html><meta charset=utf-8><title>Frame.io</title><body style=\"font:16px -apple-system,"
                    "sans-serif;max-width:32em;margin:15vh auto;padding:0 20px\"><h1 style=\"font-size:22px\">"
                    f"{'Connected' if ok else 'Not connected'}</h1><p>{html.escape(text)}</p><p>"
                    f"{'You can close this tab and go back to Colour Project Manager.' if ok else 'Close this tab and try Connect Frame.io again.'}"
                    "</p></body>")
            self._send(200 if ok else 400, page.encode("utf-8"), "text/html; charset=utf-8",
                       {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

        def do_GET(self):
            try:
                if urllib.parse.urlparse(self.path).path == "/frameio/callback":
                    self._frameio_callback()
                    return
                if urllib.parse.urlparse(self.path).path == "/healthz":  # for the health check; says nothing private
                    if (self.headers.get("Host") or "").lower() not in dash.hosts(lan=False):
                        self._send(403, {"error": "Wrong host"})
                        return
                    ok, body = dash.health()
                    self._send(200 if ok else 503, body)
                    return
                auth = self._authorise(header_only=False)
                if not auth:
                    return
                path, qs = auth
                if path in ("/", "/new", "/qc", "/frameio", "/settings", "/backups"):
                    csp = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                           "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
                    self._send(200, dash.html_path.read_bytes(), "text/html; charset=utf-8",
                               {"Content-Security-Policy": csp})
                elif path == "/api/status":
                    since = int((qs.get("since") or ["0"])[0] or 0)
                    self._send(200, dash.status_payload(since))
                elif path == "/api/library":
                    self._send(200, dash.library_payload())
                elif path == "/api/settings":
                    self._send(200, dash.settings_payload())
                elif path == "/api/project-setup":
                    fresh = (qs.get("fresh") or [""])[0] == "1"
                    self._send(200, {**dash.svc.setup.payload(fresh), "can_choose_folder": can_choose_folder(),
                                     "frameio_redirect": dash.frameio_redirect()})
                elif path == "/api/vidchecker":
                    vc = dash.svc.vidchecker
                    st = vc.status()
                    tasks, problem = [], st["problem"]
                    if st["ok"]:
                        try:
                            tasks = vc.tasks(min(200, max(1, int((qs.get("limit") or ["40"])[0] or 40))))
                        except (vidchecker.VidcheckerError, ValueError) as e:
                            problem = str(e)
                    self._send(200, {**st, "problem": problem, "tasks": tasks, "url": vc.cfg()["url"],
                                     "jobs": dash.svc.qc_jobs()})
                elif path == "/api/vidchecker/files":
                    self._send(200, {"files": dash.svc.qc_files((qs.get("job") or [""])[0])})
                elif path == "/api/drives":
                    self._send(200, {"drives": dash.svc.setup.drives_status()})
                elif path == "/api/frameio/workspaces":
                    try:
                        self._send(200, {"ok": True, "workspaces": dash.svc.frameio.workspaces()})
                    except frameio.FrameIOError as e:
                        self._send(200, {"ok": False, "error": str(e)})
                elif path == "/api/frameio/recent":
                    try:
                        fresh = (qs.get("fresh") or ["0"])[0] == "1"
                        if not dash.svc.frameio.connected():
                            raise frameio.NotConnected("Frame.io isn't connected. Connect it in Settings.")
                        self._send(200, {"ok": True, **dash.svc.frameio.recent_now(fresh=fresh)})
                    except frameio.FrameIOError as e:
                        self._send(200, {"ok": False, "error": str(e), "connected": dash.svc.frameio.connected()})
                elif path == "/favicon.ico":
                    self._send(204, b"", "image/x-icon")
                else:
                    self._send(404, {"error": "Not found"})
            except (BrokenPipeError, ConnectionResetError):
                log.debug("Dashboard request: the browser went away before the answer was sent")
            except Exception:  # noqa: BLE001
                log.exception("Dashboard request failed")
                self._send(500, {"error": "Internal error. See the log."})

        def do_POST(self):
            try:
                # Always consume the body first, so a rejected request can't leave bytes behind
                # that the next request on the same connection would be parsed from.
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= length <= 65536:
                        raise ValueError
                except ValueError:
                    self.close_connection = True
                    self._send(400, {"error": "Bad Content-Length"}, extra={"Connection": "close"})
                    return
                raw = self.rfile.read(length) if length else b"{}"
                auth = self._authorise(header_only=True)
                if not auth:
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + h for h in dash.hosts()}:
                    self._send(403, {"error": "Wrong origin"})
                    return
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    self._send(400, {"error": "Send a JSON object"})
                    return
                path = auth[0]
                svc = dash.svc
                if path == "/api/vidchecker/start":
                    files, tpl = body.get("files"), body.get("template")
                    if not isinstance(files, list) or not files or len(files) > 50 or not isinstance(tpl, int):
                        self._send(400, {"error": "Choose the files and the check."})
                        return
                    results = []
                    for f in files:
                        if not isinstance(f, str) or not svc.qc_allowed(f):
                            results.append({"file": str(f), "ok": False, "error": "Only exports in a job folder can be checked."})
                            continue
                        try:
                            r = svc.vidchecker.start(f, tpl)
                            log.info("QC: started %s on %s", r["template"], r["server_path"])
                            results.append({"file": f, "ok": True, **r})
                        except vidchecker.VidcheckerError as e:
                            log.warning("QC: couldn't start a check of %s: %s", f, e)
                            results.append({"file": f, "ok": False, "error": str(e)})
                    self._send(200, {"ok": all(r["ok"] for r in results), "results": results})
                elif path == "/api/stop-run":
                    self._send(200, {"ok": svc.request_stop_run()})
                elif path == "/api/run-now":
                    self._send(200, {"ok": svc.request_run_now(), "running": svc.status.get("state") == "running"})
                elif path == "/api/choose-folder":
                    start = body.get("start")
                    prompt = FOLDER_PROMPTS.get(body.get("purpose"), FOLDER_PROMPTS["backup"])
                    self._send(200, choose_folder(start if isinstance(start, str) else None, prompt=prompt))
                elif path == "/api/project-setup/preview":
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    self._send(200, svc.setup.plan(fields, body.get("drive"), body.get("library")))
                elif path == "/api/project-setup/create":
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    r = svc.setup.create(fields, body.get("drive"), body.get("library"))
                    for res in r.get("results", []):
                        if res["ok"] and res["kind"] == "frameio":
                            log.info("New project %s: made the Frame.io project %s with %d folders", r["job"],
                                     res["path"], res["folders"])
                        elif res["ok"]:
                            log.info("New project %s: made %d folders in %s", r["job"], res["folders"], res["path"])
                        else:
                            log.warning("New project %s: nothing made in %s: %s", r["job"], res["path"], res["error"])
                    if r.get("resolve") and r["resolve"]["state"] == "queued":
                        log.info("New project %s: the Resolve project is queued", r["job"])
                        svc.wake.set()
                    self._send(200, r)
                elif path == "/api/project-setup/retry":
                    r = svc.setup.retry(body.get("id"))
                    if r["ok"]:
                        svc.wake.set()
                    self._send(200, r)
                elif path == "/api/frameio/login":
                    try:
                        self._send(200, {"ok": True, "url": svc.frameio.login_url(dash.frameio_redirect())})
                    except frameio.FrameIOError as e:
                        self._send(200, {"ok": False, "error": str(e)})
                elif path == "/api/frameio/secret":
                    secret = body.get("secret")
                    try:
                        svc.frameio.save_secret(secret if isinstance(secret, str) else "")
                        log.info("Frame.io client secret %s", "saved in the keychain" if secret else "removed")
                        self._send(200, {"ok": True})
                    except frameio.FrameIOError as e:
                        self._send(200, {"ok": False, "error": str(e)})
                elif path == "/api/frameio/disconnect":
                    svc.frameio.disconnect()
                    log.info("Frame.io disconnected")
                    self._send(200, {"ok": True})
                elif path == "/api/pause":
                    svc.set_paused(bool(body.get("paused", True)))
                    self._send(200, {"ok": True, "paused": svc.paused})
                elif path == "/api/settings":
                    clean, errs = validate_settings(body)
                    if errs:
                        self._send(400, {"ok": False, "errors": errs})
                        return
                    apply_settings(svc.cfg_path, clean)
                    log.info("Settings saved from the dashboard: %s", ", ".join(sorted(clean)) or "no changes")
                    svc.wake.set()
                    self._send(200, {"ok": True, "message": "Saved. New settings apply from the next cycle."})
                else:
                    self._send(404, {"error": "Not found"})
            except (BrokenPipeError, ConnectionResetError):
                log.debug("Dashboard request: the browser went away before the answer was sent")
            except Exception:  # noqa: BLE001
                log.exception("Dashboard request failed")
                self._send(500, {"error": "Internal error. See the log."})

    return Handler


class ServiceRunner:
    """The whole service (scheduler + dashboard) in the current process, on a background thread.

    Used by `run` (headless) and by the standalone Mac app, which hosts it next to the menu bar icon.
    """

    def __init__(self, cfg_path: Path | None = None):
        self.cfg_path = cfg_path
        self.svc = None
        self.dash = None
        self.thread = None
        self._lock = None
        self._handler = None

    def start(self) -> bool:
        """False if another copy of the service already owns this user's backups."""
        lock = SingleInstance(app_dir() / "daemon.lock")
        if not lock.acquire():
            return False
        self._lock = lock
        status = LiveStatus()
        self._handler = EventHandler(status)
        log.addHandler(self._handler)
        self.svc = Service(self.cfg_path, status=status)
        cfg = self.svc.cfg
        if cfg["dashboard"]["enabled"]:
            try:
                self.dash = DashboardServer(self.svc, port=cfg["dashboard"]["port"],
                                            lan_hostnames=cfg["dashboard"].get("lan_hostnames") or [],
                                            lan_web_port=cfg["dashboard"].get("lan_web_port"),
                                            lan_token=cfg["dashboard"].get("lan_token", True))
                self.dash.start()
                self.svc.setup.refresh_scan()  # so the first New project page doesn't wait for the drives
                log.info("Dashboard running at http://127.0.0.1:%d/", self.dash.port)
                for n in self.dash.lan_hostnames:
                    log.info("Dashboard also on the network at %s", self.dash.lan_url(n).split("?")[0])
            except OSError as e:
                log.error("Dashboard could not start: %s", e)
                self.dash = None
        log.info("Service started: %s -> %s", f"every {cfg['interval_minutes']} min" if cfg["interval_minutes"]
                 else "continuously", cfg["backup_dir"])
        self.thread = threading.Thread(target=self._loop, name="backup-service", daemon=True)
        self.thread.start()
        return True

    def _loop(self) -> None:
        svc = self.svc
        while not svc.stopping:
            try:
                wait = svc.tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("Unexpected error in the service loop")
                wait = 60.0
            svc.wake.wait(wait)
            svc.wake.clear()

    def join(self) -> None:
        while self.thread and self.thread.is_alive():
            self.thread.join(0.5)

    def stop(self) -> None:
        if self.svc:
            self.svc.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(30)  # long enough to quit a hidden Resolve
        if self.svc:
            self.svc.close()
        if self.dash:
            self.dash.stop()
            self.dash = None
        if self._handler:
            log.removeHandler(self._handler)
            self._handler = None
        if self._lock:
            self._lock.release()
            self._lock = None
        log.info("Service stopped")


def cmd_run(cfg: dict, cfg_path: Path | None = None) -> int:
    runner = ServiceRunner(cfg_path)
    if not runner.start():
        log.error("Another resolve-cloud-backup service is already running")
        return 1
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: runner.svc.stop())
    try:
        runner.join()
    finally:
        runner.stop()
    return 0


def cmd_dashboard(cfg: dict) -> int:
    info = read_service_info()
    if not info:
        print("The service isn't running (or its dashboard is switched off). Start it, or run: "
              "resolve_cloud_backup.py run")
        return 1
    print(info["dashboard"])
    webbrowser.open(info["dashboard"])
    return 0


def cmd_list(cfg: dict) -> int:
    resolve = connect_resolve(cfg)
    if not resolve:
        print("Could not reach DaVinci Resolve. Is it running, with Preferences > System > General > "
              "External scripting using = Local?")
        return 1
    pm = resolve.GetProjectManager()
    print("Resolve:", resolve.GetVersionString())
    print("Current database:", pm.GetCurrentDatabase())
    print("Databases:")
    for d in pm.GetDatabaseList() or []:
        print("  ", d)
    cur = pm.GetCurrentProject()
    print("Open project:", cur.GetName() if cur else None)
    print("\nProjects in the current database (raw attributes from Resolve):")
    start = current_folder_path(pm)
    pm.GotoRootFolder()

    def walk(path):
        attrs = (getattr(pm, "GetProjectAttributesInCurrentFolder", lambda: {})() or {})
        for n in pm.GetProjectListInCurrentFolder() or []:
            print(f"  {'/'.join(path + [n])}\n      {attrs.get(n)}")
        for f in list(pm.GetFolderListInCurrentFolder() or []):
            if pm.OpenFolder(f):
                walk(path + [f])
                pm.GotoParentFolder()

    try:
        walk([])
    finally:
        goto_path(pm, start)
    return 0


def cmd_probe(cfg: dict, target: str | None, try_load: bool) -> int:
    """Test, for one project, which export routes work right now (e.g. while it's open elsewhere)."""
    if not target:
        print('Usage: probe "Folder/Sub/Project Name" [--try-load]')
        return 2
    resolve = connect_resolve(cfg)
    if not resolve:
        print("Could not reach DaVinci Resolve (running? External scripting = Local?)")
        return 1
    pm = resolve.GetProjectManager()
    cur = pm.GetCurrentProject()
    open_name = cur.GetName() if cur else None
    parts = target.strip("/").split("/")
    name = parts[-1]
    start = current_folder_path(pm)
    tmpdir = Path(tempfile.mkdtemp(prefix="rcb-probe-"))
    try:
        pm.GotoRootFolder()
        for f in parts[:-1]:
            if not pm.OpenFolder(f):
                print(f"Folder {f!r} not found in database {pm.GetCurrentDatabase()}")
                return 1
        if name not in (pm.GetProjectListInCurrentFolder() or []):
            print(f"Project {name!r} not found there. Use `list` to see exact names.")
            return 1
        attrs = (getattr(pm, "GetProjectAttributesInCurrentFolder", lambda: {})() or {}).get(name) or {}
        collab = truthy(attrs.get("liveCollaborationMode"))
        print(f"Project:            {target}")
        print(f"Database:           {pm.GetCurrentDatabase()}")
        print(f"Raw attributes:     {attrs}")
        print(f"Collaboration mode: {collab}")
        print(f"Open in this Resolve: {open_name!r}")

        b = Backup(cfg, tmpdir / "state.json")
        out = tmpdir / "probe.drp"
        t0 = time.monotonic()
        ok = b._direct(pm, name, out)
        size = out.stat().st_size if ok else 0
        print(f"\n1. Direct ExportProject: {'OK' if ok else 'FAILED'}"
              + (f" ({size:,} bytes, {time.monotonic() - t0:.1f}s)" if ok else ""))
        loaded_ok = None
        if not ok and try_load:
            if open_name:
                print(f"2. Load-and-export: skipped, {open_name!r} is open here. Close it and re-run.")
            else:
                print("2. Load-and-export (a 'project in use' dialog may appear in Resolve; dismiss it)...")
                out.unlink(missing_ok=True)
                loaded_ok = b._via_load(pm, name, out)
                print(f"   Load-and-export: {'OK' if loaded_ok else 'FAILED'}")
        print("\nVerdict:")
        if ok:
            print("  Direct export works for this project, so the service can back it up without opening it")
            print("  (including while someone else has it open). No config change needed.")
        elif loaded_ok:
            print("  Only load-and-export works (a read-only load is fine). The default \"load_to_export\": "
                  "\"auto\" already uses it as the fallback; don't set it to \"never\" or \"collab\" for this project.")
        elif not try_load:
            print("  Direct export failed. Re-run with --try-load to test loading it (needs no project open here).")
        else:
            print("  Neither route works right now. If the project is open elsewhere outside collaboration mode,")
            print("  consider enabling collaboration mode on it, or keep Resolve's own Live Save / Project")
            print("  Backups running on the machine that has it open.")
        return 0 if (ok or loaded_ok) else 1
    finally:
        goto_path(pm, start)
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_status(cfg: dict) -> int:
    sf = app_dir() / "state.json"
    if not sf.exists():
        print("No state yet - the service hasn't completed a cycle.")
        return 0
    st = json.loads(sf.read_text(encoding="utf-8"))
    print("Last cycle:", json.dumps(st.get("last_cycle"), indent=1))
    rows = sorted(st["projects"].items(), key=lambda kv: kv[1].get("last_backup") or "", reverse=True)
    for k, v in rows[:25]:
        err = f"  [{v['last_error']}]" if v.get("last_error") else ""
        print(f"  {v.get('last_backup', '-'):20}  fails={v.get('failures', 0)}  {k}{err}")
    print(f"({len(rows)} projects tracked)  backups in {cfg['backup_dir']}")
    return 0


def cmd_init(path: Path | None) -> int:
    p = Path(path) if path else default_config_path()
    if p.exists():
        print(f"Config already exists: {p}")
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote default config: {p}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == [WATCHDOG_FLAG]:
        setup_logging(False)
        return run_watchdog(argv[1])
    if argv[:1] == [resolve_worker.WORKER_FLAG]:
        return resolve_worker.main(argv[1:])
    if argv[:1] == [HEALTH_FLAG]:
        setup_logging(False)
        return run_health_check()
    ap = argparse.ArgumentParser(prog=APP, description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["run", "once", "list", "probe", "status", "init", "dashboard"])
    ap.add_argument("project", nargs="?", help='for `probe`: "Folder/Sub/Project Name"')
    ap.add_argument("--try-load", action="store_true", help="for `probe`: also test load-and-export")
    ap.add_argument("--config", type=Path, help=f"config file (default: {default_config_path()})")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    setup_logging(a.verbose)
    if a.command == "init":
        return cmd_init(a.config)
    cfg = load_config(a.config)
    if a.command == "probe":
        return cmd_probe(cfg, a.project, a.try_load)
    if a.command == "run":
        return cmd_run(cfg, a.config)
    return {"once": cmd_once, "list": cmd_list, "status": cmd_status, "dashboard": cmd_dashboard}[a.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
