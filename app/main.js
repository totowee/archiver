// Folder Archiver — Electron main process.
//
// The Python engine (folder-archiver.py) is the single source of truth: this
// process only spawns it and relays results to the renderer. Scheduling stays
// 100% launchd, so quitting this app never stops jobs from running.

const { app, BrowserWindow, ipcMain, dialog, shell, nativeTheme } = require("electron");
const { execFile } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// ---- engine resolution (computed once) ----------------------------------

// Always the BUNDLED/repo engine copy — never the installed copy in
// Application Support — so `add`/`resume` keep re-syncing it via self_sync().
function enginePath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "folder-archiver.py")
    : path.join(__dirname, "..", "folder-archiver.py");
}

// Prefer the CLT shim (survives python.org upgrades, same as the engine's own
// pick_python); fall back to whatever `python3` is on PATH.
function pythonPath() {
  const clt = "/usr/bin/python3";
  try {
    fs.accessSync(clt, fs.constants.X_OK);
    return clt;
  } catch (_) {
    return "python3";
  }
}

// Run the engine with the given args. Resolves to an {ok, ...} envelope;
// never rejects, so every handler can trust the shape.
function runEngine(engineArgs, { timeout = 30000 } = {}) {
  return new Promise((resolve) => {
    execFile(
      pythonPath(),
      [enginePath(), ...engineArgs],
      { timeout, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          const msg =
            (stderr && stderr.trim()) ||
            (stdout && stdout.trim()) ||
            (err.killed ? "engine timed out" : `engine exited (${err.code})`);
          resolve({ ok: false, error: msg });
        } else {
          resolve({ ok: true, stdout: stdout });
        }
      }
    );
  });
}

// ---- input validation (defense in depth; args go via argv, not a shell) ---

const SLUG_RE = /^[a-z0-9][a-z0-9-]*$/;
const EVERY_VALUES = new Set(["15m", "30m", "1h", "2h", "4h"]);
const DAILY_RE = /^\d{1,2}:\d{2}$/;

function badSlug(slug) {
  return typeof slug !== "string" || !SLUG_RE.test(slug);
}

// ---- window --------------------------------------------------------------

let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 920,
    height: 640,
    minWidth: 760,
    minHeight: 480,
    show: false,
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#1e1e1e" : "#f5f5f5",
    title: "Folder Archiver",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  // ready-to-show is the preferred signal; did-finish-load is a fallback so the
  // window can never get stuck hidden if ready-to-show doesn't fire.
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.once("did-finish-load", () => {
    if (mainWindow && !mainWindow.isVisible()) mainWindow.show();
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// Single instance: focus the existing window instead of opening a second one.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  // This is a single-window utility: closing the window quits the app.
  // Jobs keep running regardless — they live in launchd, not here.
  app.on("window-all-closed", () => app.quit());
}

// ---- IPC handlers --------------------------------------------------------

ipcMain.handle("jobs:list", async () => {
  const r = await runEngine(["list", "--json"]);
  if (!r.ok) return r;
  try {
    return { ok: true, jobs: JSON.parse(r.stdout) };
  } catch (e) {
    return { ok: false, error: "could not parse engine output: " + e.message };
  }
});

ipcMain.handle("jobs:add", async (_evt, p) => {
  p = p || {};
  const { source, dest, every, daily, weekly, monthly, keep, archiveUnchanged, label, excludes } = p;
  if (typeof source !== "string" || !source) return { ok: false, error: "missing source folder" };
  if (typeof dest !== "string" || !dest) return { ok: false, error: "missing destination folder" };
  const args = ["add", "--source", source, "--dest", dest];
  if (label != null) {
    if (typeof label !== "string" || !label.trim()) return { ok: false, error: "invalid archive name" };
    // --label=… form so a name starting with "-" can't be read as a flag
    args.push("--label=" + label.trim());
  }
  if (excludes != null) {
    if (!Array.isArray(excludes)) return { ok: false, error: "invalid exclude patterns" };
    for (const pat of excludes) {
      if (typeof pat !== "string" || !pat.trim() || pat.length > 200) {
        return { ok: false, error: "invalid exclude pattern" };
      }
      args.push("--exclude=" + pat.trim()); // = form is dash-safe
    }
  }
  if (every) {
    if (!EVERY_VALUES.has(every)) return { ok: false, error: "invalid interval preset" };
    args.push("--every", every);
  } else if (daily) {
    if (!DAILY_RE.test(daily)) return { ok: false, error: "time must be HH:MM (24-hour)" };
    args.push("--daily", daily);
  } else if (weekly) {
    if (!/^[0-6]$/.test(String(weekly.day))) return { ok: false, error: "invalid weekday" };
    if (!DAILY_RE.test(weekly.time)) return { ok: false, error: "time must be HH:MM (24-hour)" };
    args.push("--weekly", String(weekly.day), weekly.time);
  } else if (monthly) {
    const d = parseInt(monthly.day, 10);
    if (!Number.isInteger(d) || d < 1 || d > 31) return { ok: false, error: "invalid day of month" };
    if (!DAILY_RE.test(monthly.time)) return { ok: false, error: "time must be HH:MM (24-hour)" };
    args.push("--monthly", String(d), monthly.time);
  } else {
    return { ok: false, error: "no schedule chosen" };
  }
  const keepN = parseInt(keep, 10);
  if (!Number.isInteger(keepN) || keepN < 1) return { ok: false, error: "keep must be a whole number ≥ 1" };
  args.push("--keep", String(keepN));
  if (archiveUnchanged) args.push("--archive-unchanged");
  args.push("--tsv");
  const r = await runEngine(args);
  if (!r.ok) return r;
  return { ok: true, slug: r.stdout.trim() };
});

ipcMain.handle("jobs:runNow", async (_evt, slug) => {
  if (badSlug(slug)) return { ok: false, error: "invalid job id" };
  // A paused job runs inline and can take minutes — no timeout here.
  const r = await runEngine(["run-now", slug], { timeout: 0 });
  if (!r.ok) return r;
  return { ok: true, output: r.stdout.trim() || "Archive run finished." };
});

ipcMain.handle("jobs:pause", async (_evt, slug) => {
  if (badSlug(slug)) return { ok: false, error: "invalid job id" };
  const r = await runEngine(["pause", slug]);
  if (!r.ok) return r;
  return { ok: true, output: r.stdout.trim() };
});

ipcMain.handle("jobs:resume", async (_evt, slug) => {
  if (badSlug(slug)) return { ok: false, error: "invalid job id" };
  const r = await runEngine(["resume", slug]);
  if (!r.ok) return r;
  return { ok: true, output: r.stdout.trim() };
});

ipcMain.handle("jobs:remove", async (_evt, slug, label) => {
  if (badSlug(slug)) return { ok: false, error: "invalid job id" };
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "warning",
    buttons: ["Cancel", "Delete Job"],
    defaultId: 0,
    cancelId: 0,
    message: `Delete the archive job “${label || slug}”?`,
    detail: "Existing zip archives will NOT be deleted — only the scheduled job is removed.",
  });
  if (response !== 1) return { ok: true, cancelled: true };
  const r = await runEngine(["remove", slug, "--yes"]);
  if (!r.ok) return r;
  return { ok: true, output: r.stdout.trim() };
});

