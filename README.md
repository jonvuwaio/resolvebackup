# resolve-cloud-backup

A small background service for **macOS, Windows and Linux** that incrementally backs up recently
modified DaVinci Resolve projects (including Blackmagic Cloud projects) as `.drp` files throughout the day.

The service and dashboard are pure Python standard library. The optional macOS menu bar icon uses `rumps`, which the installer sets up in its own private environment.

## How it works

Cloud project libraries can only be read through Resolve itself, so the service talks to a **running
DaVinci Resolve** over its scripting API:

1. Walk every folder of the chosen database.
2. Read each project's `lastModifiedDate` (`GetProjectAttributesInCurrentFolder`).
3. Export only projects whose date changed since their last backup (`ExportProject` → `.drp`).
4. Prune old copies: keep everything from the last 24 h, then one per day for 30 days (the newest backup of
   every project is always kept, even if the project is later deleted from the cloud).

Backups land in `<backup_dir>/<database>/<folder>/<project>/<project>__YYYYMMDD-HHMMSS.drp`.
Files are written to a temporary name and renamed only when complete, so a crash never leaves a corrupt backup.

## See it working live

- **Dashboard:** the service runs a small web page on your own machine only (`127.0.0.1`, never the network).
  It shows a timeline of every check with a live playhead, what is being exported right now and how far through
  the library it is, an activity feed, every project's backup status, and a **Settings** drawer (backup folder,
  interval, active hours, include/exclude, retention) that saves straight to `config.json`.
  Open it with `python3 resolve_cloud_backup.py dashboard` (prints the address) or from the menu bar icon.
  The address contains a private access token, so other websites and other users on the machine can't use it.
- **macOS menu bar icon:** a coloured dot: green up to date, blue backing up, amber waiting for Resolve,
  red something failed, grey paused or the service isn't running. Click it for the status line, *Open dashboard*,
  *Back up now*, *Pause backups*, *Open backup folder* and *Open log*. You get a notification when a check
  finishes with a failure. Quitting the icon never stops backups.

## Requirements

- **DaVinci Resolve Studio**, running and signed in to Blackmagic Cloud. The free version only allows scripts
  to run from inside Resolve, not from an external process.
- In Resolve: *Preferences → System → General → External scripting using* = **Local**.
- Python 3.8+ (64-bit) on the same machine.

## Install as a Mac app (recommended on macOS)

`Resolve Backup.app` is one standalone menu bar app: it *is* the backup service, the dashboard and the icon in a
single process. No Python install, no background scripts. Open it and a dot appears in the menu bar; use
**Open at login** in its menu to have it start with your Mac. **Quit Resolve Backup** stops backups.

An app has to be built on a Mac (I can't produce one from Linux), and it's one command:

```bash
xcode-select --install        # once, if you don't have Python 3 yet
./build_mac_app.sh            # -> dist/Resolve Backup.app  and  dist/Resolve-Backup.zip
```

Drag the app to `/Applications` and open it. It's signed ad-hoc rather than notarised, so the first time macOS
may say it can't verify the developer: right-click the app, choose *Open*, and confirm once. If you'd rather not
build it yourself, `.github/workflows/build-mac.yml` builds the same zip on GitHub's Mac runners
(Actions → *Build Mac app* → Run workflow) if you put this folder in a private repository.

If an older background-service install is already running, the app detects it and just shows its status instead
of starting a second service.

## Install as a background script (Windows, Linux, or macOS without the app)

```bash
python3 install.py install --backup-dir ~/ResolveCloudBackups --interval 30
```

On macOS this also installs the menu bar icon (`--no-menubar` to skip it). That step needs internet once, to fetch
`rumps`; if it fails the service still installs and you can use the dashboard instead. Failure notifications are sent through
macOS's own notification command, so if none appear, check *System Settings → Notifications* (they may be listed as
"Script Editor").
The command ends by printing the dashboard address.

