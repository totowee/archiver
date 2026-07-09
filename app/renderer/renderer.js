// Renderer: table + detail + modals + 5s polling. All engine access goes
// through window.archiver (see preload.js). No direct Node access here.

const api = window.archiver;

const state = {
  jobs: [],
  selectedSlug: null,
  busy: false,
  destTouched: false, // did the user edit the dest path manually?
  labelTouched: false, // did the user edit the archive name manually?
  logSlug: null,
  nj: { source: null, destBase: null }, // New Job picker zones
};

// Icon for the picker zones (inline so everything stays local).
const FOLDER_SVG =
  '<svg class="zone-icon" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">' +
  '<path d="M5 13c0-2.2 1.8-4 4-4h10.5l4 5H39c2.2 0 4 1.8 4 4v17c0 2.2-1.8 4-4 4H9c-2.2 0-4-1.8-4-4V13z" ' +
  'fill="var(--accent)" opacity="0.75"/></svg>';

// ---- helpers -------------------------------------------------------------

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function $(id) {
  return document.getElementById(id);
}

let toastTimer = null;
function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 3500);
}

function showBanner(html, isError) {
  const el = $("banner");
  el.innerHTML = html;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
}
function hideBanner() {
  $("banner").classList.add("hidden");
}

function lastRunClass(job) {
  const res = job.last_run && job.last_run.result;
  if (!res) return "";
  if (res.indexOf("error") === 0) return "run-error";
  if (res.indexOf("skipped-no-source") === 0) return "run-warn";
  return "";
}

function lastRunText(job) {
  if (!job.last_run) return "never run";
  return job.last_run.summary || job.last_run.result || "—";
}

function parentOf(p) {
  const s = p.replace(/\/+$/, "");
  const i = s.lastIndexOf("/");
  return i > 0 ? s.slice(0, i) : "/";
}
function folderName(p) {
  const s = p.replace(/\/+$/, "");
  return s.slice(s.lastIndexOf("/") + 1) || s;
}
function joinPath(a, b) {
  return a.replace(/\/+$/, "") + "/" + b;
}
function todayStr() {
  const d = new Date();
  return (
    d.getFullYear() +
    "-" +
    String(d.getMonth() + 1).padStart(2, "0") +
    "-" +
    String(d.getDate()).padStart(2, "0")
  );
}
function timeStr() {
  const d = new Date();
  return (
    String(d.getHours()).padStart(2, "0") +
    "." +
    String(d.getMinutes()).padStart(2, "0") +
    "." +
    String(d.getSeconds()).padStart(2, "0")
  );
}
function ord(n) {
  const suf = n % 100 >= 10 && n % 100 <= 20 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th";
  return n + suf;
}

// ---- data / polling ------------------------------------------------------

async function refresh() {
  try {
    const r = await api.listJobs();
    if (!r.ok) {
      showBanner("⚠️ Could not read jobs: " + esc(r.error), true);
      return;
    }
    hideBanner();
    state.jobs = r.jobs;
    // Drop a selection whose job vanished.
    if (state.selectedSlug && !state.jobs.some((j) => j.slug === state.selectedSlug)) {
      state.selectedSlug = null;
    }
    render();
    const now = new Date();
    $("refresh-note").textContent = "updated " + now.toLocaleTimeString();
  } finally {
    schedulePoll();
  }
}

// Poll fast while an archive is being written so the progress bar is live;
// settle back to a lazy poll when idle.
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, state.jobs.some((j) => j.running) ? 1000 : 5000);
}

// ---- render --------------------------------------------------------------

function render() {
  const hasJobs = state.jobs.length > 0;
  $("table-wrap").classList.toggle("hidden", !hasJobs);
  $("empty-state").classList.toggle("hidden", hasJobs);
  renderTable();
  renderDetail();
}

// Live progress while a run is writing the zip. pct comes from the engine
// (part-file size vs estimated final size); no pct yet → indeterminate bar.
function progressHTML(j) {
  const p = j.progress;
  if (p && p.phase === "zipping" && p.pct != null) {
    return (
      `<div class="progress-wrap"><div class="progress-track">` +
      `<div class="progress-fill" style="width:${Math.max(2, p.pct)}%"></div></div>` +
      `<span class="progress-label">${p.pct}%</span></div>`
    );
  }
  return (
    `<div class="progress-wrap"><div class="progress-track indet"><div class="progress-fill"></div></div>` +
    `<span class="progress-label">Preparing…</span></div>`
  );
}

