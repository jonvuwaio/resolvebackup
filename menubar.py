#!/usr/bin/env python3
"""
Menu bar icon for resolve-cloud-backup (macOS).

A small coloured dot in the menu bar shows what the background service is doing.
Click it for the details, or to open the live dashboard, back up now or pause.

    python3 menubar.py              start the menu bar icon (needs `pip install rumps`)
    python3 menubar.py --print      print what the icon would show right now, then exit

The icon is only a window onto the service: it reads ``service.json`` to find the service's
local port and access token, then polls the same local API the dashboard uses. Quitting the
icon never stops backups.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_cloud_backup as rcb  # noqa: E402

AGENT_LABEL = "com.resolvecloudbackup.agent"
LOGIN_LABEL = "com.resolvecloudbackup.app"
POLL_SECONDS = 1.5

# name -> (r, g, b). Matches the dashboard's state colours.
COLOURS = {
    "ok": (52, 178, 126),
    "run": (86, 148, 255),
    "wait": (224, 160, 60),
    "bad": (235, 98, 90),
    "idle": (140, 140, 146),
}

STATUS_TEXT = {
    "resolve_unavailable": "DaVinci Resolve wasn't reachable",
    "low_disk": "Backup disk almost full",
    "backup_dir_unavailable": "Backup folder unavailable",
    "database_not_current": "A different database is open in Resolve",
    "database_not_found": "Database not found in Resolve",
    "project_open_cannot_switch": "A project is open, so the database couldn't be switched",
    "switch_database_failed": "Couldn't switch database",
    "switch_failed": "Couldn't switch database",
    "no_project_manager": "Resolve's Project Manager wasn't available",
    "error": "The check hit an error",
    "interrupted": "Stopped part way so you could use Resolve",
    "library_not_opened": "A library didn't open in the hidden Resolve",
}


# ---------------------------------------------------------------- pure logic
def _parse(ts):
    try:
        return dt.datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def _hm(ts) -> str:
    d = _parse(ts)
    return d.strftime("%H:%M") if d else ""


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summarize(status: dict | None, offline: bool = False, denied: bool = False) -> dict:
    """Turn the service's status payload into what the menu shows.

    Returns {key, colour, title, detail, running, paused, can_run, can_pause, bad_cycle}.
    ``key`` is one of: off, denied, start, run, paused, wait, rest, ok, bad.
    """
    base = {"running": False, "paused": False, "can_run": False, "can_pause": False, "bad_cycle": None,
            "detail": ""}
    if denied:
        return {**base, "key": "denied", "colour": "bad", "title": "Can't talk to the backup service",
                "detail": "Restart the service to refresh its access link"}
    if offline or not status:
        return {**base, "key": "off", "colour": "idle", "title": "Backup service isn't running",
                "detail": "Choose Start backup service"}

    state = status.get("state")
    last = status.get("last_cycle") or {}
    cyc = status.get("cycle") or {}
    paused = bool(status.get("paused"))
    out = {**base, "paused": paused, "can_pause": True, "can_run": state != "running"}
    cfg = status.get("config") or {}
    nxt = _hm(status.get("next_run_at"))

    failed = int(last.get("failed") or last.get("failing") or 0)
    last_status = last.get("status")
    last_bad = bool(last_status) and last_status not in ("ok", "resolve_unavailable", "interrupted")

    if state == "running":
        cur = cyc.get("current")
        seen, expected = cyc.get("seen"), cyc.get("expected")
        if cur:
            detail = f"Exporting {cur}"
        elif cyc.get("phase") == "starting_resolve":
            detail = "Starting DaVinci Resolve hidden"
        elif cyc.get("phase") == "connecting":
            detail = "Connecting to Resolve"
        else:
            detail = "Reading the project library"
        if expected and seen is not None:
            detail += f" ({min(seen, expected)} of {expected})"
        return {**out, "key": "run", "colour": "run", "title": "Backing up now", "detail": detail,
                "running": True, "can_run": False}
    if state == "paused" or paused:
        return {**out, "key": "paused", "colour": "idle", "title": "Paused",
                "detail": "Nothing is backed up until you resume"}
    if state == "waiting_for_resolve":
        d = "Open DaVinci Resolve Studio to continue"
        if nxt:
            d += f". Trying again at {nxt}"
        return {**out, "key": "wait", "colour": "wait", "title": "Waiting for Resolve", "detail": d}
    if state == "outside_active_hours":
        ah = cfg.get("active_hours") or []
        if nxt:
            return {**out, "key": "rest", "colour": "idle", "title": "Out of hours",
                    "detail": f"Checking less often. Next at {nxt}"}
        return {**out, "key": "rest", "colour": "idle",
                "title": f"Resting until {ah[0]}" if ah else "Resting",
                "detail": "Back up now still works"}
    if state == "idle":
        if failed or last_bad:
            if failed:
                title = f"{plural(failed, 'project')} couldn't be backed up"
            else:
                title = STATUS_TEXT.get(last_status, "The last check had a problem")
            return {**out, "key": "bad", "colour": "bad", "title": title,
                    "detail": _last_line(last, nxt), "bad_cycle": last.get("at") if (last.get("failed") or last_bad) else None}
        return {**out, "key": "ok", "colour": "ok", "title": "Up to date", "detail": _last_line(last, nxt)}
    return {**out, "key": "start", "colour": "idle", "title": "Starting", "detail": ""}


def _last_line(last: dict, nxt: str) -> str:
    parts = []
    if last.get("at"):
        if last.get("status") == "ok":
            parts.append(f"Last check {_hm(last['at'])}: {last.get('exported', 0)} backed up")
        else:
            parts.append(f"Last check {_hm(last['at'])}")
    if nxt:
        parts.append(f"next at {nxt}")
    return ", ".join(parts)


class Alerts:
    """Decides when to raise a notification: once per cycle that finished with a problem.

    The first status seen after the icon starts is only recorded, so launching the icon
    never replays an old failure.
    """

    def __init__(self):
        self._seen = None
        self._primed = False

    def check(self, summary: dict) -> str | None:
        at = summary.get("bad_cycle")
        msg = None
        if self._primed and at and at != self._seen:
            msg = summary["title"]
        if summary["key"] in ("ok", "bad"):
            self._seen = at or self._seen
            self._primed = True
        return msg


def icon_png(rgb: tuple, size: int) -> bytes:
    """A filled circle with a soft edge, as a PNG with transparency (stdlib only)."""
    r, g, b = rgb
    c = (size - 1) / 2
    radius = size * 0.30
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            a = max(0.0, min(1.0, radius + 0.5 - d))
            row += bytes((r, g, b, int(a * 255)))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_icons(directory: Path) -> dict:
    """Write 1x and 2x icons for every colour. Returns {colour: path of the 1x file}."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, rgb in COLOURS.items():
        p = directory / f"dot-{name}.png"
        p.write_bytes(icon_png(rgb, 22))
        (directory / f"dot-{name}@2x.png").write_bytes(icon_png(rgb, 44))
        paths[name] = p
    return paths