| OS      | Installed as                                           |
|---------|--------------------------------------------------------|
| macOS   | launchd LaunchAgents (`~/Library/LaunchAgents`): service + menu bar icon |
| Windows | Task Scheduler task, at logon, hidden, auto-restart    |
| Linux   | `systemd --user` service (XDG autostart if no systemd) |

It runs per *user*, not system-wide, because Resolve and its cloud sign-in live in your login session.
`python3 install.py status` / `uninstall` do what you'd expect (backups are never deleted by uninstall).
On Windows, run it from a normal (non-admin) prompt.

## First-run check (important)

With Resolve open:

```bash
python3 resolve_cloud_backup.py list     # what Resolve reports: databases, projects, modified dates
python3 resolve_cloud_backup.py once -v  # one cycle, verbose
python3 resolve_cloud_backup.py status
```

`list` shows the exact `DbType`/`DbName` of your cloud database and the format of `lastModifiedDate`. If your
cloud database isn't the one currently selected in Resolve, set it in the config (below).

**Several cloud libraries.** Only the library that's open in Resolve can be backed up. Resolve's scripting API
can't list cloud libraries (in Resolve 21 one appears only as the current database, reported as `PostgreSQL` on an
AWS address), and `SetCurrentDatabase` refuses to switch into one. A script can switch *out* of a cloud library
but not back, so this app never switches away from one. Each library you open is backed up into its own folder
(named after its `db_…` database name), and the dashboard's **Libraries** section lists every library it has
checked. The section shows each library's top-level folders and when it was last checked, and flags any library not
opened for over 7 days.

Logs: `app dir/logs/backup.log` — macOS `~/Library/Application Support/resolve-cloud-backup`,
Windows `%APPDATA%\resolve-cloud-backup`, Linux `~/.config/resolve-cloud-backup`.

## Configuration (`config.json` in that folder, or the dashboard's Settings drawer)

Changes apply from the next cycle without restarting. Only the dashboard port needs a restart.

| Key | Default | Meaning |
|---|---|---|
| `backup_dir` | `~/ResolveCloudBackups` | Where backups go. Avoid iCloud/Documents-protected folders on macOS. |
| `interval_minutes` | `30` | Time between cycles. |
| `active_hours` | `null` | e.g. `["07:00","22:00"]`. May wrap midnight. |
| `database` | `{name: null, type: null}` | Database to back up; null = whichever is current in Resolve. |
| `switch_database` | `false` | Allow switching to `database` if it isn't current. Only ever done when no project is open. |
| `initial_lookback_days` | `7` | First run only: skip projects untouched for longer. |
| `include` / `exclude` | `[]` | Globs or folder prefixes on `Folder/Sub/Project`. |
| `load_to_export` | `"auto"` | What to do when a direct export fails: `never`, `auto` (load read-only → export → close, any project), `collab` (same, collaboration-mode projects only), `always`. See "Projects open elsewhere". |
| `retry_backoff_max_minutes` | `120` | Failing projects retry after 5, 10, 20 … min, capped at this. |
| `alert_command` | `null` | Shell command run once when a project hits `alert_after_failures` (default 3) consecutive failures. Gets `RCB_PROJECT`, `RCB_DATABASE`, `RCB_FAILURES`, `RCB_REASON`. |
| `save_open_project` | `false` | Save the project you have open before exporting. |
| `max_exports_per_cycle` | `50` | Remaining projects are picked up next cycle. |
| `retention` | `24 h / 30 days, no cap` | `keep_all_hours`, `keep_daily_days`, and `max_backups`: never keep more than this many backups of each project (the newest). `0` = no limit. Applied on top of the time rules, straight away when changed. |
| `min_free_gb` | `5` | Skip the cycle if the backup disk is nearly full. |
| `with_stills_and_luts` | `true` | Passed to `ExportProject`. |
| `dashboard` | `{enabled: true, port: 8765}` | The local live dashboard. If the port is taken the next free one (up to +19) is used. `alert_command` isn't editable from the browser, on purpose. |

## Projects open elsewhere

Backups must not depend on nobody having the project open, so export never needs the project to be free:

