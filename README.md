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

- **Dashboard:** the service runs a small web page on your own machine only (`127.0.0.1`), unless you list
  `lan_hostnames` in `config.json` to open it to other machines (see Configuration).
  It has four pages: **Home** (the start page: backup status with Back up now / Stop / Pause, every drive's free
  space, and the jobs made recently), **New project**, **QC** (Vidchecker checks) and **Backups**. Drives under 15% free show amber, under 5% red; drives set up here but not connected
  are listed too, and shares on the same storage say so.
  The bar at the top of every page shows whether backups are running. The Backups page shows a timeline of every check with a live playhead, what is being exported right now and how far through
  the library it is, an activity feed, every project's backup status, and a **Settings** drawer (backup folder,
  interval, active hours, include/exclude, retention) that saves straight to `config.json`.
  Open it with `python3 resolve_cloud_backup.py dashboard` (prints the address) or from the menu bar icon.
  The address contains a private access token, so other websites and other users on the machine can't use it.
- **macOS menu bar icon:** a coloured dot: green up to date, blue backing up, amber waiting for Resolve,
  red something failed, grey paused or the service isn't running. Click it for the status line, *Open Colour Project Manager* (Home), *New project…*, *Backups…* (the Backups page), *Back up now*, *Stop this backup* (ends the check that's
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

Changes apply from the next cycle without restarting. Only the `dashboard` settings need a restart.

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
| `own_resolve_export_limit_minutes` | `15` | How long one project's backup may take in a Resolve you have open. Past that the app stops waiting, counts that project as failed for now and carries on. Your Resolve is never stopped. See "Self-healing". |
| `resolve_memory_warn_gb` | `24` | Warn on the dashboard, in the menu bar and through `alert_command` when the Resolve you have open uses more memory than this. `0` = never. |
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
| `dashboard.lan_hostnames` | `[]` | Names other machines may use for the dashboard, e.g. `["colour.local"]`. When set, the dashboard also listens on the network and accepts those names; Frame.io sign-in still has to be done on this machine. On a Mac each `.local` name is advertised by Bonjour, so other machines find it with no DNS setup. Traffic is plain HTTP, so use it only on a network you trust. |
| `dashboard.lan_web_port` | `80` | With `lan_hostnames`, also serve the dashboard on this port, so port 80 gives plain `http://colour.local`. If the port is taken the dashboard carries on without it (see the log). `null` = off. |
| `dashboard.lan_token` | `true` | `false` lets anyone on the network open the dashboard through a `lan_hostnames` name without the access token. `127.0.0.1` still needs it, and the host and same-origin checks still block other websites. |

## Projects open elsewhere

Backups must not depend on nobody having the project open, so export never needs the project to be free:

1. **Direct export from the Project Manager.** `ExportProject` works on the project as saved in the database
   without opening it, so a project that is open (or locked) on another machine is backed up like any other.
   This is the normal path and the one nearly every backup takes.
