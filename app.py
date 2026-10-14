#!/usr/bin/env python3
"""
Resolve Backup: the standalone Mac app.

One process: the backup service, the live dashboard and the menu bar icon. Double-click the app and a dot
appears in the menu bar; there is nothing else to install or keep running. (Packaged by build_mac_app.sh.)
"""
from __future__ import annotations

import json
import sys

import resolve_cloud_backup as rcb
import menubar


def main() -> int:
    rcb.setup_logging(False)
    cfg_path = rcb.default_config_path()
    first_run = not cfg_path.exists()
    if first_run:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(rcb.DEFAULTS, indent=2) + "\n", encoding="utf-8")
    runner = rcb.ServiceRunner(cfg_path)
    if runner.start():
        return menubar.run_app(runner, first_run=first_run)
    # Another copy (for example the older background-service install) already runs the backups:
    # just show its status instead of starting a second service.
    menubar.notify("Resolve Backup", "Backups are already running in the background. This icon shows their status.")
    return menubar.run_app(None)


if __name__ == "__main__":
    sys.exit(main())