# ------------------------------------------------------------- talking to the service
class ServiceClient:
    def __init__(self, directory: Path | None = None):
        self.directory = directory

    def info(self) -> dict | None:
        i = rcb.read_service_info(self.directory)
        if not i or not i.get("port") or not i.get("token"):
            return None
        return i

    def _request(self, path: str, body: dict | None = None, timeout: float = 3.0):
        info = self.info()
        if not info:
            raise ConnectionError("service.json missing")
        url = f"http://127.0.0.1:{info['port']}{path}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers={"X-RCB-Token": info["token"]})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def poll(self) -> tuple[dict | None, bool, bool]:
        """Returns (status, offline, denied)."""
        try:
            return self._request("/api/status"), False, False
        except urllib.error.HTTPError as e:
            return None, False, e.code in (401, 403)
        except (OSError, ValueError, ConnectionError):
            return None, True, False

    def post(self, path: str, body: dict | None = None):
        return self._request(path, body or {})

    def dashboard_url(self) -> str | None:
        i = self.info()
        return f"http://127.0.0.1:{i['port']}/?t={i['token']}" if i else None


def notify(title: str, message: str) -> None:
    """macOS notification via osascript (works without an app bundle)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def start_service() -> None:
    """Ask launchd to (re)start the background service."""
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                   capture_output=True, timeout=10)


def open_path(p) -> None:
    subprocess.run(["open", str(p)], capture_output=True, timeout=10)


# ------------------------------------------------------------------ launch at login
def login_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist"


def login_command(executable: str | None = None, script: str | None = None) -> list:
    """What launchd should run at login: the packaged .app if we are one, otherwise this script."""
    exe = executable or sys.executable
    if getattr(sys, "frozen", False):
        parts = Path(exe).resolve().parts
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].endswith(".app"):
                return ["/usr/bin/open", "-a", str(Path(*parts[:i + 1]))]
        return [exe]
    return [exe, script or str(Path(__file__).resolve().parent / "app.py")]


def login_enabled() -> bool:
    return login_plist_path().exists()


def set_login(enabled: bool, command: list | None = None) -> None:
    """Start the app when you log in (a per-user LaunchAgent; takes effect at next login)."""
    import plistlib
    p = login_plist_path()
    if not enabled:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        plistlib.dump({"Label": LOGIN_LABEL, "ProgramArguments": command or login_command(),
                       "RunAtLoad": True, "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}, fh)


# ------------------------------------------------------------------------- the app
def run_app(runner=None, first_run: bool = False) -> int:
    """Show the menu bar icon.

    With ``runner`` (a started ``rcb.ServiceRunner``) the app *is* the service: the menu has a login-item
    toggle and Quit stops backups. Without one it is just a window onto a service running elsewhere.
    """
    embedded = runner is not None
    try:
        import rumps  # type: ignore
    except ImportError:
        print("The menu bar icon needs the 'rumps' package: python3 -m pip install rumps", file=sys.stderr)
        return 2
    if sys.platform != "darwin":
        print("The menu bar icon is for macOS only. On other systems use: resolve_cloud_backup.py dashboard",
              file=sys.stderr)
        return 2

    lock = rcb.SingleInstance(rcb.app_dir() / "menubar.lock")
    if not lock.acquire():
        print("The menu bar icon is already running.")
        if runner:
            runner.stop()
        return 0
    try:  # keep out of the Dock
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:  # noqa: BLE001
        pass

    client = ServiceClient()
    icons = write_icons(rcb.app_dir() / "menubar-icons")
    alerts = Alerts()

    class App(rumps.App):
        def __init__(self):
            super().__init__("Resolve Backup", icon=str(icons["idle"]), template=False, quit_button=None)
            self.latest = (None, True, False)
            self.shown = None
            self.m_title = rumps.MenuItem("Starting")
            self.m_detail = rumps.MenuItem("")
            self.m_dash = rumps.MenuItem("Open dashboard", callback=self.open_dashboard)
            self.m_new = rumps.MenuItem("New project…", callback=self.new_project)
            self.m_run = rumps.MenuItem("Back up now", callback=self.run_now)
            self.m_pause = rumps.MenuItem("Pause backups", callback=self.toggle_pause)
            self.m_start = rumps.MenuItem("Start backup service", callback=self.start)
            self.m_login = rumps.MenuItem("Open at login", callback=self.toggle_login)
            self.m_login.state = login_enabled()
            self.m_folder = rumps.MenuItem("Open backup folder", callback=self.open_folder)
            self.m_log = rumps.MenuItem("Open log", callback=self.open_log)
            self.m_quit = rumps.MenuItem("Quit Resolve Backup" if embedded else "Quit menu bar icon",
                                         callback=self.quit)
            top = [self.m_title, self.m_detail, None, self.m_new, self.m_dash, self.m_run, self.m_pause]
            if not embedded:
                top.append(self.m_start)
            self.menu = top + [None, self.m_folder, self.m_log] + ([self.m_login] if embedded else []) + [None, self.m_quit]
            if first_run:  # say hello once: the dashboard explains what Resolve needs
                self.hello = rumps.Timer(self.say_hello, 2)
                self.hello.start()
            self.summary = summarize(None, offline=True)
            threading.Thread(target=self._poll_loop, daemon=True).start()
            self.timer = rumps.Timer(self.refresh, 1)
            self.timer.start()

        def _poll_loop(self):
            while True:
                try:
                    self.latest = client.poll()
                except Exception:  # noqa: BLE001
                    self.latest = (None, True, False)
                time.sleep(POLL_SECONDS)

        def refresh(self, _=None):
            status, offline, denied = self.latest
            s = summarize(status, offline, denied)
            self.summary = s
            if self.shown != s:
                self.shown = s
                self.icon = str(icons[s["colour"]])
                self.m_title.title = s["title"]
                self.m_detail.title = s["detail"] or " "
                self.m_run.set_callback(self.run_now if s["can_run"] else None)
                self.m_pause.set_callback(self.toggle_pause if s["can_pause"] else None)
                self.m_pause.title = "Resume backups" if s["paused"] else "Pause backups"
                self.m_start.set_callback(self.start if s["key"] in ("off", "denied") else None)
                self.m_dash.set_callback(self.open_dashboard if s["key"] not in ("off", "denied") else None)
                self.m_new.set_callback(self.new_project if s["key"] not in ("off", "denied") else None)
            msg = alerts.check(s)
            if msg:
                notify("Resolve Backup", msg)

        # ---- actions
        def say_hello(self, timer):
            timer.stop()
            self.open_dashboard(None)

        def toggle_login(self, item):
            item.state = not item.state
            try:
                set_login(bool(item.state))
            except OSError as e:
                item.state = not item.state
                notify("Resolve Backup", f"Couldn't change the login setting: {e}")

        def quit(self, _):
            if runner:
                runner.stop()
            rumps.quit_application()

        def open_dashboard(self, _):
            url = client.dashboard_url()
            if url:
                webbrowser.open(url)

        def new_project(self, _):
            url = client.dashboard_url()
            if url:
                webbrowser.open(url + "#new")

        def run_now(self, _):
            try:
                client.post("/api/run-now")
            except Exception:  # noqa: BLE001
                pass

        def toggle_pause(self, _):
            try:
                client.post("/api/pause", {"paused": not self.summary["paused"]})
            except Exception:  # noqa: BLE001
                pass

        def start(self, _):
            start_service()

        def open_folder(self, _):
            status = self.latest[0] or {}
            d = (status.get("config") or {}).get("backup_dir")
            if not d:
                try:
                    d = rcb.load_config(None)["backup_dir"]
                except Exception:  # noqa: BLE001
                    d = None
            if d:
                Path(d).expanduser().mkdir(parents=True, exist_ok=True)
                open_path(Path(d).expanduser())

        def open_log(self, _):
            log = rcb.app_dir() / "logs" / "backup.log"
            open_path(log if log.exists() else log.parent)

    try:
        App().run()
    finally:
        lock.release()
        if runner:
            runner.stop()
    return 0


def print_now() -> int:
    status, offline, denied = ServiceClient().poll()
    s = summarize(status, offline, denied)
    print(f"{s['title']}\n{s['detail']}\n(icon: {s['colour']})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print the current state and exit")
    a = ap.parse_args(argv)
    return print_now() if a.print else run_app()


if __name__ == "__main__":
    sys.exit(main())