2. **Fallback: load, export, close.** If a direct export fails, the service opens the project in *its own*
   Resolve (read-only is fine when it's open elsewhere), exports it and closes it again. This is the default
   (`load_to_export: "auto"`) and applies to any project.
3. **If both fail,** the project is retried with backoff and flagged in the log, `status` and `alert_command`
   rather than failing silently. **Clear failed backups** on the Backups page forgets every failure, so each
   project is tried afresh the next time its library is checked. A failure in a library the app no longer checks
   (not a cloud library it takes in turn, and not open in Resolve for 7 days, such as the local library from a day
   Resolve had it open) shows as *Failed, library not checked* and doesn't count as failing.

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
- **If the hidden Resolve stops answering** (a call into it never comes back), the watchdog stops it once the call
  has run for 1 minute (making a new project, normally about 15 s) or 3 minutes (backing up one project). Such a call freezes the
  whole app, dashboard included, so the limit is kept by the watchdog, not the app. The check or new project then
  fails and is retried as usual; Retry on a new project never makes a second copy.
- **If the hidden Resolve crashes,** the check ends and your library choice is put back straight away. Resolve may
  show its own crash-report window.
- **If the power goes,** your library choice is put back when the app next starts (at login). Open Resolve before
  that and it opens on the library that was being checked; switch back once in the Project Manager. The choice is
  only ever changed by writing a new file and swapping it in one step, so it is never left half-written.
- A hidden Resolve left running (for example if the watchdog was stopped too) is used by the next check and then
  quit.
- Turn it off with `"background_resolve": false` (or the Settings checkbox).
- **If the Mac restarts or logs out during a hidden check,** the app stops the hidden Resolve straight away. If
  macOS reopens it anyway at the next login (as an ordinary Resolve, window and all), the app takes it back: only
  when it started with that login, isn't in front and has no project open. It is quit and checks run hidden
  again. Any other Resolve is yours and is never quit.

## Self-healing

- **Calls into Resolve run in a separate process.** Resolve's scripting library holds Python's lock while it
  waits for Resolve, so these calls used to freeze the whole app. A Resolve left open for days grew to 53 GB, each
  backup took up to 20 minutes, and the dashboard, drive free space and menu bar stopped answering. Now the app only
  waits on that process, with a time limit on every call: 2 minutes for an ordinary call, 3 minutes per backup in
  the hidden Resolve, and `own_resolve_export_limit_minutes` (15) per backup in a Resolve you have open. When a
  call runs over, the process is replaced, that project is counted as failed (so it waits out its retry backoff),
  and the check ends with "DaVinci Resolve stopped answering". The next one starts in `retry_minutes`.
- **The Resolve you have open is watched, never stopped.** Past `resolve_memory_warn_gb` the dashboard and menu
  bar say it needs a restart, and `alert_command` runs once.
- **A health check restarts the app.** The Mac app installs a LaunchAgent (`com.resolvecloudbackup.health`) that
  runs the app's own program every minute. If the app crashed, it starts it again. If the app hasn't answered for
  5 checks in a row (its loop quiet for 45 minutes, or no answer at all), it stops the app and starts it again. A
  hidden Resolve it was using is quit by its watchdog. Quit the app from the menu bar and it is left alone.
- **Open at login follows the copy you run.** Each time the app starts, the login item is pointed at that copy,
  so opening a newer version once is enough. Before, an old copy in Downloads kept starting after every restart.

## New project set-up

**New project** (its own page, or **New project…** in the menu bar) sets up a job in one go. Every job gets
all of these, named after the job: its working folders on the workstation's RAID
(`Projects/<Client>/<Job>`, from the `Project` template), its deliverables folders on the deliverables drive
(`Deliverables/Projects/<Job>`, from the `Deliverables` template; exports only, never working projects) and its
DaVinci Resolve project with the bins, and its Frame.io project with the review folders.

1. Pick the **project drive**: the RAID of the workstation the job is on. Drives are added on the **Settings**
   page (**New project > Add a drive**), which offers every connected drive that isn't set up yet, already filled
   in as `Projects/<Client>/<Job>` with the `Project` template (change either for a drive that's laid out
   differently).
2. Pick the **job name** from the list, which comes from column G of the Job Report's CURRENT tab (newest number
   first; jobs already set up are at the bottom, to finish a missing part). The **client folder** is then picked
   for you: the folder that client's jobs went in most recently. To pick another, type part of its name in the
   client folder box: the list narrows as you type (arrow keys and Enter, or click). Only existing folders can be
   picked, to keep typos out of it; **Type a name that isn't on the list…** and **New client folder…** are there
   for the exceptions. A job picked from the list lands in a name box below it, so a producer's name can be tidied
   before **Create project**; the list still counts that entry as set up. A
   typed job name starts with the next free number. Every path is previewed live. A network drive that stops answering (a hung share)
   doesn't hold the page up: it's shown as not answering, its folders come from the last time it was read, and a job
   can't be made on it until it answers again.

   The job list is read from a link to just the job names, published from Google Sheets, so the Job Report itself
   (budgets, contacts) stays private: make a sheet with `=IMPORTRANGE("<Job Report ID>", "CURRENT!G:G")` in A1,
   publish it to the web as CSV, and paste the link on the **Settings** page under **New project > Job list**. The list is read
   again every two minutes, or when you press **Refresh**.
   The **DaVinci Resolve database** list picks where the Resolve project goes: the default one (set in Settings,
   where the template project lives) unless you pick another. In another database the project is made from the
   newest backup of the template project, so it needs to have been backed up once.
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
2. Default redirect URI: `https://localhost:8765/frameio/callback`. Redirect URI pattern: the address shown on the
   **Settings** page under Frame.io (normally `http://localhost:8765/frameio/callback`).
3. On **Settings**, paste the **Client ID** and the **Client Secret** and press **Connect Frame.io**: it saves both
   (the secret goes in the Mac keychain, never in the settings file) and opens Adobe's sign-in in a new tab. It
   won't start without the secret, which an OAuth Web App credential needs.
4. Choose the **workspace**, tick **Make a Frame.io project for every new job** and press **Save**.

The app stays signed in (the sign-in is renewed by itself). If Adobe ever ends it, the Frame.io row says so and
**Connect Frame.io** signs in again. **Disconnect** removes the sign-in from the keychain.

Templates are the folders inside the templates folder (for example `/Volumes/WashDeliverables/FolderStructures`).
Each top-level folder there is one template, and its sub-folders are copied (empty) into the new job, so changing a
template there changes every job made after. Placeholders such as `{job}` work in template folder names too.

