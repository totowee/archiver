// Bridges a small, explicit API into the renderer. Everything crosses via
// ipcRenderer.invoke → the handlers in main.js; nothing else is exposed.

const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("archiver", {
  listJobs: () => ipcRenderer.invoke("jobs:list"),
  statPath: (p) => ipcRenderer.invoke("ui:statPath", p),
  // Resolves a DataTransfer File (Finder drag-and-drop) to its absolute path.
  pathForFile: (file) => webUtils.getPathForFile(file),
  addJob: (payload) => ipcRenderer.invoke("jobs:add", payload),
  runNow: (slug) => ipcRenderer.invoke("jobs:runNow", slug),
  pauseJob: (slug) => ipcRenderer.invoke("jobs:pause", slug),
  resumeJob: (slug) => ipcRenderer.invoke("jobs:resume", slug),
  removeJob: (slug, label) => ipcRenderer.invoke("jobs:remove", slug, label),
  logTail: (slug, lines) => ipcRenderer.invoke("jobs:log", slug, lines),
  chooseFolder: (opts) => ipcRenderer.invoke("ui:chooseFolder", opts),
  revealArchives: (dest, zip) => ipcRenderer.invoke("ui:revealArchives", dest, zip),
  openLogFolder: () => ipcRenderer.invoke("ui:openLogFolder"),
  openPrivacySettings: () => ipcRenderer.invoke("ui:openPrivacySettings"),
});