function renderTable() {
  const body = $("jobs-body");
  body.innerHTML = state.jobs
    .map((j) => {
      const sel = j.slug === state.selectedSlug ? " selected" : "";
      const badge = j.running
        ? '<span class="badge badge-running">Running</span>'
        : j.status === "active"
          ? '<span class="badge badge-active">Active</span>'
          : '<span class="badge badge-paused">Paused</span>';
      const lastCell = j.running ? progressHTML(j) : esc(lastRunText(j));
      return (
        `<tr class="job-row${sel}" data-slug="${esc(j.slug)}">` +
        `<td class="cell-label">${esc(j.label)}</td>` +
        `<td>${esc(j.schedule_human)}</td>` +
        `<td class="col-keep">${esc(j.keep)}</td>` +
        `<td>${badge}</td>` +
        `<td class="cell-lastrun ${lastRunClass(j)}">${lastCell}</td>` +
        `</tr>`
      );
    })
    .join("");
  body.querySelectorAll(".job-row").forEach((tr) => {
    tr.addEventListener("click", () => {
      state.selectedSlug = tr.getAttribute("data-slug");
      render();
    });
  });
}

function renderDetail() {
  const pane = $("detail");
  const job = state.jobs.find((j) => j.slug === state.selectedSlug);
  if (!job) {
    pane.classList.add("hidden");
    pane.innerHTML = "";
    return;
  }
  pane.classList.remove("hidden");

  const srcWarn = job.source_available ? "" : ' <span class="warn">(not available)</span>';
  const lr = job.last_run;
  const lrDetail = lr
    ? esc(lr.summary) + (lr.duration_s != null ? ` · ${esc(lr.duration_s)}s` : "")
    : "never run";

  const pauseResume =
    job.status === "active"
      ? '<button class="btn" data-act="pause">Pause</button>'
      : '<button class="btn" data-act="resume">Resume</button>';

  // macOS blocked the background python3 from this job's folders: show the
  // fix inline instead of leaving just a red error in the table.
  const permWarning =
    lr && lr.result === "error: no folder access"
      ? `<div class="detail-warning">macOS is blocking this job's folders. In <b>System Settings →
         Privacy &amp; Security → Full Disk Access</b>, click <b>+</b> and add
         <code>/usr/bin/python3</code> (⇧⌘G to type the path), then Run Now again.
         <button class="btn" data-act="privacy">Open Privacy Settings</button></div>`
      : "";

  pane.innerHTML =
    `<h2>${esc(job.label)}</h2>` +
    `<div class="detail-slug">${esc(job.slug)}</div>` +
    `<div class="detail-grid">` +
    `<div class="k">Source</div><div class="v path">${esc(job.source)}${srcWarn}</div>` +
    `<div class="k">Archives</div><div class="v path">${esc(job.dest)}</div>` +
    `<div class="k">Saved as</div><div class="v path">YYYY-MM-DD HH.MM.SS ${esc(job.label)}.zip</div>` +
    `<div class="k">Schedule</div><div class="v">${esc(job.schedule_human)}</div>` +
    `<div class="k">Keep newest</div><div class="v">${esc(job.keep)} zips</div>` +
    `<div class="k">Skip unchanged</div><div class="v">${job.skip_if_unchanged ? "yes" : "no"}</div>` +
    `<div class="k">Created</div><div class="v">${esc(job.created || "—")}</div>` +
    `<div class="k">Last run</div><div class="v ${lastRunClass(job)}">${lrDetail}</div>` +
    (job.running ? `<div class="k">Progress</div><div class="v">${progressHTML(job)}</div>` : "") +
    `</div>` +
    permWarning +
    `<div class="detail-actions">` +
    `<button class="btn btn-primary" data-act="run"${job.running ? " disabled" : ""}>${
      job.running ? "Running…" : "Run Now"
    }</button>` +
    pauseResume +
    `<button class="btn" data-act="log">View Log</button>` +
    `<button class="btn" data-act="reveal">Reveal in Finder</button>` +
    `<button class="btn btn-danger" data-act="delete">Delete Job…</button>` +
    `</div>`;

  pane.querySelectorAll("[data-act]").forEach((btn) => {
    const act = btn.getAttribute("data-act");
    btn.disabled =
      (state.busy && act !== "log" && act !== "reveal" && act !== "privacy") ||
      (act === "run" && job.running);
    btn.addEventListener("click", () => onDetailAction(act, job));
  });
}