**Finishing a job that's partly there.** Each part is looked for by the job's number (e.g. `WC092`), not just
its exact name, so a part made earlier under another name or spelling is found: the job's folder on any project
drive and in any client folder, its Deliverables folder, its Resolve project in any database (from the backup
record), and its Frame.io project. Parts found are shown and left alone, and only the missing ones are made.
Picking a job that's already on a drive picks that drive and client folder for you; making a second job folder on
another drive or in another client folder is refused, with where the job already is.

Safety: nothing is touched until **Create project**; an existing job folder is never added to; if anything fails
part way, the folders made by that attempt are removed again (only empty folders this app just created); a job
number already in use is flagged. Every job made is recorded in `created_projects.json` in the app's folder, and in
the log.

The New project page only asks for the job: everything set up once (drives, the locations every job also gets,
the templates folder, the job list, the Resolve project and Frame.io) is on the **Settings** page. In paths, `{job}` is the job name; `{client_folder}`, `{client}`, `{job_no}`, `{title}`, `{yyyy}`,
`{mm}` and `{dd}` also work.

A hidden (`-nogui`) Resolve stops answering scripts as soon as it opens a project (checked on Resolve 21.1), so
the hidden Resolve never opens one: projects that can only be backed up by opening them are retried once Resolve
is open normally.

## Frame.io page

The **Frame.io** page lists the videos added or changed in the last two weeks (project first, then the video and its
folder), then the projects created or changed in that time (new ones marked), each linking straight to Frame.io.
The newest 12 videos and 8 projects show, with **Show all** for the rest. **Home** shows the latest nine projects
under **Recent on Frame.io**. It shows the last
list at once and checks Frame.io again in the background every two minutes while it's open (the very first read
takes about 20 seconds). Only folders that have changed are opened, so it stays quick with hundreds of projects.

## QC with Vidchecker

The **QC** page runs checks on a Telestream Vidchecker server (8.x) and shows their results, without opening
Vidchecker:

1. Pick the **job** (the job folders on the deliverables drive, most recently changed first). Its exports (`.mov`,
   `.mxf`, `.mp4`…) are listed with their last Vidchecker result, so you can see what's been checked.
2. Pick the **check**: only the Vidchecker templates ticked on the **Settings** page are offered (for example
   the four Wash / Clearcast ones), so nobody picks the wrong one of the server's 60.
3. Tick the files and press **Run check**. **Recent checks** shows each one waiting, checking (with progress) and
   then Passed / Warning / Failed / Rejected with its alert count. **Open Vidchecker for reports** opens the
   server's own page for the full report.

The server runs Windows and sees the shares under its own drive letters (WashDeliverables is `W:`). The app works
out the server's path for a file from the share it's mounted from here, and asks the server whether it can see the
file before starting a check, so a check is never started on a path it can't open. A drive the server hasn't got
mapped (for example Colour-3-Raid) says so. Only exports inside a job folder can be sent from this app.

    "vidchecker": {"url": "http://10.0.0.66:51060", "templates": [117, 116, 111, 114]}

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

The 258 tests run against a fake Resolve API (incremental detection, nested folders, open-elsewhere and
collaboration scenarios, backoff and alerts, `probe`, database switching, retention, caps, filters) plus the
scheduler, the hidden-Resolve hand-over (with faked processes), the dashboard's API and its security (token, host/origin checks), the menu bar logic and icons, the installer, new project set-up (templates, previews, creating and rolling back folders, and its dashboard API), and self-healing: the Resolve worker process (real child processes against a fake scripting module, including a call that hangs), the health check, taking back a Resolve reopened by macOS, and the memory warning.

## Not verified against real Resolve or macOS

I could not run this against a real Resolve install, and the Mac app has not been built or run on a real Mac (its logic, icons, in-process service and packaging of the service and dashboard are tested on Linux; the `rumps` menu code, the macOS build script and the launch-at-login/launchd steps are not). Resolve's scripting library is loaded from Resolve's own folder at runtime, and I haven't confirmed it loads inside the packaged app; if backups report that Resolve is unreachable although it is running with scripting on, use the script install instead and tell me. The dashboard was checked in a real browser engine against a simulated Resolve. If the icon doesn't appear, look at `logs/menubar.err.log` and use `resolve_cloud_backup.py dashboard` meanwhile. The pieces most worth checking on your first run with `list`/`once`:
the cloud database's `DbType`, the `lastModifiedDate` format (change detection compares the raw value, so an
unfamiliar format still works; it is only used to apply the first-run `initial_lookback_days` filter, and if it
can't be parsed on the first run, up to `max_exports_per_cycle` projects are exported per cycle until all are
covered), and that the scripted `ExportProject` call behaves like the Project Manager's own export on cloud
projects and on projects open elsewhere. It should, but `probe` confirms it for your setup in a minute.
