#!/usr/bin/env python3
"""Folder Archiver — zip a folder on a recurring schedule, keep only the newest N zips.

Each job zips one source folder into a destination folder (Finder-style zip via
`ditto`), names archives  YYYY-MM-DD HH.MM.SS <Label>.zip  (date+time first so
they sort chronologically; a same-second re-run gets " (2)", " (3)", …), and
rotates them so only the newest N remain. Scheduling is one launchd LaunchAgent per job, so archiving
keeps running with no app open. Pause/resume/remove any time.

Runtime layout (all per-user, no sudo):
  ~/Library/Application Support/FolderArchiver/   config, state, locks, installed copy
  ~/Library/LaunchAgents/com.folderarchiver.*     one plist per job
  ~/Library/Logs/FolderArchiver/<slug>.log        one log per job

Kept Python 3.9-compatible on purpose: launchd jobs run under /usr/bin/python3
(the Command Line Tools shim, 3.9) because that path survives python.org upgrades.
Stdlib only.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import traceback

__version__ = "1.0.0"

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, "Library", "Application Support", "FolderArchiver")
BIN_DIR = os.path.join(APP_DIR, "bin")
INSTALLED_PY = os.path.join(BIN_DIR, "folder-archiver.py")
STATE_DIR = os.path.join(APP_DIR, "state")
LOCK_DIR = os.path.join(APP_DIR, "locks")
CONFIG_PATH = os.path.join(APP_DIR, "jobs.json")
CONFIG_LOCK = os.path.join(APP_DIR, "jobs.json.lock")
LAUNCH_AGENTS = os.path.join(HOME, "Library", "LaunchAgents")
LOG_DIR = os.path.join(HOME, "Library", "Logs", "FolderArchiver")
LABEL_NS = "com.folderarchiver."

PRESETS = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400}
DATE_FMT = "%Y-%m-%d"
TIME_FMT = "%H.%M.%S"  # dots — colons aren't safe in filenames
# Keep at least this much free on the destination volume beyond the estimated zip size.
DEFAULT_RESERVE = 1 << 30  # 1 GiB; override for tests via FOLDER_ARCHIVER_RESERVE_BYTES


# ---------- small utilities ----------

def ensure_dirs():
    for d in (APP_DIR, BIN_DIR, STATE_DIR, LOCK_DIR, LOG_DIR, LAUNCH_AGENTS):
        os.makedirs(d, exist_ok=True)


def atomic_write(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def die(msg):
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(2)


def path_inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent + os.sep)


# ---------- config / state ----------

def pick_python():
    p = "/usr/bin/python3"
    if os.access(p, os.X_OK):
        return p
    return sys.executable or "python3"


def default_config():
    return {"version": 1, "python_path": pick_python(), "jobs": {}}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return default_config()
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    cfg.setdefault("jobs", {})
    return cfg


def save_config(cfg):
    atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2, sort_keys=True).encode("utf-8"))


class config_lock(object):
    """Serializes read-modify-write cycles on jobs.json across processes."""

    def __enter__(self):
        ensure_dirs()
        self.f = open(CONFIG_LOCK, "a+")
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        self.f.close()
        return False


def state_path(slug):
    return os.path.join(STATE_DIR, f"{slug}.json")


def load_state(slug):
    try:
        with open(state_path(slug), "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(slug, d):
    ensure_dirs()
    atomic_write(state_path(slug), json.dumps(d, indent=2).encode("utf-8"))


# ---------- run progress (read by the GUI while a zip is being written) ----------

def progress_path(slug):
    return os.path.join(STATE_DIR, f"{slug}.progress.json")


def write_progress(slug, d):
    try:
        atomic_write(progress_path(slug), json.dumps(d).encode("utf-8"))
    except OSError:
        pass


def clear_progress(slug):
    try:
        os.remove(progress_path(slug))
    except OSError:
        pass


def load_progress(slug):
    """Progress of a run in flight; None if absent or stale (e.g. crashed run).

    The zipping loop refreshes the file every 0.5s; a long metadata scan can
    leave it untouched for a while, so staleness only means "don't trust the
    numbers" — the GUI falls back to an indeterminate bar via `running`.
    """
    p = progress_path(slug)
    try:
        if time.time() - os.path.getmtime(p) > 5:
            return None
        with open(p, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_running(slug):
    """True while some process holds this job's run lock (archive in progress)."""
    try:
        f = open(lock_path(slug), "a+")
    except OSError:
        return False
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        f.close()


