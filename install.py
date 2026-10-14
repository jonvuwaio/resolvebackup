#!/usr/bin/env python3
"""
Install / uninstall resolve-cloud-backup as a per-user background service.

    python3 install.py install [--backup-dir DIR] [--interval MIN] [--no-menubar]
    python3 install.py uninstall
    python3 install.py status

macOS   -> launchd LaunchAgent  (~/Library/LaunchAgents) + a menu bar icon (second LaunchAgent)
Windows -> Task Scheduler task, runs at logon, hidden, auto-restarts
Linux   -> systemd --user service (falls back to an XDG autostart entry)

It is installed per *user* (not system-wide) on purpose: Resolve is a GUI app that
runs inside your login session, and its Blackmagic Cloud sign-in belongs to that user.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import resolve_cloud_backup as rcb  # noqa: E402

SYSTEM = platform.system()
NAME = "resolve-cloud-backup"
LABEL = "com.resolvecloudbackup.agent"
MENU_LABEL = "com.resolvecloudbackup.menubar"
FILES = ("resolve_cloud_backup.py", "dashboard.html", "menubar.py")
TASK = "ResolveCloudBackup"


def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


def python_exe() -> str:
    exe = sys.executable
    if SYSTEM == "Windows":
        pw = Path(exe).with_name("pythonw.exe")
        if pw.exists():
            return str(pw)  # no console window
    return exe


def deploy(args) -> tuple[Path, Path]:
    """Copy the program files to a stable location and write config if absent."""
    d = rcb.app_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(HERE / name, d / name)
    script = d / "resolve_cloud_backup.py"
    cfg_path = d / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else dict(rcb.DEFAULTS)
    if args.backup_dir:
        cfg["backup_dir"] = args.backup_dir
    if args.interval:
        cfg["interval_minutes"] = args.interval
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return script, cfg_path


def wait_for_dashboard(since: float, timeout: float = 10.0) -> str | None:
    """The dashboard address, once the freshly (re)started service has published it."""
    end = time.time() + timeout
    while time.time() < end:
        info = rcb.read_service_info()
        try:
            fresh = info and info.get("dashboard") and \
                __import__("datetime").datetime.fromisoformat(info["started_at"]).timestamp() >= since - 2
        except (KeyError, ValueError, TypeError):
            fresh = False
        if fresh:
            return info["dashboard"]
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------- macOS
def mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def mac_install(script, cfg):
    logs = rcb.app_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python_exe(), str(script), "run", "--config", str(cfg)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "Nice": 10,
        "LowPriorityBackgroundIO": True,
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
    }
    p = mac_plist()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        plistlib.dump(plist, fh)
    dom = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{dom}/{LABEL}"], check=False)
    run(["launchctl", "bootstrap", dom, str(p)])
    run(["launchctl", "kickstart", "-k", f"{dom}/{LABEL}"], check=False)


def mac_uninstall():
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    mac_plist().unlink(missing_ok=True)
    mac_menu_uninstall()


def mac_status():
    dom = f"gui/{os.getuid()}"
    svc = "running/loaded" if run(["launchctl", "print", f"{dom}/{LABEL}"], check=False).returncode == 0 else "not installed"
    bar = "running/loaded" if run(["launchctl", "print", f"{dom}/{MENU_LABEL}"], check=False).returncode == 0 else "not installed"
    return f"service: {svc}\nmenu bar icon: {bar}"


def menubar_plist_dict(python: str, script: Path, logs: Path) -> dict:
    return {
        "Label": MENU_LABEL,
        "ProgramArguments": [python, str(script)],
        "RunAtLoad": True,
        # restart if it crashes, but not when "Quit menu bar icon" exits cleanly
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "menubar.out.log"),
        "StandardErrorPath": str(logs / "menubar.err.log"),
    }


def mac_menu_python() -> str | None:
    """Make a private Python environment with `rumps` (the menu bar library). None if that fails."""
    venv = rcb.app_dir() / "venv"
    py = venv / "bin" / "python3"
    try:
        if not py.exists():
            run([sys.executable, "-m", "venv", str(venv)])
        r = run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "rumps"], check=False)
        if r.returncode != 0:
            print(f"Could not install 'rumps' (the menu bar library):\n{r.stdout}{r.stderr}")
            return None
        return str(py)
    except (SystemExit, OSError) as e:
        print(f"Could not set up the menu bar environment: {e}")
        return None


def mac_menu_install():
    py = mac_menu_python()
    if not py:
        print("The background service is installed, but the menu bar icon was skipped. "
              "Retry later with: python3 install.py install  (needs internet to fetch 'rumps'). "
              "You can still open the dashboard with: python3 resolve_cloud_backup.py dashboard")
        return False
    logs = rcb.app_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = mac_menu_plist()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        plistlib.dump(menubar_plist_dict(py, rcb.app_dir() / "menubar.py", logs), fh)
    dom = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{dom}/{MENU_LABEL}"], check=False)
    run(["launchctl", "bootstrap", dom, str(p)], check=False)
    run(["launchctl", "kickstart", "-k", f"{dom}/{MENU_LABEL}"], check=False)
    return True


def mac_menu_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MENU_LABEL}.plist"


def mac_menu_uninstall():
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{MENU_LABEL}"], check=False)
    mac_menu_plist().unlink(missing_ok=True)


# -------------------------------------------------------------------- Windows
def win_install(script, cfg):
    user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Incremental DaVinci Resolve cloud project backups</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(python_exe())}</Command>
    <Arguments>"{escape(str(script))}" run --config "{escape(str(cfg))}"</Arguments>
  </Exec></Actions>
</Task>"""
    xml_path = rcb.app_dir() / "task.xml"
    xml_path.write_text(xml, encoding="utf-16")
    run(["schtasks", "/Create", "/TN", TASK, "/XML", str(xml_path), "/F"])
    run(["schtasks", "/End", "/TN", TASK], check=False)  # stop an old instance so new config applies
    run(["schtasks", "/Run", "/TN", TASK], check=False)


