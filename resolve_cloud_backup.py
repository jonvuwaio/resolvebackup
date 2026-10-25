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
import copy
import datetime as dt
import fnmatch
import hashlib
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

import project_setup

APP = "resolve-cloud-backup"
VERSION = "1.9.0"
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
    # Changing the port needs a service restart; everything else reloads by itself.
    "dashboard": {"enabled": True, "port": 8765},
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
    # New project set-up (dashboard > New project): folder templates, the workstation drives a job can go on,
    # and extra locations that can also get folders. See project_setup.py for the format.
    "project_setup": {"templates_dir": None, "drives": [], "extras": []},
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
def connect_resolve(cfg: dict):
    """Return the Resolve scripting object, or None if Resolve isn't reachable."""
    api, lib = default_script_paths()
    api = cfg.get("resolve_script_api") or os.environ.get("RESOLVE_SCRIPT_API") or api
    lib = cfg.get("resolve_script_lib") or os.environ.get("RESOLVE_SCRIPT_LIB") or lib
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


def watchdog_command(app_pid: int, resolve_pid: int, cfg_path: Path, conf: Path, saved: Path) -> list[str]:
    """How to start the watchdog: this same program (the packaged app's executable, or this script)."""
    arg = json.dumps({"app": app_pid, "resolve": resolve_pid, "config": str(cfg_path), "conf": str(conf),
                      "saved": str(saved)})
    if getattr(sys, "frozen", False):
        return [sys.executable, WATCHDOG_FLAG, arg]
    return [sys.executable, str(Path(__file__).resolve()), WATCHDOG_FLAG, arg]


def start_watchdog(cmd: list[str]) -> None:
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError as e:
        log.warning("Could not start the watchdog for the hidden DaVinci Resolve: %s", e)


def run_watchdog(arg: str, alive=pid_alive, sleep=time.sleep, hidden_factory=None) -> int:
    """Outlives this app for as long as a hidden Resolve it started runs. If the app stops (crash, force-quit)
    first, quit the hidden Resolve properly, put back the user's library choice and, if they had tried to open
    Resolve meanwhile, open it for them. Otherwise it does nothing: the app tidies up itself."""
    a = json.loads(arg)
    app_pid, resolve_pid = int(a["app"]), int(a["resolve"])
    lib = ActiveLibrary(Path(a["conf"]), Path(a["saved"]))
    while True:
        if not alive(resolve_pid):
            if not alive(app_pid):
                lib.restore()
            return 0
        if not alive(app_pid):
            break
        sleep(1)
    log.warning("Resolve Backup stopped while a hidden DaVinci Resolve was running; quitting it")
    try:
        cfg = load_config(Path(a["config"]))
    except (OSError, ValueError):
        cfg = copy.deepcopy(DEFAULTS)
    h = (hidden_factory or HiddenResolve)(cfg)
    h.adopt(resolve_pid)
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
            r = self.connect_fn(cfg)
            pm = r.GetProjectManager() if r else None
            if pm and pm.GetCurrentDatabase():
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

    def set(self, **kw) -> None:
        with self._lock:
            self._d.update(kw)

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self._d.get(key))

    def update_cycle(self, **kw) -> None:
        with self._lock:
            if self._d["cycle"] is not None:
                self._d["cycle"] = {**self._d["cycle"], **kw}

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
            "with_stills_and_luts", "max_exports_per_cycle", "min_free_gb", "retention", "project_setup")


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
FOLDER_PROMPTS = {"backup": "Choose where Resolve Backup saves your backups",
                  "templates": "Choose the folder that holds your folder templates",
                  "projects": "Choose the folder new jobs go in on this drive"}


def can_choose_folder() -> bool:
    return SYSTEM == "Darwin" and shutil.which("osascript") is not None


