# Colour Project Manager

Sets up new colour jobs (folders on the workstation RAID and the deliverables drive, the DaVinci Resolve project
and the Frame.io project) and backs up DaVinci Resolve projects. It started as resolve-cloud-backup, which is still
the name of its code, its data folder (`~/Library/Application Support/resolve-cloud-backup`) and its bundle ID.

A small background service for **macOS, Windows and Linux** that incrementally backs up recently
modified DaVinci Resolve projects (including Blackmagic Cloud projects) as `.drp` files throughout the day.

The service and dashboard are pure Python standard library. The optional macOS menu bar icon uses `rumps`, which the installer sets up in its own private environment.

## How it works

Cloud project libraries can only be read through Resolve itself, so the service talks to a **running
DaVinci Resolve** over its scripting API. On a Mac, while you don't have Resolve open, the service starts it
**hidden** (`Resolve -nogui`, no windows) for each check and quits it afterwards (see "Hidden Resolve" below).
A check then does this:

1. Walk every folder of the chosen database.
2. Read each project's `lastModifiedDate` (`GetProjectAttributesInCurrentFolder`).
3. Export only projects whose date changed since their last backup (`ExportProject` → `.drp`).
4. Prune old copies: keep everything from the last 24 h, then one per day for 30 days (the newest backup of
   every project is always kept, even if the project is later deleted from the cloud).

Backups land in `<backup_dir>/<database>/<folder>/<project>/<project>__YYYYMMDD-HHMMSS.drp`.
Files are written to a temporary name and renamed only when complete, so a crash never leaves a corrupt backup.

## See it working live

- **Dashboard:** the service runs a small web page on your own machine only (`127.0.0.1`, never the network).
  It has two pages: **Projects** (the start page: set up a new job, and the jobs made recently) and **Backups**.
  The bar at the top of every page shows whether backups are running. The Backups page shows a timeline of every check with a live playhead, what is being exported right now and how far through
  the library it is, an activity feed, every project's backup status, and a **Settings** drawer (backup folder,
  interval, active hours, include/exclude, retention) that saves straight to `config.json`.
  Open it with `python3 resolve_cloud_backup.py dashboard` (prints the address) or from the menu bar icon.
  The address contains a private access token, so other websites and other users on the machine can't use it.
- **macOS menu bar icon:** a coloured dot: green up to date, blue backing up, amber waiting for Resolve,
  red something failed, grey paused or the service isn't running. Click it for the status line, *New project…*
  (the Projects page), *Backups…* (the Backups page), *Back up now*, *Stop this backup* (ends the check that's
  running, after the current project; the next one runs as usual), *Pause backups*, *Open backup folder* and *Open log*. You get a notification when a check
  finishes with a failure. Quitting the icon never stops backups.

## Requirements

- **DaVinci Resolve Studio**, running and signed in to Blackmagic Cloud. The free version only allows scripts
  to run from inside Resolve, not from an external process.
- In Resolve: *Preferences → System → General → External scripting using* = **Local**.
- Python 3.8+ (64-bit) on the same machine.

## Install as a Mac app (recommended on macOS)

`Colour Project Manager.app` is one standalone menu bar app: it *is* the backup service, the dashboard and the icon in a
single process. No Python install, no background scripts. Open it and a dot appears in the menu bar; use
**Open at login** in its menu to have it start with your Mac. **Quit Colour Project Manager** stops backups.