# ---------- self-install (stable path for launchd) ----------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def self_sync():
    """Copy this script to the AppSupport location that launchd plists point at.

    Jobs must keep working if the workspace or the .app moves, so plists never
    reference the copy the user invoked — only the installed one.
    """
    ensure_dirs()
    me = os.path.realpath(os.path.abspath(__file__))
    if me == os.path.realpath(INSTALLED_PY):
        return False
    if os.path.exists(INSTALLED_PY) and sha256_file(me) == sha256_file(INSTALLED_PY):
        return False
    shutil.copy2(me, INSTALLED_PY)
    return True


# ---------- labels / slugs / per-job paths ----------

def sanitize_label(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60].strip() or "Archive"


def make_slug(label, taken):
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "job"
    slug, i = base, 2
    while slug in taken:
        slug = f"{base}-{i}"
        i += 1
    return slug


def launchd_label(slug):
    return LABEL_NS + slug


def plist_path(slug):
    return os.path.join(LAUNCH_AGENTS, launchd_label(slug) + ".plist")


def log_path(slug):
    return os.path.join(LOG_DIR, f"{slug}.log")


def lock_path(slug):
    return os.path.join(LOCK_DIR, f"{slug}.lock")


# ---------- schedules ----------

# Index = launchd's Weekday number (0 = Sunday), used verbatim in the plists.
WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def ordinal(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def schedule_human(sched):
    if sched["type"] == "interval":
        secs = int(sched["seconds"])
        pretty = {900: "every 15 min", 1800: "every 30 min", 3600: "every hour",
                  7200: "every 2 hours", 14400: "every 4 hours"}
        if secs in pretty:
            return pretty[secs]
        if secs % 3600 == 0:
            return f"every {secs // 3600} h"
        if secs % 60 == 0:
            return f"every {secs // 60} min"
        return f"every {secs} s"
    t = "{:02d}:{:02d}".format(int(sched["hour"]), int(sched["minute"]))
    if sched["type"] == "weekly":
        return f"weekly on {WEEKDAY_NAMES[int(sched['weekday'])]} at {t}"
    if sched["type"] == "monthly":
        return f"monthly on the {ordinal(sched['day'])} at {t}"
    return f"daily at {t}"


def parse_daily(text):
    m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
    if not m:
        raise ValueError("time must be HH:MM (24-hour), e.g. 03:00")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ValueError("time must be within 00:00-23:59")
    return h, mi


def parse_weekday(text):
    t = text.strip().lower()
    if t.isdigit():
        n = int(t)
        if n > 6:
            raise ValueError("weekday number must be 0-6 (0 = Sunday)")
        return n
    if len(t) >= 3:
        for i, name in enumerate(WEEKDAY_NAMES):
            if name.lower().startswith(t):
                return i
    raise ValueError("weekday must be sun/mon/tue/wed/thu/fri/sat (or 0-6, 0 = Sunday)")


def parse_monthday(text):
    try:
        d = int(text)
    except ValueError:
        raise ValueError("day of month must be a number, 1-31")
    if not 1 <= d <= 31:
        raise ValueError("day of month must be 1-31")
    return d


# ---------- launchd ----------

def launchctl(*args):
    return subprocess.run(["launchctl"] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=30)


def gui_domain():
    return f"gui/{os.getuid()}"


def is_loaded(slug):
    return launchctl("print", f"{gui_domain()}/{launchd_label(slug)}").returncode == 0


def write_job_plist(slug, job, python_path):
    d = {
        "Label": launchd_label(slug),
        "ProgramArguments": [python_path, INSTALLED_PY, "run", slug],
        "RunAtLoad": False,
        "StandardOutPath": log_path(slug),
        "StandardErrorPath": log_path(slug),
    }
    sched = job["schedule"]
    if sched["type"] == "interval":
        d["StartInterval"] = int(sched["seconds"])
    else:
        cal = {"Hour": int(sched["hour"]), "Minute": int(sched["minute"])}
        if sched["type"] == "weekly":
            cal["Weekday"] = int(sched["weekday"])  # launchd: 0 = Sunday
        elif sched["type"] == "monthly":
            cal["Day"] = int(sched["day"])
        d["StartCalendarInterval"] = cal
    atomic_write(plist_path(slug), plistlib.dumps(d))


def bootout_job(slug):
    launchctl("bootout", f"{gui_domain()}/{launchd_label(slug)}")  # fine if not loaded


def bootstrap_job(slug):
    bootout_job(slug)
    r = launchctl("bootstrap", gui_domain(), plist_path(slug))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip() or r.returncode}")


# ---------- job log ----------