ipcMain.handle("jobs:log", async (_evt, slug, lines) => {
  if (badSlug(slug)) return { ok: false, error: "invalid job id" };
  const n = Number.isInteger(lines) && lines > 0 ? lines : 200;
  const r = await runEngine(["log", slug, "-n", String(n)]);
  if (!r.ok) return r;
  return { ok: true, text: r.stdout };
});

ipcMain.handle("ui:chooseFolder", async (_evt, opts) => {
  opts = opts || {};
  const options = {
    title: opts.title || "Choose a folder",
    properties: ["openDirectory", "createDirectory"],
  };
  if (opts.defaultPath) options.defaultPath = opts.defaultPath;
  const { canceled, filePaths } = await dialog.showOpenDialog(mainWindow, options);
  if (canceled || !filePaths.length) return { ok: true, cancelled: true };
  return { ok: true, path: filePaths[0] };
});

ipcMain.handle("ui:revealArchives", async (_evt, dest, zip) => {
  if (typeof dest !== "string" || !dest) return { ok: false, error: "no destination" };
  try {
    if (zip) {
      const zipPath = path.join(dest, zip);
      if (fs.existsSync(zipPath)) {
        shell.showItemInFolder(zipPath); // opens dest and selects the newest zip
        return { ok: true };
      }
    }
    if (fs.existsSync(dest)) {
      const err = await shell.openPath(dest);
      return err ? { ok: false, error: err } : { ok: true };
    }
    return { ok: false, error: "the archives folder does not exist yet" };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle("ui:statPath", async (_evt, p) => {
  if (typeof p !== "string" || !p) return { ok: false, error: "no path" };
  try {
    return { ok: true, isDir: fs.statSync(p).isDirectory() };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle("ui:openPrivacySettings", async () => {
  // Deep-link straight to the Full Disk Access pane.
  try {
    await shell.openExternal("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles");
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle("ui:openLogFolder", async () => {
  const logDir = path.join(os.homedir(), "Library", "Logs", "FolderArchiver");
  try {
    fs.mkdirSync(logDir, { recursive: true });
    const err = await shell.openPath(logDir);
    return err ? { ok: false, error: err } : { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});