def win_uninstall():
    run(["schtasks", "/End", "/TN", TASK], check=False)
    run(["schtasks", "/Delete", "/TN", TASK, "/F"], check=False)


def win_status():
    r = run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"], check=False)
    return r.stdout.strip() if r.returncode == 0 else "not installed"


# ---------------------------------------------------------------------- Linux
def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{NAME}.service"


def autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / f"{NAME}.desktop"


def linux_install(script, cfg):
    cmd = f'"{python_exe()}" "{script}" run --config "{cfg}"'
    if shutil.which("systemctl") and run(["systemctl", "--user", "show-environment"], check=False).returncode == 0:
        unit = f"""[Unit]
Description=Incremental DaVinci Resolve cloud project backups

[Service]
ExecStart={cmd}
Restart=always
RestartSec=30
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=default.target
"""
        p = unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(unit)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", f"{NAME}.service"])
        run(["systemctl", "--user", "restart", f"{NAME}.service"])  # pick up a changed config
        return
    p = autostart_path()  # no systemd user session: start at desktop login instead
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"[Desktop Entry]\nType=Application\nName={NAME}\nExec={cmd}\nX-GNOME-Autostart-enabled=true\n")
    subprocess.Popen(cmd, shell=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("systemd user session not available; installed an autostart entry and started it now.")


def linux_uninstall():
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", f"{NAME}.service"], check=False)
        unit_path().unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], check=False)
    autostart_path().unlink(missing_ok=True)


def linux_status():
    if unit_path().exists():
        return run(["systemctl", "--user", "is-active", f"{NAME}.service"], check=False).stdout.strip()
    return "autostart entry installed" if autostart_path().exists() else "not installed"


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["install", "uninstall", "status"])
    ap.add_argument("--backup-dir", help="where .drp backups are stored")
    ap.add_argument("--interval", type=int, help="minutes between backup cycles")
    ap.add_argument("--no-menubar", action="store_true", help="macOS: skip the menu bar icon")
    args = ap.parse_args()

    if sys.version_info < (3, 8):
        sys.exit("Python 3.8+ (64-bit) required")
    table = {
        "Darwin": (mac_install, mac_uninstall, mac_status),
        "Windows": (win_install, win_uninstall, win_status),
        "Linux": (linux_install, linux_uninstall, linux_status),
    }
    if SYSTEM not in table:
        sys.exit(f"Unsupported OS: {SYSTEM}")
    inst, uninst, stat = table[SYSTEM]

    if args.action == "install":
        began = time.time()
        script, cfg = deploy(args)
        inst(script, cfg)
        bar = SYSTEM == "Darwin" and not args.no_menubar and mac_menu_install()
        print(f"Installed. Config: {cfg}\nLogs:   {rcb.app_dir() / 'logs' / 'backup.log'}")
        url = wait_for_dashboard(began)
        if url:
            print(f"Live dashboard: {url}")
        if bar:
            print("A coloured dot should now be in your menu bar. Click it for status and the dashboard.")
        print("Next: with Resolve running (Preferences > System > General > External scripting using: Local), "
              "check `python3 resolve_cloud_backup.py list`.")
    elif args.action == "uninstall":
        uninst()
        print(f"Service removed. Config/state/backups left in place ({rcb.app_dir()}).")
    else:
        print(stat())


if __name__ == "__main__":
    main()