// ---- detail actions ------------------------------------------------------

async function onDetailAction(act, job) {
  if (state.busy && act !== "log" && act !== "reveal" && act !== "privacy") return;

  if (act === "log") return openLog(job);
  if (act === "privacy") {
    const r = await api.openPrivacySettings();
    if (!r.ok) toast(r.error, true);
    return;
  }
  if (act === "reveal") {
    const r = await api.revealArchives(job.dest, job.last_run && job.last_run.zip);
    if (!r.ok) toast(r.error, true);
    return;
  }

  setBusy(true);
  try {
    let r;
    if (act === "run") {
      if (job.running) return;
      toast("Running…");
      r = await api.runNow(job.slug);
    } else if (act === "pause") {
      r = await api.pauseJob(job.slug);
    } else if (act === "resume") {
      r = await api.resumeJob(job.slug);
    } else if (act === "delete") {
      r = await api.removeJob(job.slug, job.label);
      if (r.ok && r.cancelled) return; // user backed out of the confirm dialog
    }
    if (!r.ok) {
      toast(r.error, true);
    } else if (r.output) {
      toast(r.output);
    }
  } finally {
    setBusy(false);
    await refresh();
  }
}

function setBusy(v) {
  state.busy = v;
  document.querySelectorAll("#detail [data-act]").forEach((b) => {
    const act = b.getAttribute("data-act");
    if (act !== "log" && act !== "reveal" && act !== "privacy") {
      b.disabled = v;
    }
  });
}

// ---- New Job modal -------------------------------------------------------

