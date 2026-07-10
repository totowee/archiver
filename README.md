# Folder Archiver

Zips a folder on a recurring schedule and keeps only the newest N zips — older
ones are deleted automatically. Runs in the background via macOS launchd, so it
keeps archiving with no app or browser open. Pause, resume, or delete a job any
time.

**Get the app**: download `Folder.Archiver-…-mac-arm64.zip` from the
[Releases page](https://github.com/totowee/archiver/releases) (first launch:
right-click → Open, since the build is ad-hoc signed), or build it locally
with `./build-app.sh` (see Development below). The bundle itself isn't
committed to the repo.

## Quick start

1. Double-click **Folder Archiver.app** — a window opens listing your jobs (or
   an empty state the first time).
2. **New Job** → drag the folder you want archived from Finder into **Source**
   and a folder or disk into **Destination** — or click **+** in either box to
   browse. The **archive name** (the `<Name>` in `YYYY-MM-DD HH.MM.SS <Name>.zip`) is
   editable and defaults to the folder's name, with a live filename preview;
   the destination path is editable too (it defaults to a `<Name> Archives`
   folder). **Exclude patterns** control what's left out of the archive — the
   field comes prefilled for Avid projects (`*.log *.lck ._* SearchData
   WaveformCache`); edit it or clear it to archive everything. Pick a schedule
   (every 15 min / 30 min / 1 h / 2 h / 4 h — or daily, weekly, or monthly at
   a set time) → set how many zips to keep (default 10) → **Create Job**.
   Dropping a folder anywhere on the main window also starts a new job with it
   as the source.
3. A banner offers **Run Now** — macOS will ask for folder access the first time
   (click Allow). After that, everything is automatic.

Archives are named `2026-07-09 14.30.55 <Folder Name>.zip` — date and time
first so they sort chronologically in Finder (a re-run within the same second
gets ` (2)`, ` (3)`, …). Finder-style zips, made with `ditto`, so Finder
metadata survives.

## The window

The main window is a live table of jobs (label, schedule, keep, status, last
run), refreshed every few seconds. Select a job to see its detail panel and act
on it:

- **Run Now** — archive immediately.
- **Pause** / **Resume** — stop or restart the schedule (existing zips stay).
- **View Log** — the tail of that job's log, in-app.
- **Reveal in Finder** — open the archives folder (selecting the newest zip).
- **Delete Job…** — removes the job only; existing zips are never deleted.

**New Job** is in the header, alongside **Open Log Folder**. The New Job form
is a source → destination picker: two drop zones that take folders (or disks)
dragged straight from Finder, plus an editable archive name and destination
path. While a job is zipping, its row shows a live progress bar with a
percentage (from the growing zip vs. the folder's size, scaled by the last
run's compression ratio — so it's an estimate on the very first run). Jobs
keep running on schedule even when the window is closed and the app has quit —
the schedule lives in launchd, not in the app.

## Terminal use (same engine)

```
./folder-archiver.py add --source <folder> --dest <folder> --every 1h --keep 10
./folder-archiver.py add --source <folder> --dest <folder> --daily 03:00
./folder-archiver.py add --source <folder> --dest <folder> --weekly sun 03:00
./folder-archiver.py add --source <folder> --dest <folder> --monthly 1 03:00
./folder-archiver.py add ... --exclude '*.lck' --exclude 'WaveformCache'
./folder-archiver.py list | status | run-now <slug> | pause <slug> | resume <slug>
./folder-archiver.py remove <slug> | log <slug> | doctor [--fix]
```

`list --json` prints a machine-readable array (this is what the GUI polls); add
`--check-launchd` to include each job's launchd load state.

By default a run is **skipped when nothing in the folder changed** since the
newest zip (checked via file/folder modification times), so retention copies
aren't wasted on identical snapshots. Use `--archive-unchanged` on `add` to
archive every time regardless.

## Good to know

- **Scheduling is launchd**: interval jobs fire every N minutes while the Mac
  is awake; firings during sleep are skipped (no pile-up). Daily / weekly /
  monthly jobs run at the set time, or once on wake if the Mac was asleep at
  that time — but a run whose time passes while the Mac is shut down is
  skipped entirely (no catch-up on boot). The GUI's monthly picker offers days
  1–28 so a job can never hit a day the month doesn't have. The first
  automatic run happens one interval after the job is created.
- A **"Background items added"** notification appears once per new job (System
  Settings → Login Items shows it under python3). Normal.
- If a run fails with **no folder access**, macOS blocked the background
  `python3` (common when a job touches Documents / Desktop / Downloads). The
  job's detail pane shows the fix and an **Open Privacy Settings** button:
  grant **Full Disk Access** to `/usr/bin/python3` (in the picker press ⇧⌘G
  and type the path), then **Run Now**. `doctor` flags this too.
- **Exclude patterns** stage the run through the system `rsync` (filtered copy
  next to the zip, then zipped, staging removed) — so those runs briefly need
  about twice the source's filtered size free on the destination. Files that
  vanish mid-copy (lock files on a live project) are tolerated. Changes to
  excluded files don't count as "changes" for the skip-unchanged check.
- An unmounted source (external drive) is skipped quietly and picked up again
  next time the drive is there.
- Big folders: a lock prevents overlapping runs, and a run aborts safely if
  the destination volume would drop below ~1 GiB free.

## Where things live

```
~/Library/Application Support/FolderArchiver/   config (jobs.json), state, installed CLI copy
~/Library/LaunchAgents/com.folderarchiver.*     one schedule plist per job
~/Library/Logs/FolderArchiver/<slug>.log        one log per job
```

The plists point at the *installed* CLI copy in Application Support (synced
automatically whenever a job is added/resumed), so jobs keep working even if
this folder or the app is moved.

## Development

The GUI is a small Electron app in `app/` (plain JS — `main.js`, `preload.js`,
`renderer/`). The Python engine is the single source of truth; the GUI only
shells out to it.

- **Prereq (building only):** Node + npm (e.g. `brew install node`). The
  packaged app embeds its own runtime, so end users need nothing.
- **Run the GUI in dev:** `cd app && npx electron .` — it uses the engine copy
  in this folder, so engine edits are picked up without repackaging.
- **Build the app:** `./build-app.sh` — installs deps (first run downloads
  Electron, ~120 MB), packages **Folder Archiver.app** with
  `@electron/packager`, and bundles `folder-archiver.py` into
  `Contents/Resources/` (outside asar, so launchd's external `python3` can run
  it). Run this after editing `folder-archiver.py` or anything in `app/`.
- `app/node_modules/` and `dist/` are disposable build artifacts.
- The engine stays **Python 3.9-compatible, stdlib only** — launchd runs it
  under `/usr/bin/python3` (the CLT shim) because that path survives Python
  upgrades.

## Uninstall

Delete every job (`remove` keeps your zips), then delete
`~/Library/Application Support/FolderArchiver`,
`~/Library/Logs/FolderArchiver`, and this folder (which includes
`app/node_modules`).