def choose_folder(start: str | None = None, run=subprocess.run,
                  prompt: str = "Choose where Resolve Backup saves your backups") -> dict:
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
            free_gb = shutil.disk_usage(self.root).free / 1e9
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
               "open_name": open_name, "can_load": open_name is None, "exports": 0, "touched": set(),
               "interrupted": interrupted or (lambda: False)}
        try:
            if open_name and self.cfg["save_open_project"]:
                pm.SaveProject()
            pm.GotoRootFolder()
            self._walk(pm, [], 0, ctx)
        except CycleInterrupted:
            stats["status"] = "interrupted"
            log.info("Stopped this check part way through; it carries on next time")
        finally:
            try:
                if switched:
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
        out, reason = self._export(pm, path, name, ctx, collab)
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
        if self.cfg["pause_between_exports_seconds"]:
            self.sleep(self.cfg["pause_between_exports_seconds"])

    # ---- export
    def project_dir(self, folder: str, path: list[str], name: str) -> Path:
        return self.root.joinpath(safe_name(folder), *[safe_name(p) for p in path], safe_name(name))

    def _direct(self, pm, name: str, tmp: Path) -> bool:
        try:
            pm.ExportProject(name, str(tmp), self.cfg["with_stills_and_luts"])
        except Exception as e:  # noqa: BLE001
            log.warning("ExportProject(%r) raised: %s", name, e)
        return tmp.exists() and tmp.stat().st_size > 0

    def _via_load(self, pm, name: str, tmp: Path) -> bool:
        proj = None
        try:
            proj = pm.LoadProject(name)
            if not proj:
                return False
            return self._direct(pm, name, tmp)
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
        self.paused = False
        self.next_run = wall()  # first cycle straight away
        self.hidden_factory = hidden_factory or (lambda cfg: HiddenResolve(cfg, connect=connect))
        self.processes = processes
        self.active_library = active_library or ActiveLibrary(resolve_prefs_dir() / "activedb.conf",
                                                              self.dir / "activedb.conf.saved")
        self.watchdog = watchdog or (lambda pid: start_watchdog(watchdog_command(
            os.getpid(), pid, self.cfg_path, self.active_library.path, self.active_library.saved)))
        self.backup = Backup(self.cfg, self.dir / "state.json", connect=connect, clock=clock,
                             status=self.status, sleep=lambda s: self._stop.wait(s))
        # reads config.json itself, so a drive added from the dashboard works at once, even mid-backup
        self.setup = project_setup.ProjectSetup(lambda: load_config(self.cfg_path), self.dir,
                                                known_names=lambda: self.backup.load_state()["projects"].keys())
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
            return None  # the user has Resolve open
        h = self.hidden_factory(self.cfg)
        h.on_exit = self.active_library.restore
        if procs:
            h.adopt(hidden[0])
            self.watchdog(h.pid)
            return h
        lib = self._next_library() if self.cfg.get("rotate_libraries") else None
        if lib and self.active_library.point_at(library_uuid(lib)):
            h.library = lib
            log.info("Hidden check of library %s", self.backup.known_library_names(self.backup.load_state())
                     .get(lib, lib))
        if h.launch():
            self.watchdog(h.pid)
            return h
        self.active_library.restore()
        return None

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
        hidden = None
        try:
            hidden = self._hidden_session()
            if hidden:
                status = self.backup.cycle(
                    connect=lambda cfg: hidden.connect(cfg, lambda: self.stopping),
                    interrupted=lambda: self.stopping or hidden.wanted(), hidden=True,
                    expect_db=hidden.library)["status"]
            else:
                status = self.backup.cycle(interrupted=lambda: self.stopping)["status"]
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
            lock.release()
        retry = status in ("resolve_unavailable", "error", "no_project_manager", "backup_dir_unavailable")
        wait = self.cfg["retry_minutes"] * 60 if retry else self._wait_after_cycle()
        self.next_run = self.wall() + wait * random.uniform(0.95, 1.05)

    def tick(self) -> float:
        self.reload_config()
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


class DashboardServer:
    """Local-only web UI + JSON API. Every request needs the token and a localhost Host header."""

    def __init__(self, service: Service, port: int = 8765, html_path: Path | None = None,
                 token: str | None = None, state_dir: Path | None = None):
        self.svc = service
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
                self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", p), handler)
                break
            except OSError as e:
                err = e
        else:
            raise err  # type: ignore[misc]
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True).start()
        _write_private(self.dir / "service.json", json.dumps({
            "pid": os.getpid(), "port": self.port, "token": self.token, "version": VERSION,
            "started_at": self.started.isoformat(timespec="seconds"), "dashboard": self.url()}))

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        try:
            (self.dir / "service.json").unlink()
        except OSError:
            pass

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
            free = shutil.disk_usage(root).free
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
            if host not in (f"127.0.0.1:{dash.port}", f"localhost:{dash.port}"):
                self._send(403, {"error": "Wrong host"})
                return None
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            tok = self.headers.get("X-RCB-Token") or ("" if header_only else (qs.get("t") or [""])[0])
            if not secrets.compare_digest(tok.encode("utf-8"), dash.token.encode("utf-8")):
                self._send(401, {"error": "Missing or wrong access token. Open the dashboard from the menu bar "
                                          "icon, or run: resolve_cloud_backup.py dashboard"})
                return None
            return parsed.path, qs

        def do_GET(self):
            try:
                auth = self._authorise(header_only=False)
                if not auth:
                    return
                path, qs = auth
                if path == "/":
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
                    self._send(200, {**dash.svc.setup.payload(), "can_choose_folder": can_choose_folder()})
                elif path == "/favicon.ico":
                    self._send(204, b"", "image/x-icon")
                else:
                    self._send(404, {"error": "Not found"})
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
                if origin and origin not in (f"http://127.0.0.1:{dash.port}", f"http://localhost:{dash.port}"):
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
                if path == "/api/run-now":
                    self._send(200, {"ok": svc.request_run_now(), "running": svc.status.get("state") == "running"})
                elif path == "/api/choose-folder":
                    start = body.get("start")
                    prompt = FOLDER_PROMPTS.get(body.get("purpose"), FOLDER_PROMPTS["backup"])
                    self._send(200, choose_folder(start if isinstance(start, str) else None, prompt=prompt))
                elif path == "/api/project-setup/preview":
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    extras = body.get("extras")
                    extras = {i for i in extras if isinstance(i, int)} if isinstance(extras, list) else None
                    self._send(200, svc.setup.plan(fields, body.get("drive"), extras))
                elif path == "/api/project-setup/create":
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    r = svc.setup.create(fields, body.get("drive"), body.get("extras"))
                    for res in r.get("results", []):
                        if res["ok"]:
                            log.info("New project %s: made %d folders in %s", r["job"], res["folders"], res["path"])
                        else:
                            log.warning("New project %s: nothing made in %s: %s", r["job"], res["path"], res["error"])
                    self._send(200, r)
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
                self.dash = DashboardServer(self.svc, port=cfg["dashboard"]["port"])
                self.dash.start()
                log.info("Dashboard running at http://127.0.0.1:%d/", self.dash.port)
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