function openNew(prefillSource) {
  hideBanner();
  state.nj = { source: null, destBase: null };
  state.destTouched = false;
  state.labelTouched = false;
  $("in-label").value = "";
  $("in-dest").value = "";
  $("in-schedule").value = "1h";
  $("in-time").value = "03:00";
  $("in-weekday").value = "1";
  $("in-monthday").value = "1";
  $("in-keep").value = "10";
  $("in-unchanged").checked = false;
  updateScheduleFields();
  $("new-error").classList.add("hidden");
  renderZones();
  updateNamePreview();
  $("modal-new").classList.remove("hidden");
  if (prefillSource) setSource(prefillSource);
}
function closeNew() {
  $("modal-new").classList.add("hidden");
}
function newError(msg) {
  const el = $("new-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

// -- picker zones --

function zoneHTML(p, kind) {
  if (!p) {
    const what = kind === "source" ? "folder to archive" : "folder or disk";
    return (
      `<button class="zone-add" data-zone-add="${kind}" title="Choose…">+</button>` +
      `<div class="zone-hint">Drag a ${what} here<br>or click +</div>`
    );
  }
  return (
    `<button class="zone-clear" data-zone-clear="${kind}" title="Remove">×</button>` +
    FOLDER_SVG +
    `<div class="zone-name">${esc(folderName(p))}</div>` +
    `<div class="zone-path">${esc(p)}</div>`
  );
}

function renderZones() {
  $("zone-source").innerHTML = zoneHTML(state.nj.source, "source");
  $("zone-dest").innerHTML = zoneHTML(state.nj.destBase, "dest");
  document.querySelectorAll("[data-zone-add]").forEach((b) =>
    b.addEventListener("click", () => zonePick(b.getAttribute("data-zone-add")))
  );
  document.querySelectorAll("[data-zone-clear]").forEach((b) =>
    b.addEventListener("click", () => zoneClear(b.getAttribute("data-zone-clear")))
  );
}

async function zonePick(kind) {
  const r = await api.chooseFolder(
    kind === "source"
      ? { title: "Choose the folder to archive" }
      : {
          title: "Choose where to save the zip archives",
          defaultPath: state.nj.source ? parentOf(state.nj.source) : undefined,
        }
  );
  if (!r.ok || r.cancelled) return;
  if (kind === "source") setSource(r.path);
  else setDestBase(r.path);
}

function zoneClear(kind) {
  if (kind === "source") state.nj.source = null;
  else {
    state.nj.destBase = null;
    state.destTouched = false;
  }
  updateDestField();
  renderZones();
}

function setSource(p) {
  state.nj.source = p;
  if (!state.labelTouched) $("in-label").value = folderName(p);
  updateDestField();
  updateNamePreview();
  renderZones();
}
function setDestBase(p) {
  state.nj.destBase = p;
  state.destTouched = false; // an explicit drop/pick overrides manual edits
  updateDestField();
  renderZones();
}

// The archive name (zip filename prefix): the editable field, falling back to
// the source folder's name.
function effectiveLabel() {
  return $("in-label").value.trim() || (state.nj.source ? folderName(state.nj.source) : "");
}

function updateNamePreview() {
  const label = effectiveLabel();
  $("name-preview").textContent =
    "Saved as:  " + todayStr() + " " + timeStr() + " " + (label || "…") + ".zip";
}

// The editable path field is the source of truth for where zips go; the zones
// just fill it in. Dest base chosen → "<Name> Archives" inside it; otherwise
// the classic sibling suggestion (skipped when the source is a volume root).
function updateDestField() {
  if (state.destTouched) return;
  const { source, destBase } = state.nj;
  const name = effectiveLabel();
  let v = "";
  if (destBase) {
    v = name ? joinPath(destBase, name + " Archives") : destBase;
  } else if (source) {
    const parent = parentOf(source);
    if (parent !== "/" && parent !== "/Volumes") {
      v = joinPath(parent, name + " Archives");
    }
  }
  $("in-dest").value = v;
}

// -- drag and drop --

// Resolve a drop to an absolute folder path: either one of our disk tiles
// (custom mime type) or a real folder dragged in from Finder.
async function droppedPath(e) {
  const custom = e.dataTransfer.getData("application/x-fa-path");
  if (custom) return custom;
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return null;
  const p = api.pathForFile(f);
  if (!p) return null;
  const st = await api.statPath(p);
  if (!st.ok || !st.isDir) {
    newError("Drop a folder, not a file.");
    return null;
  }
  return p;
}

[["zone-source", "source"], ["zone-dest", "dest"]].forEach(([id, kind]) => {
  const el = $(id);
  el.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    el.classList.add("drag-over");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
  el.addEventListener("drop", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    el.classList.remove("drag-over");
    const p = await droppedPath(e);
    if (!p) return;
    if (kind === "source") setSource(p);
    else setDestBase(p);
  });
});

// Window-level: never let a stray drop navigate the page; a folder dropped
// on the main view opens the New Job form with it as the source.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", async (e) => {
  e.preventDefault();
  if (!$("modal-new").classList.contains("hidden")) return; // zones handle it
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const p = api.pathForFile(f);
  if (!p) return;
  const st = await api.statPath(p);
  if (st.ok && st.isDir) openNew(p);
});

async function createJob() {
  const source = state.nj.source;
  const dest = $("in-dest").value.trim();
  const sched = $("in-schedule").value;
  const keep = parseInt($("in-keep").value, 10);

  if (!source) return newError("Choose a folder to archive.");
  if (!dest) return newError("Choose where to save the archives.");
  // Cheap client-side guard; the engine re-validates authoritatively.
  const s = source.replace(/\/+$/, "");
  const d = dest.replace(/\/+$/, "");
  if (d === s || d.indexOf(s + "/") === 0) {
    return newError("The archives folder must be outside the source folder.");
  }
  if (!Number.isInteger(keep) || keep < 1) return newError("Keep newest must be a whole number, 1 or more.");

  const payload = { source, dest, keep, archiveUnchanged: $("in-unchanged").checked };
  const label = $("in-label").value.trim();
  if (label) payload.label = label; // engine defaults to the folder name
  if (sched === "daily" || sched === "weekly" || sched === "monthly") {
    const t = $("in-time").value;
    if (!/^\d{1,2}:\d{2}$/.test(t)) return newError("Enter a valid time.");
    if (sched === "daily") payload.daily = t;
    else if (sched === "weekly") payload.weekly = { day: $("in-weekday").value, time: t };
    else payload.monthly = { day: parseInt($("in-monthday").value, 10), time: t };
  } else {
    payload.every = sched;
  }

  $("btn-new-create").disabled = true;
  const r = await api.addJob(payload);
  $("btn-new-create").disabled = false;
  if (!r.ok) return newError(r.error);

  closeNew();
  await refresh();
  state.selectedSlug = r.slug;
  render();
  offerFirstRun(r.slug);
}