def jlog(slug, msg, echo=False):
    line = f"{now_stamp()} [{slug}] {msg}"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_path(slug), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    # Echo for interactive use. Under launchd stdout is the log file itself and
    # isatty() is false, so nothing is written twice.
    if echo or sys.stdout.isatty():
        print(line)


# ---------- archive scanning ----------

def archive_re(label):
    # "2026-07-09 14.30.55 Label.zip"; a same-second re-run is
    # "2026-07-09 14.30.55 Label (2).zip", … Groups: date, time, n.
    return re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}\.\d{2}\.\d{2}) %s(?: \((\d+)\))?\.zip$"
                      % re.escape(label))


def legacy_archive_re(label):
    # Date-only names from before times were added — still counted by rotation.
    return re.compile(r"^(\d{4}-\d{2}-\d{2}) %s(?: \((\d+)\))?\.zip$" % re.escape(label))


def list_archives(dest, label):
    """[(epoch_ts, filename)] for this job's zips only, sorted oldest -> newest.

    epoch_ts comes from the date+time in the name; same-second copies are
    ordered by their " (n)" suffix (the plain name counts as n=1). Older
    date-only names still match, counted at midnight so they sort before —
    i.e. older than — any timed zip of the same day.
    """
    rx = archive_re(label)
    legacy_rx = legacy_archive_re(label)
    out = []
    try:
        names = os.listdir(dest)
    except OSError:
        return []
    for n in names:
        m = rx.match(n)
        if m:
            stamp, copy_n = f"{m.group(1)} {m.group(2)}", int(m.group(3) or 1)
            fmt = f"{DATE_FMT} {TIME_FMT}"
        else:
            m = legacy_rx.match(n)
            if not m:
                continue
            stamp, copy_n = m.group(1), int(m.group(2) or 1)
            fmt = DATE_FMT
        try:
            ts = time.mktime(time.strptime(stamp, fmt))
        except (ValueError, OverflowError):
            continue
        out.append((ts, copy_n, n))
    out.sort()
    return [(ts, name) for ts, _, name in out]


def scan_source(source):
    """One metadata-only walk: newest mtime (files AND dirs — dir mtimes catch
    deletes/renames/copied-in-old-files), total apparent bytes, file count."""
    newest, total, count = 0.0, 0, 0
    for root, dirs, files in os.walk(source):
        try:
            st = os.stat(root, follow_symlinks=False)
            if st.st_mtime > newest:
                newest = st.st_mtime
        except OSError:
            pass
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if st.st_mtime > newest:
                newest = st.st_mtime
            total += st.st_size
            count += 1
    return newest, total, count


# ---------- the run itself ----------

def run_job(slug, manual):
    ensure_dirs()
    cfg = load_config()
    job = cfg["jobs"].get(slug)

    def out(msg):
        jlog(slug, msg, echo=manual)

    if job is None:
        out("ERROR: unknown job")
        return 1
    if not job.get("enabled", True) and not manual:
        out("skipped: job is paused")
        return 0

    lock_f = open(lock_path(slug), "a+")
    try:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        out("skipped: previous run still in progress")
        return 0

    clear_progress(slug)  # debris from a crashed run
    started = time.time()

    def finish(result, extra=None):
        st = {"last_run": now_stamp(), "result": result,
              "duration_s": round(time.time() - started, 1)}
        if extra:
            st.update(extra)
        save_state(slug, st)

    try:
        return _run_job_locked(slug, job, out, finish, started)
    except Exception:
        tb = traceback.format_exc().strip().splitlines()
        out("ERROR: unexpected failure: " + " | ".join(tb[-3:]))
        finish("error: unexpected failure")
        return 1
    finally:
        clear_progress(slug)
        lock_f.close()