1. **Direct export from the Project Manager.** `ExportProject` works on the project as saved in the database
   without opening it, so a project that is open (or locked) on another machine is backed up like any other.
   This is the normal path and the one nearly every backup takes.
2. **Fallback: load, export, close.** If a direct export fails, the service opens the project in *its own*
   Resolve (read-only is fine when it's open elsewhere), exports it and closes it again. This is the default
   (`load_to_export: "auto"`) and applies to any project.
3. **If both fail,** the project is retried with backoff and flagged in the log, `status` and `alert_command`
   rather than failing silently.

One thing to watch: I don't know whether a *scripted* load of a locked project opens it read-only quietly or raises
Resolve's "project in use" dialog, which could stall the service until someone dismisses it. If you ever see that,
set `"load_to_export": "collab"` (only load collaboration-mode projects) or `"never"`. `probe --try-load` shows
what happens for a given project.

Steps 2 and 3 only load a project when none is open in the service's own Resolve, so the cleanest setup is a
machine that isn't used for editing (Resolve open at the Project Manager, nothing loaded). Step 1 works either way.
The backup is the *saved* state in the database: edits live only in someone's open session aren't included until
they save or Live Save writes them.

Test your own setup: have a colleague open a project, then

```bash
python3 resolve_cloud_backup.py probe "Folder/Project Name"              # direct export only
python3 resolve_cloud_backup.py probe "Folder/Project Name" --try-load   # + load-and-export (needs nothing open here)
```

It prints whether the project is in collaboration mode, which route worked, and what to set. It writes only to a
temp folder and leaves nothing behind.

## Restoring locally

If the cloud is unreachable: in Resolve switch to a local (Disk or PostgreSQL) database, right-click in the Project
Manager → *Import Project*, and pick the newest `.drp` from the project's backup folder. Relink media if paths differ.

## Behaviour you should know about

- **It never disturbs open work.** It only loads a project (the fallback described above) when you have *no*
  project open, and it restores the folder you were looking at in the Project Manager.
- **Resolve must be running.** If it isn't, the service retries every 5 minutes. For an always-on setup, run
  Resolve on a spare workstation (`-nogui` works with scripting) rather than your editing machine.
- **`.drp` files contain the project (timelines, grades, media pool), not the media.**
- **Complement, not replacement,** for Blackmagic Cloud's own project history and your media backups.
- **Self-hosted PostgreSQL project server instead of Blackmagic Cloud?** This still works (it appears as a
  `PostgreSQL` database in `list`). For disaster recovery you could additionally run `pg_dump` on the server.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The 88 tests run against a fake Resolve API (incremental detection, nested folders, open-elsewhere and
collaboration scenarios, backoff and alerts, `probe`, database switching, retention, caps, filters) plus the
scheduler, the dashboard's API and its security (token, host/origin checks), the menu bar logic and icons, and the installer.

## Not verified against real Resolve or macOS

I could not run this against a real Resolve install, and the Mac app has not been built or run on a real Mac (its logic, icons, in-process service and packaging of the service and dashboard are tested on Linux; the `rumps` menu code, the macOS build script and the launch-at-login/launchd steps are not). Resolve's scripting library is loaded from Resolve's own folder at runtime, and I haven't confirmed it loads inside the packaged app; if backups report that Resolve is unreachable although it is running with scripting on, use the script install instead and tell me. The dashboard was checked in a real browser engine against a simulated Resolve. If the icon doesn't appear, look at `logs/menubar.err.log` and use `resolve_cloud_backup.py dashboard` meanwhile. The pieces most worth checking on your first run with `list`/`once`:
the cloud database's `DbType`, the `lastModifiedDate` format (change detection compares the raw value, so an
unfamiliar format still works; it is only used to apply the first-run `initial_lookback_days` filter, and if it
can't be parsed on the first run, up to `max_exports_per_cycle` projects are exported per cycle until all are
covered), and that the scripted `ExportProject` call behaves like the Project Manager's own export on cloud
projects and on projects open elsewhere. It should, but `probe` confirms it for your setup in a minute.