// After creating a job, offer to run the first archive (mirrors the old flow).
function offerFirstRun(slug) {
  showBanner(
    "Job created. Run the first archive now? " +
      "<em>(macOS may ask for folder access — click Allow.)</em>" +
      '<span class="banner-actions">' +
      '<button class="btn btn-primary" id="banner-run">Run Now</button>' +
      '<button class="btn btn-secondary" id="banner-later">Later</button>' +
      "</span>"
  );
  $("banner-run").addEventListener("click", async () => {
    hideBanner();
    setBusy(true);
    toast("Running…");
    const r = await api.runNow(slug);
    setBusy(false);
    if (!r.ok) toast(r.error, true);
    else toast(r.output || "Archive run finished.");
    await refresh();
  });
  $("banner-later").addEventListener("click", hideBanner);
}

// ---- Log viewer modal ----------------------------------------------------

async function openLog(job) {
  state.logSlug = job.slug;
  $("log-title").textContent = job.label + " — log";
  $("log-body").textContent = "Loading…";
  $("modal-log").classList.remove("hidden");
  await loadLog();
}
async function loadLog() {
  if (!state.logSlug) return;
  const r = await api.logTail(state.logSlug, 200);
  const body = $("log-body");
  if (!r.ok) {
    body.textContent = "Could not read log: " + r.error;
    return;
  }
  body.textContent = r.text && r.text.trim() ? r.text : "(no log entries yet)";
  body.scrollTop = body.scrollHeight;
}
function closeLog() {
  state.logSlug = null;
  $("modal-log").classList.add("hidden");
}

// ---- wiring --------------------------------------------------------------

$("btn-new").addEventListener("click", openNew);
$("btn-new-empty").addEventListener("click", openNew);
$("btn-open-logs").addEventListener("click", () => api.openLogFolder());

$("in-dest").addEventListener("input", () => {
  state.destTouched = true;
});
$("in-label").addEventListener("input", () => {
  state.labelTouched = true;
  updateNamePreview();
  updateDestField(); // the "<Name> Archives" suggestion follows the name
});
$("btn-new-cancel").addEventListener("click", closeNew);
$("btn-new-create").addEventListener("click", createJob);
function updateScheduleFields() {
  const v = $("in-schedule").value;
  const calendar = v === "daily" || v === "weekly" || v === "monthly";
  $("daily-time-wrap").style.display = calendar ? "" : "none";
  $("weekday-wrap").style.display = v === "weekly" ? "" : "none";
  $("monthday-wrap").style.display = v === "monthly" ? "" : "none";
}
$("in-schedule").addEventListener("change", updateScheduleFields);

// Days 1-28 only: every month has them, so a monthly job can never silently
// skip a month (launchd doesn't fire on a day the month lacks).
$("in-monthday").innerHTML = Array.from(
  { length: 28 },
  (_, i) => `<option value="${i + 1}">${ord(i + 1)}</option>`
).join("");

$("btn-log-refresh").addEventListener("click", loadLog);
$("btn-log-openfolder").addEventListener("click", () => api.openLogFolder());
$("btn-log-close").addEventListener("click", closeLog);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("modal-log").classList.contains("hidden")) closeLog();
    else if (!$("modal-new").classList.contains("hidden")) closeNew();
  }
});
// Click outside a modal card closes it.
document.querySelectorAll(".modal-overlay").forEach((ov) => {
  ov.addEventListener("click", (e) => {
    if (e.target === ov) ov.classList.add("hidden");
  });
});

// ---- boot ----------------------------------------------------------------

refresh(); // each refresh schedules the next poll (1s while running, 5s idle)