def _run_job_locked(slug, job, out, finish, started):
    source, dest, label = job["source"], job["dest"], job["label"]

    if not os.path.isdir(source):
        out(f"skipped: source unavailable ({source})")
        finish("skipped-no-source")
        return 0
    if path_inside(dest, source):
        out("ERROR: destination is inside the source folder — refusing to archive")
        finish("error: dest inside source")
        return 1
    try:
        os.makedirs(dest, exist_ok=True)
    except PermissionError:
        out(f"ERROR: macOS blocked access to the destination folder ({dest}). "
            "Fix: System Settings > Privacy & Security > Full Disk Access > add /usr/bin/python3, "
            "then Run Now again.")
        finish("error: no folder access")
        return 1
    except OSError as e:
        out(f"skipped: destination unavailable ({e})")
        finish("skipped-no-dest")
        return 0

    # macOS privacy (TCC): a launchd python3 that hasn't been granted access
    # to protected folders (Documents/Desktop/Downloads, external drives) gets
    # "Operation not permitted" on every read — and os.walk/list_archives would
    # swallow that into "0 files"/"no archives". Probe both folders up front so
    # the failure is one clear instruction instead of a traceback.
    for what, p in (("source", source), ("destination", dest)):
        try:
            os.listdir(p)
        except PermissionError:
            out(f"ERROR: macOS blocked access to the {what} folder ({p}). "
                "Fix: System Settings > Privacy & Security > Full Disk Access > add /usr/bin/python3, "
                "then Run Now again.")
            finish("error: no folder access")
            return 1
        except OSError:
            pass  # unmounted volume etc. — the existing checks handle those

    write_progress(slug, {"phase": "scanning"})
    newest_mtime, total_bytes, file_count = scan_source(source)
    existing = list_archives(dest, label)

    if job.get("skip_if_unchanged", True) and existing:
        # Zip filenames only carry the date, so prefer the exact mtime
        # snapshot recorded when the newest zip was made; the fallback
        # (midnight of the zip's date) is coarse and errs toward re-archiving.
        st = load_state(slug)
        if st.get("snapshot_mtime") is not None and st.get("zip") == existing[-1][1]:
            unchanged = newest_mtime <= st["snapshot_mtime"]
        else:
            unchanged = newest_mtime <= existing[-1][0]
        if unchanged:
            out(f"skipped: no changes since {existing[-1][1]}")
            finish("skipped-unchanged", {"kept": len(existing),
                                         "zip": existing[-1][1],
                                         "snapshot_mtime": st.get("snapshot_mtime")})
            return 0

    reserve = int(os.environ.get("FOLDER_ARCHIVER_RESERVE_BYTES", DEFAULT_RESERVE))
    free = shutil.disk_usage(dest).free
    if free < total_bytes + reserve:
        out(f"ERROR: not enough free space on destination "
            f"(need ~{human_size(total_bytes)} + {human_size(reserve)} reserve, have {human_size(free)})")
        finish("error: insufficient free space")
        return 1

    # Clear crash debris: our own .part / .part.err files older than a day.
    # The time segment is optional so debris from the older date-only naming
    # still gets cleaned up.
    part_rx = re.compile(r"^\d{4}-\d{2}-\d{2}(?: \d{2}\.\d{2}\.\d{2})? %s(?: \(\d+\))?\.zip\.part(\.err)?$"
                         % re.escape(label))
    try:
        debris = os.listdir(dest)
    except OSError:
        debris = []
    for n in debris:
        if part_rx.match(n):
            p = os.path.join(dest, n)
            try:
                if time.time() - os.path.getmtime(p) > 86400:
                    os.remove(p)
            except OSError:
                pass

    # Date+time-first names sort chronologically in Finder. A same-second
    # re-run gets a Finder-style " (2)", " (3)", … suffix, continuing from the
    # HIGHEST n seen for that second — never reusing a freed lower name —
    # because the newest zip must sort newest or rotation would delete it
    # immediately.
    now_dt = datetime.datetime.now()
    stamp = f"{now_dt.strftime(DATE_FMT)} {now_dt.strftime(TIME_FMT)}"
    rx = archive_re(label)
    max_n = 0
    for _, name in existing:
        m = rx.match(name)
        if m and f"{m.group(1)} {m.group(2)}" == stamp:
            max_n = max(max_n, int(m.group(3) or 1))
    final_name = f"{stamp} {label}.zip" if max_n == 0 else f"{stamp} {label} ({max_n + 1}).zip"
    while os.path.exists(os.path.join(dest, final_name)):  # race guard only
        max_n += 1
        final_name = f"{stamp} {label} ({max_n + 1}).zip"
    part = os.path.join(dest, final_name + ".part")
    final = os.path.join(dest, final_name)

    # Zip with live progress: ditto reports nothing, so watch the .part file
    # grow and estimate the final size from the last run's compression ratio
    # (1.0 on a first run — about right for media files; capped at 99% until
    # the zip is really finished).
    est_ratio = 1.0
    prev = load_state(slug)
    if prev.get("zip_bytes") and prev.get("src_bytes"):
        try:
            est_ratio = min(1.5, max(0.05, float(prev["zip_bytes"]) / float(prev["src_bytes"])))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    est_final = max(1, int(total_bytes * est_ratio))

    # stderr goes to a file, not a pipe: nobody drains a pipe while we sit in
    # the progress loop, and a full pipe buffer would deadlock ditto.
    err_path = part + ".err"
    with open(err_path, "w") as errf:
        proc = subprocess.Popen(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", source, part],
                                stdout=subprocess.DEVNULL, stderr=errf)
        while True:
            try:
                rc = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                try:
                    part_bytes = os.path.getsize(part)
                except OSError:
                    part_bytes = 0
                write_progress(slug, {"phase": "zipping", "zip": final_name,
                                      "part_bytes": part_bytes, "est_bytes": est_final,
                                      "pct": min(99, part_bytes * 100 // est_final)})
    if rc != 0:
        try:
            with open(err_path, "r") as f:
                tail = " | ".join(f.read().strip().splitlines()[-3:])
        except OSError:
            tail = ""
        for p in (part, err_path):
            try:
                os.remove(p)
            except OSError:
                pass
        out(f"ERROR: ditto failed (rc {rc}) {tail}")
        finish(f"error: ditto rc {rc}")
        return 1
    try:
        os.remove(err_path)
    except OSError:
        pass
    os.replace(part, final)  # rotation below never sees half-written zips
    zip_bytes = os.path.getsize(final)

    archives = list_archives(dest, label)
    keep = max(1, int(job.get("keep", 10)))
    deleted = 0
    while len(archives) > keep:
        _, name = archives.pop(0)
        try:
            os.remove(os.path.join(dest, name))
            deleted += 1
        except OSError as e:
            out(f"WARNING: could not delete {name} ({e})")
            break

    dur = time.time() - started
    out(f"archived {final_name} ({human_size(zip_bytes)} from {file_count} files, "
        f"{dur:.1f}s, kept {len(archives)}, deleted {deleted})")
    finish("archived", {"zip": final_name, "zip_bytes": zip_bytes,
                        "kept": len(archives), "deleted": deleted,
                        "snapshot_mtime": newest_mtime,
                        "src_bytes": total_bytes})  # → next run's progress estimate
    return 0


# ---------- commands ----------

def cmd_add(args):
    ensure_dirs()
    self_sync()

    source = os.path.realpath(os.path.expanduser(args.source))
    if not os.path.isdir(source):
        die(f"source is not a folder: {source}")
    dest = os.path.realpath(os.path.expanduser(args.dest))
    if path_inside(dest, source):
        die("destination must not be inside the source folder — each zip would swallow the previous ones")
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        die(f"cannot create destination: {e}")

    label = sanitize_label(args.label or os.path.basename(source))

    try:
        if args.every:
            schedule = {"type": "interval", "seconds": PRESETS[args.every]}
        elif args.daily:
            h, m = parse_daily(args.daily)
            schedule = {"type": "daily", "hour": h, "minute": m}
        elif args.weekly:
            wd = parse_weekday(args.weekly[0])
            h, m = parse_daily(args.weekly[1])
            schedule = {"type": "weekly", "weekday": wd, "hour": h, "minute": m}
        else:
            day = parse_monthday(args.monthly[0])
            h, m = parse_daily(args.monthly[1])
            schedule = {"type": "monthly", "day": day, "hour": h, "minute": m}
    except ValueError as e:
        die(str(e))

    if args.keep < 1:
        die("--keep must be at least 1")

    with config_lock():
        cfg = load_config()
        for s, j in cfg["jobs"].items():
            if os.path.realpath(j["dest"]) == dest and j["label"] == label:
                die(f"job '{s}' already archives to the same destination with the same label — "
                    f"their rotations would delete each other's zips")
        if args.slug:
            slug = args.slug
            if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
                die("--slug must be lowercase letters/digits/hyphens")
            if slug in cfg["jobs"]:
                die(f"slug already exists: {slug}")
        else:
            slug = make_slug(label, cfg["jobs"])
        python_path = pick_python()
        cfg["python_path"] = python_path
        cfg["jobs"][slug] = {
            "label": label,
            "source": source,
            "dest": dest,
            "schedule": schedule,
            "keep": args.keep,
            "skip_if_unchanged": not args.archive_unchanged,
            "enabled": True,
            "created": now_stamp(),
        }
        save_config(cfg)
        write_job_plist(slug, cfg["jobs"][slug], python_path)
        bootstrap_job(slug)

    if args.tsv:
        print(slug)
    else:
        when = ("first run about one interval from now"
                if schedule["type"] == "interval"
                else "first run at the next scheduled time")
        print(f"Created job '{label}' (slug: {slug})")
        print(f"  archives  {os.path.join(dest, 'YYYY-MM-DD HH.MM.SS ' + label + '.zip')}")
        print(f"  schedule  {schedule_human(schedule)}  ({when})")
        print(f"  keeps     newest {args.keep} zips, older ones auto-deleted")
        print(f"  log       {log_path(slug)}")
    return 0


def job_status_word(job):
    return "active" if job.get("enabled", True) else "paused"


def last_run_summary(slug):
    st = load_state(slug)
    if not st.get("last_run"):
        return "never run"
    res = st.get("result", "?")
    if res == "archived":
        return f"archived {st.get('zip', '?')} ({human_size(st.get('zip_bytes', 0))}) @ {st['last_run']}"
    return f"{res} @ {st['last_run']}"


def tsv_line(slug, job):
    return "\t".join([slug, job["label"], schedule_human(job["schedule"]),
                      str(job.get("keep", 10)), job_status_word(job), last_run_summary(slug)])


def job_json(slug, job, check_launchd=False):
    """One job as a plain dict for `list --json` (consumed by the GUI)."""
    st = load_state(slug)
    last_run = None
    if st.get("last_run"):
        last_run = {
            "at": st.get("last_run"),
            "result": st.get("result"),
            "summary": last_run_summary(slug),
            "zip": st.get("zip"),
            "zip_bytes": st.get("zip_bytes"),
            "kept": st.get("kept"),
            "deleted": st.get("deleted"),
            "duration_s": st.get("duration_s"),
        }
    d = {
        "slug": slug,
        "label": job["label"],
        "source": job["source"],
        "dest": job["dest"],
        "schedule": job["schedule"],
        "schedule_human": schedule_human(job["schedule"]),
        "keep": job.get("keep", 10),
        "skip_if_unchanged": job.get("skip_if_unchanged", True),
        "enabled": job.get("enabled", True),
        "status": job_status_word(job),
        "created": job.get("created"),
        "source_available": os.path.isdir(job["source"]),
        "log_path": log_path(slug),
        "last_run": last_run,
        "running": is_running(slug),
    }
    d["progress"] = load_progress(slug) if d["running"] else None
    if check_launchd:
        d["loaded"] = is_loaded(slug)
    return d


def cmd_list(args):
    cfg = load_config()
    jobs = cfg["jobs"]
    if getattr(args, "json", False):
        check = getattr(args, "check_launchd", False)
        payload = [job_json(slug, jobs[slug], check) for slug in sorted(jobs)]
        print(json.dumps(payload, indent=2))
        return 0
    if args.tsv:
        for slug in sorted(jobs):
            print(tsv_line(slug, jobs[slug]))
        return 0
    if not jobs:
        print("No archive jobs yet. Create one with:")
        print("  folder-archiver.py add --source <folder> --dest <folder> --every 1h --keep 10")
        return 0
    rows = [("SLUG", "LABEL", "SCHEDULE", "KEEP", "STATUS", "LAST RUN")]
    for slug in sorted(jobs):
        j = jobs[slug]
        rows.append((slug, j["label"], schedule_human(j["schedule"]),
                     str(j.get("keep", 10)), job_status_word(j), last_run_summary(slug)))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        cells = [c.ljust(widths[i]) for i, c in enumerate(r[:5])] + [r[5]]
        print("  ".join(cells).rstrip())
    return 0


def cmd_status(args):
    cfg = load_config()
    jobs = cfg["jobs"]
    if args.slug and args.slug not in jobs:
        die(f"unknown job: {args.slug}")
    slugs = [args.slug] if args.slug else sorted(jobs)
    if args.tsv:
        for slug in slugs:
            print(tsv_line(slug, jobs[slug]))
        return 0
    if not jobs:
        print("No archive jobs yet.")
        return 0
    blocks = []
    for slug in slugs:
        j = jobs[slug]
        loaded = is_loaded(slug)
        status = job_status_word(j)
        if loaded != j.get("enabled", True):
            status += "  [launchd out of sync — run: doctor --fix]"
        lines = [
            f"{j['label']}  ({slug})",
            f"  status    {status}",
            f"  schedule  {schedule_human(j['schedule'])}",
            f"  source    {j['source']}" + ("" if os.path.isdir(j["source"]) else "  [not available]"),
            f"  archives  {j['dest']}  (keep newest {j.get('keep', 10)})",
            f"  last run  {last_run_summary(slug)}",
        ]
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    return 0


def cmd_run(args):
    return run_job(args.slug, manual=False)


def cmd_run_now(args):
    cfg = load_config()
    if args.slug not in cfg["jobs"]:
        die(f"unknown job: {args.slug}")
    if is_loaded(args.slug):
        # Kickstart runs in the real launchd context, so the one-time macOS
        # folder-access prompt (Desktop/Documents/external drives) is granted to
        # the same process that scheduled runs use.
        r = launchctl("kickstart", f"{gui_domain()}/{launchd_label(args.slug)}")
        if r.returncode == 0:
            print("Archive run started in the background.")
            print(f"Watch it with:  folder-archiver.py log {args.slug}")
            return 0
    return run_job(args.slug, manual=True)


def cmd_pause(args):
    with config_lock():
        cfg = load_config()
        job = cfg["jobs"].get(args.slug)
        if job is None:
            die(f"unknown job: {args.slug}")
        bootout_job(args.slug)
        try:
            # launchd re-loads every plist in ~/Library/LaunchAgents at login,
            # so pausing must remove the file, not just unload it.
            os.remove(plist_path(args.slug))
        except OSError:
            pass
        job["enabled"] = False
        save_config(cfg)
    print(f"Paused '{job['label']}' — no more scheduled runs. Existing zips are untouched.")
    print(f"Resume with:  folder-archiver.py resume {args.slug}")
    return 0


def cmd_resume(args):
    self_sync()
    with config_lock():
        cfg = load_config()
        job = cfg["jobs"].get(args.slug)
        if job is None:
            die(f"unknown job: {args.slug}")
        python_path = pick_python()
        cfg["python_path"] = python_path
        job["enabled"] = True
        save_config(cfg)
        write_job_plist(args.slug, job, python_path)
        bootstrap_job(args.slug)
    print(f"Resumed '{job['label']}' — {schedule_human(job['schedule'])}.")
    return 0


def cmd_remove(args):
    with config_lock():
        cfg = load_config()
        job = cfg["jobs"].get(args.slug)
        if job is None:
            die(f"unknown job: {args.slug}")
        if not args.yes:
            sys.stdout.write(f"Delete job '{job['label']}'? Existing zip archives will NOT be deleted. [y/N] ")
            sys.stdout.flush()
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 1
        bootout_job(args.slug)
        for p in (plist_path(args.slug), state_path(args.slug), lock_path(args.slug)):
            try:
                os.remove(p)
            except OSError:
                pass
        del cfg["jobs"][args.slug]
        save_config(cfg)
    print(f"Removed job '{job['label']}'. Archives in {job['dest']} were left untouched.")
    return 0


def cmd_log(args):
    p = log_path(args.slug)
    if not os.path.exists(p):
        print(f"(no log yet: {p})")
        return 0
    with open(p, "r") as f:
        lines = f.readlines()
    sys.stdout.write("".join(lines[-args.n:]))
    return 0


def cmd_install(args):
    changed = self_sync()
    print(f"{'Installed' if changed else 'Already up to date'}: {INSTALLED_PY}")
    return 0


def cmd_doctor(args):
    ensure_dirs()
    problems = []

    def report(ok, msg):
        print(f"  {'ok' if ok else '!!'}  {msg}")
        if not ok:
            problems.append(msg)

    print("Install")
    me = os.path.realpath(os.path.abspath(__file__))
    installed = os.path.exists(INSTALLED_PY)
    report(installed, f"installed copy present: {INSTALLED_PY}")
    if installed and me != os.path.realpath(INSTALLED_PY):
        report(sha256_file(me) == sha256_file(INSTALLED_PY),
               "installed copy matches this script (fix: install)")
    cfg = load_config()
    py = cfg.get("python_path", pick_python())
    report(os.access(py, os.X_OK), f"interpreter executable: {py}")

    for slug in sorted(cfg["jobs"]):
        j = cfg["jobs"][slug]
        print(f"\nJob '{j['label']}' ({slug})")
        report(os.path.isdir(j["source"]), f"source present: {j['source']}")
        report(os.path.isdir(j["dest"]), f"destination present: {j['dest']}")
        report(not path_inside(j["dest"], j["source"]), "destination is outside the source")
        enabled = j.get("enabled", True)
        if enabled:
            report(os.path.exists(plist_path(slug)), f"plist present: {plist_path(slug)}")
            report(is_loaded(slug), "launchd agent loaded")
        else:
            report(not os.path.exists(plist_path(slug)), "paused: no plist lingering")
            report(not is_loaded(slug), "paused: not loaded in launchd")
        # permission-shaped errors in the recent log
        try:
            with open(log_path(slug), "r") as f:
                tail = f.readlines()[-50:]
            perm = [ln for ln in tail if "Operation not permitted" in ln or "PermissionError" in ln
                    or "blocked access" in ln]
            report(not perm, "no permission errors in recent log"
                   + ("" if not perm else " — grant Full Disk Access to " + py + " (System Settings > Privacy & Security)"))
        except OSError:
            pass

    if args.fix:
        print("\nApplying fixes…")
        self_sync()
        python_path = pick_python()
        with config_lock():
            cfg = load_config()
            cfg["python_path"] = python_path
            save_config(cfg)
            for slug in sorted(cfg["jobs"]):
                j = cfg["jobs"][slug]
                try:
                    os.makedirs(j["dest"], exist_ok=True)
                except OSError:
                    pass
                if j.get("enabled", True):
                    write_job_plist(slug, j, python_path)
                    bootstrap_job(slug)
                    print(f"  reloaded {slug}")
                else:
                    bootout_job(slug)
                    try:
                        os.remove(plist_path(slug))
                    except OSError:
                        pass
        print("Done — run doctor again to verify.")
        return 0

    print(f"\n{len(problems)} problem(s) found." if problems else "\nAll good.")
    return 1 if problems else 0


# ---------- argparse ----------

def build_parser():
    p = argparse.ArgumentParser(
        prog="folder-archiver.py",
        description="Zip a folder on a recurring schedule (launchd) and keep only the newest N zips.",
        epilog="example: folder-archiver.py add --source ~/Projects/MyShow --dest ~/Archives --every 1h --keep 10")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="create a job and start its schedule")
    a.add_argument("--source", required=True, help="folder to archive")
    a.add_argument("--dest", required=True, help="folder the zips are written into (must be outside source)")
    a.add_argument("--label", help="display name + zip filename prefix (default: source folder name)")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--every", choices=["15m", "30m", "1h", "2h", "4h"], help="recurring interval preset")
    g.add_argument("--daily", metavar="HH:MM", help="once a day at this time (24-hour)")
    g.add_argument("--weekly", nargs=2, metavar=("DAY", "HH:MM"),
                   help="once a week, e.g. --weekly sun 03:00 (day name or 0-6, 0 = Sunday)")
    g.add_argument("--monthly", nargs=2, metavar=("DAY", "HH:MM"),
                   help="once a month on day 1-31, e.g. --monthly 1 03:00 "
                        "(months without that day are skipped)")
    a.add_argument("--keep", type=int, default=10, help="how many newest zips to keep (default 10)")
    a.add_argument("--archive-unchanged", action="store_true",
                   help="archive every time, even when nothing in the folder changed")
    a.add_argument("--slug", help="explicit job id (default: derived from label)")
    a.add_argument("--tsv", action="store_true", help="print only the new job's slug (for scripting)")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list all jobs")
    l.add_argument("--tsv", action="store_true", help="machine-readable, tab-separated")
    l.add_argument("--json", action="store_true", help="machine-readable JSON array (used by the GUI)")
    l.add_argument("--check-launchd", action="store_true",
                   help="with --json, include each job's launchd 'loaded' state (one launchctl call per job)")
    l.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="detailed status for one or all jobs")
    st.add_argument("slug", nargs="?")
    st.add_argument("--tsv", action="store_true")
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="perform one archive run (what launchd invokes)")
    r.add_argument("slug")
    r.set_defaults(func=cmd_run)

    rn = sub.add_parser("run-now", help="trigger a run immediately")
    rn.add_argument("slug")
    rn.set_defaults(func=cmd_run_now)

    pa = sub.add_parser("pause", help="stop scheduled runs (job and archives are kept)")
    pa.add_argument("slug")
    pa.set_defaults(func=cmd_pause)

    re_ = sub.add_parser("resume", help="restart scheduled runs")
    re_.add_argument("slug")
    re_.set_defaults(func=cmd_resume)

    rm = sub.add_parser("remove", help="delete a job (existing archives are never deleted)")
    rm.add_argument("slug")
    rm.add_argument("--yes", action="store_true", help="skip confirmation")
    rm.set_defaults(func=cmd_remove)

    lg = sub.add_parser("log", help="show the tail of a job's log")
    lg.add_argument("slug")
    lg.add_argument("-n", type=int, default=40, help="lines to show (default 40)")
    lg.set_defaults(func=cmd_log)

    dr = sub.add_parser("doctor", help="check config, plists, launchd and permissions")
    dr.add_argument("--fix", action="store_true", help="repair install, plists and launchd state")
    dr.set_defaults(func=cmd_doctor)

    ins = sub.add_parser("install", help="copy this script to the stable launchd location")
    ins.set_defaults(func=cmd_install)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