An app has to be built on a Mac (I can't produce one from Linux), and it's one command:

```bash
xcode-select --install        # once, if you don't have Python 3 yet
./build_mac_app.sh            # -> dist/Colour Project Manager.app  and  dist/Colour-Project-Manager.zip
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

**Several cloud libraries.** Resolve's scripting API can't list cloud libraries (in Resolve 21 one appears only as
the current database, reported as `PostgreSQL` on an AWS address), and `SetCurrentDatabase` refuses to switch into
one. A script can switch *out* of a cloud library but not back, so this app never switches the Resolve you have
open. Instead, on a Mac, each **hidden** check opens the next library in turn (`rotate_libraries`, on by default):
before starting the hidden Resolve it changes the line `cloud*:<library id>` in Resolve's
`~/Library/Preferences/Blackmagic Design/DaVinci Resolve/activedb.conf` (the `*` marks the library Resolve opens at
start-up), and puts the file back as soon as the hidden Resolve quits, before Resolve is reopened for you. A copy of
the original is kept in the app's folder until then, so if the app stops part way it is put back the next time
the app starts. The library checked longest ago goes first. It only ever swaps one cloud library for another: if
you last used a local or network database, the hidden Resolve opens that and nothing is changed. If Resolve opens
something other than the library asked for, the check is skipped and reported, and the next library is tried.
Leave libraries out with `"skip_libraries": ["Name"]`. While you have Resolve open, only the library open in it
is backed up. Each library is backed up into its own folder named after the library.

The API only reports a cloud library by an internal name (`db_<id>`). The real name comes from the line
`BlackmagicID: PL(<id>:'<name>':…)` that Resolve writes to its own log (`ResolveDebug.txt`) for every library your
account can see. Names are remembered, so they survive Resolve's log rotation. A folder created under the internal
name, or under an old name after a library is renamed, is renamed to match on the next check. It is never merged
into an existing folder. Projects are tracked by the internal id, so a rename doesn't trigger fresh exports. The
dashboard's **Libraries** section lists every library your account can see. For each one it shows the top-level
folders, when it was last checked, and whether it's open now, not opened for over 7 days, or never backed up.

Logs: `app dir/logs/backup.log` — macOS `~/Library/Application Support/resolve-cloud-backup`,
Windows `%APPDATA%\resolve-cloud-backup`, Linux `~/.config/resolve-cloud-backup`.

## Configuration (`config.json` in that folder, or the dashboard's Settings drawer)

Changes apply from the next cycle without restarting. Only the dashboard port needs a restart.

| Key | Default | Meaning |
|---|---|---|
| `backup_dir` | `~/ResolveCloudBackups` | Where backups go. Avoid iCloud/Documents-protected folders on macOS. On a Mac, **Choose…** in the dashboard's Settings opens the standard folder window. |
| `interval_minutes` | `30` | Time between cycles. `0` = continuously, a minute apart. |
| `active_hours` | `null` | e.g. `["07:00","22:00"]`. May wrap midnight. |
| `off_hours_interval_minutes` | `0` | Outside `active_hours`, check this often instead. `0` = not at all. The first check of the day still runs when `active_hours` starts. |
| `background_resolve` | `true` | macOS: while Resolve isn't open, start it hidden for each check, then quit it. See "Hidden Resolve". |
| `rotate_libraries` | `true` | With `background_resolve`: each hidden check opens the next of your cloud libraries in turn. See "Several cloud libraries". |
| `skip_libraries` | `[]` | Library names to leave out of the rotation. |
| `resolve_app` | `null` | Path to Resolve's executable for `background_resolve`; null = `/Applications/DaVinci Resolve/…/MacOS/Resolve`. |
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

## Hidden Resolve (macOS)

Driving the Resolve you have open makes its Project Manager flick from folder to folder during every check.
A hidden Resolve avoids that, and is far faster: on a real 829-project cloud library a full check took
about 80 seconds hidden against about 17 minutes through an open Resolve.

- **When you don't have Resolve open,** each check starts `Resolve -nogui`, waits for it to answer (about
  15 seconds), backs up the next cloud library in turn (see "Several cloud libraries"), and quits it. You may see Resolve's Dock icon
  while it runs, but no windows.
- **Resolve runs one copy at a time.** Opening Resolve from the Dock or Finder while the hidden copy runs only
  brings the hidden copy forward, with no window. The service notices, stops the check between projects, quits the
  hidden copy and opens Resolve normally for you, so it takes a few seconds longer to appear than usual.
  The interrupted check carries on next time.
- **When you have Resolve open,** checks go through your Resolve as before.
- **If the app stops during a hidden check** (crash, force-quit), a small watchdog (the app's own program, started
  alongside each hidden Resolve, idle otherwise) quits the hidden Resolve properly within a few seconds and puts back
  your library choice. If you had tried to open Resolve meanwhile, it then opens it for you.
- **If the hidden Resolve crashes,** the check ends and your library choice is put back straight away. Resolve may
  show its own crash-report window.
- **If the power goes,** your library choice is put back when the app next starts (at login). Open Resolve before
  that and it opens on the library that was being checked; switch back once in the Project Manager. The choice is
  only ever changed by writing a new file and swapping it in one step, so it is never left half-written.
- A hidden Resolve left running (for example if the watchdog was stopped too) is used by the next check and then
  quit.
- Turn it off with `"background_resolve": false` (or the Settings checkbox).

## New project set-up

**New project** (the Projects page, or **New project…** in the menu bar) sets up a job in one go. Every job gets
all of these, named after the job: its working folders on the workstation's RAID
(`Projects/<Client>/<Job>`, from the `Project` template), its deliverables folders on the deliverables drive
(`Deliverables/Projects/<Job>`, from the `Deliverables` template; exports only, never working projects) and its
DaVinci Resolve project with the bins, and its Frame.io project with the review folders.

1. Pick the **project drive**: the RAID of the workstation the job is on. **Add a drive…** at the bottom of the
   list offers every connected drive that isn't set up yet, already filled in as `Projects/<Client>/<Job>` with
   the `Project` template (change either for a drive that's laid out differently).
2. Pick the **job name** from the list, which comes from column G of the Job Report's CURRENT tab (newest number
   first; jobs already set up are at the bottom, to finish a missing part). The **client folder** is then picked
   for you: the folder that client's jobs went in most recently. Both are drop-downs, to keep typing (and typos)
   out of it; **Type a name that isn't on the list…** and **New client folder…** are there for the exceptions. A
   typed job name starts with the next free number. Every path is previewed live.

   The job list is read from a link to just the job names, published from Google Sheets, so the Job Report itself
   (budgets, contacts) stays private: make a sheet with `=IMPORTRANGE("<Job Report ID>", "CURRENT!G:G")` in A1,
   publish it to the web as CSV, and paste the link under **Templates and locations > Job list**. The list is read
   again every two minutes, or when you press **Refresh**.
3. Check the list of what the job gets and press **Create project**. It's all or nothing: Create stays off until
   every part can be made (a drive that isn't connected blocks it), and if one part fails, what that attempt made
   elsewhere is taken back. A part that already exists is left alone, so a job missing one part can be finished.

**The Resolve project** (for example `WC105_WSM_Varley_Spring26_CV1` in the library's
`WSMLondon/WC105_WSM_Varley_Spring26` folder) is a copy of your template project (`Project_Template/ProjectTemplate`),
so it starts with the template's bins, timeline and project settings: change the template in Resolve and every new
job follows. It's made by exporting the template and importing that under the new name, which never opens a
project, so it's safe while you're working in Resolve:

- Resolve open on the right library: it's made there, in a couple of seconds, and the Project Manager is put
  back on the folder you were looking at.
- Resolve open on another library: it isn't made; switch library (or close Resolve) and press **Retry**.
- Resolve closed: Resolve is started hidden on the right library for about 20 seconds, as for backups.

The folders are made at once; the Resolve project follows a moment later and the panel shows its progress. A
backup check that's running stops between projects to let it through. Folders it had to make in Resolve are
removed again if the project can't be made.

**The Frame.io project** (for example `WC105_WSM_Varley_Spring26` in the chosen workspace) gets the folders of
the `FrameIO` template (Grade, Online, VFX/TO_VFX, VFX/FROM_VFX). It's made last, once the drives are done; if Frame.io
fails, the folders are taken back and the Resolve project isn't made, so pressing Create again starts clean. A
project half-made in Frame.io is deleted again. Frame.io has to be reachable for Create to be on.

Setting up Frame.io (once; it uses the new Frame.io with an Adobe ID):

1. In the [Adobe Developer Console](https://developer.adobe.com/console), signed in with the same Adobe ID you use
   for Frame.io, create a project, **Add API** > **Frame.io API** > **User Authentication** > **OAuth Web App**.
2. Default redirect URI: `https://localhost:8765/frameio/callback`. Redirect URI pattern: the address shown under
   **New project > Templates and locations > Frame.io** (normally `http://localhost:8765/frameio/callback`).
3. In that Frame.io section, paste the **Client ID**, paste the **Client secret** and press **Save secret** (it goes
   in the Mac keychain, never in the settings file), then **Connect Frame.io** and sign in in the tab that opens.
4. Choose the **workspace**, tick **Also make a Frame.io project** and press **Save locations**.

The app stays signed in (the sign-in is renewed by itself). If Adobe ever ends it, the Frame.io row says so and
**Connect Frame.io** signs in again. **Disconnect** removes the sign-in from the keychain.

Templates are the folders inside the templates folder (for example `/Volumes/WashDeliverables/FolderStructures`).
Each top-level folder there is one template, and its sub-folders are copied (empty) into the new job, so changing a
template there changes every job made after. Placeholders such as `{job}` work in template folder names too.

Safety: nothing is touched until **Create project**; an existing job folder is never added to; if anything fails
part way, the folders made by that attempt are removed again (only empty folders this app just created); a job
number already in use is flagged. Every job made is recorded in `created_projects.json` in the app's folder, and in
the log.

Drives, the locations every job also gets, and the templates folder can also be edited under **Templates and locations** in the
same panel. In paths, `{job}` is the job name; `{client_folder}`, `{client}`, `{job_no}`, `{title}`, `{yyyy}`,
`{mm}` and `{dd}` also work. The Resolve library, template project, folder and project name are set there too.
Creating the Frame.io project comes next.

A hidden (`-nogui`) Resolve stops answering scripts as soon as it opens a project (checked on Resolve 21.1), so
the hidden Resolve never opens one: projects that can only be backed up by opening them are retried once Resolve
is open normally.

## Restoring locally

If the cloud is unreachable: in Resolve switch to a local (Disk or PostgreSQL) database, right-click in the Project
Manager → *Import Project*, and pick the newest `.drp` from the project's backup folder. Relink media if paths differ.

## Behaviour you should know about

- **It never disturbs open work.** It only loads a project (the fallback described above) when you have *no*
  project open, and it restores the folder you were looking at in the Project Manager.
- **Resolve must be running** (or, on a Mac, startable hidden). If it can't be reached, the service retries every
  5 minutes. For an always-on setup, run Resolve on a spare workstation (`-nogui` works with scripting) rather than
  your editing machine.
- **`.drp` files contain the project (timelines, grades, media pool), not the media.**
- **Complement, not replacement,** for Blackmagic Cloud's own project history and your media backups.
- **Self-hosted PostgreSQL project server instead of Blackmagic Cloud?** This still works (it appears as a
  `PostgreSQL` database in `list`). For disaster recovery you could additionally run `pg_dump` on the server.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The 193 tests run against a fake Resolve API (incremental detection, nested folders, open-elsewhere and
collaboration scenarios, backoff and alerts, `probe`, database switching, retention, caps, filters) plus the
scheduler, the hidden-Resolve hand-over (with faked processes), the dashboard's API and its security (token, host/origin checks), the menu bar logic and icons, the installer, and new project set-up (templates, previews, creating and rolling back folders, and its dashboard API).

## Not verified against real Resolve or macOS

I could not run this against a real Resolve install, and the Mac app has not been built or run on a real Mac (its logic, icons, in-process service and packaging of the service and dashboard are tested on Linux; the `rumps` menu code, the macOS build script and the launch-at-login/launchd steps are not). Resolve's scripting library is loaded from Resolve's own folder at runtime, and I haven't confirmed it loads inside the packaged app; if backups report that Resolve is unreachable although it is running with scripting on, use the script install instead and tell me. The dashboard was checked in a real browser engine against a simulated Resolve. If the icon doesn't appear, look at `logs/menubar.err.log` and use `resolve_cloud_backup.py dashboard` meanwhile. The pieces most worth checking on your first run with `list`/`once`:
the cloud database's `DbType`, the `lastModifiedDate` format (change detection compares the raw value, so an
unfamiliar format still works; it is only used to apply the first-run `initial_lookback_days` filter, and if it
can't be parsed on the first run, up to `max_exports_per_cycle` projects are exported per cycle until all are
covered), and that the scripted `ExportProject` call behaves like the Project Manager's own export on cloud
projects and on projects open elsewhere. It should, but `probe` confirms it for your setup in a minute.
