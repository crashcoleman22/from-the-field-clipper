/* From the Field clipper UI. Vanilla JS, no build step, no external assets. */
"use strict";

const state = {
  config: null,
  view: "overview",
  overview: null,
  episodes: [],
  detail: null,
  brand: null,
  storage: null,
  clips: [],
  selected: new Set(),
  uploads: [],
  uploadBusy: false,
  reviewInputFocused: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const STATUS_LABELS = {
  queued: "Queued",
  processing: "Processing",
  clips_awaiting_review: "Review",
  rendering_previews: "Previews",
  rendering: "Rendering",
  approved: "Approved",
  failed: "Failed",
  new: "New",
};

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtBytes(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const power = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
  return (value / Math.pow(1024, power)).toFixed(power > 1 ? 1 : 0) + " " + units[power];
}

function fmtDate(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
    date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function fmtClock(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  return minutes + ":" + String(total % 60).padStart(2, "0");
}

function statusPill(status) {
  const known = STATUS_LABELS[status] || status || "Unknown";
  const cls = /fail/i.test(status || "") ? "bad" : /approved/.test(status || "") ? "good"
    : /review/.test(status || "") ? "warn" : "neutral";
  return `<span class="pill ${cls}">${esc(known)}</span>`;
}

function slug(value) {
  return String(value || "clip").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 48) || "clip";
}

function toast(message, bad = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast" + (bad ? " bad" : "");
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, bad ? 7000 : 3500);
}

async function api(path, body, method = "GET") {
  if (body !== undefined && method === "GET") method = "POST";
  const options = { method, headers: { "Accept": "application/json" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = state.config ? state.config.csrf : "";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { error: text }; }
  if (response.status === 401 && !path.includes("/api/config")) {
    location.href = "/login";
    throw new Error("Sign in again.");
  }
  if (!response.ok) throw new Error((data && data.error) || ("Request failed: " + response.status));
  return data;
}

let pollTimer = null;

async function loadConfig() {
  state.config = await api("/api/config");
  renderNav();
  renderSystemStrip();
}

function renderSystemStrip() {
  if (!state.overview) return;
  const disk = state.overview.disk || {};
  const chip = $("#disk-chip");
  chip.textContent = fmtBytes(disk.free) + " free";
  chip.className = "chip " + (disk.ok ? "" : "bad");
  chip.title = "Safety reserve: " + fmtBytes(disk.min_free);
  const ai = state.overview.ai || {};
  const aiNode = $("#ai-state");
  aiNode.textContent = ai.enabled ? "AI: " + (ai.model || "ready") : "AI fallback";
  aiNode.className = "pill subtle " + (ai.enabled ? "good" : "");
}

function counts() {
  return (state.overview && state.overview.counts) || {};
}

function renderNav() {
  const sections = (state.config && state.config.sections) || [];
  const c = counts();
  const badges = {
    queue: (c.processing || 0),
    review: (c.awaiting_review || 0),
    approved: (c.approved_clips || 0),
  };
  $("#nav").innerHTML = sections.map((section) => {
    const active = section.id === state.view ? " active" : "";
    const soon = section.available ? "" : '<span class="soon">soon</span>';
    const badge = badges[section.id] ? `<span class="badge">${badges[section.id]}</span>` : "";
    return `<a class="nav-item${active}" href="#/${esc(section.id)}"
      data-section="${esc(section.id)}">${esc(section.label)}${badge}${soon}</a>`;
  }).join("");
}

function titleFor(view) {
  const section = ((state.config && state.config.sections) || []).find((item) => item.id === view);
  return section ? section.label : "From the Field";
}

async function refreshOverview() {
  state.overview = await api("/api/overview");
  state.episodes = state.overview.episodes || [];
  renderNav();
  renderSystemStrip();
}

function render() {
  $("#page-title").textContent = titleFor(state.view);
  renderNav();
  const view = $("#view");
  const renderers = {
    overview: renderOverview,
    upload: renderUpload,
    queue: renderQueue,
    review: renderReviewIndex,
    approved: renderApproved,
    calendar: () => renderComingSoon("Content Calendar",
      "Plan approved clips on a calendar when scheduling and publishing are connected."),
    published: () => renderComingSoon("Published Content",
      "This section will show posts after Facebook, Instagram, and YouTube are connected."),
    analytics: () => renderComingSoon("Analytics",
      "Performance analytics arrive after publishing is connected. Nothing is published today."),
    brand: renderBrand,
    storage: renderStorage,
  };
  (renderers[state.view] || renderOverview)(view).catch((error) => {
    view.innerHTML = `<div class="panel error-panel"><h2>Could not load this section</h2>
      <p>${esc(error.message)}</p><button class="button" onclick="location.reload()">Reload</button></div>`;
  });
}

/* ---------------------------------------------------------------- overview */
async function renderOverview(root) {
  await refreshOverview();
  const c = counts();
  const disk = state.overview.disk || {};
  const ai = state.overview.ai || {};
  const queue = state.overview.queue || {};
  root.innerHTML = `
    <section class="hero panel">
      <div>
        <span class="eyebrow">BROWSER UPLOAD → VERTICAL CLIPS</span>
        <h2>Turn full episodes into grower-ready clips.</h2>
        <p>Upload MP4 episodes, let Parakeet transcribe them on this workspace, review
        AI-assisted clip picks, and export one captioned 1080×1920 master per approved clip.</p>
      </div>
      <div class="hero-actions">
        <a class="button primary" href="#/upload">Upload episodes</a>
        <a class="button ghost" href="#/review">Review clips ${c.awaiting_review ? `(${c.awaiting_review})` : ""}</a>
      </div>
    </section>
    <section class="stat-grid">
      ${statCard("Episodes", c.episodes || 0, "in this workspace")}
      ${statCard("Processing", c.processing || 0, "one at a time")}
      ${statCard("Awaiting review", c.awaiting_review || 0, "clip decisions")}
      ${statCard("Approved clips", c.approved_clips || 0, "vertical masters")}
      ${statCard("Failed", c.failed || 0, "need attention", c.failed ? "bad" : "")}
      ${statCard("Free disk", fmtBytes(disk.free), disk.ok ? "above reserve" : "below reserve", disk.ok ? "" : "bad")}
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Workflow</h2><span class="muted">Publishing and analytics are not connected yet.</span></div>
      <ol class="pipeline">
        ${(state.overview.pipeline || []).map((stage, index) => `
          <li class="${index >= 8 ? "future" : ""}"><span>${index + 1}</span>${esc(stage)}</li>`).join("")}
      </ol>
    </section>
    <div class="two-col">
      <section class="panel">
        <div class="panel-head"><h2>Recent episodes</h2><a class="button ghost small" href="#/queue">Queue</a></div>
        ${episodeTable((state.overview.episodes || []).slice(0, 6))}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>System</h2></div>
        <dl class="facts">
          <div><dt>Transcription</dt><dd>Parakeet · CPU · resumable chunks</dd></div>
          <div><dt>AI editorial</dt><dd>${ai.enabled ? esc(ai.model) : "Deterministic fallback"}</dd></div>
          <div><dt>Gateway</dt><dd>${esc(ai.gateway || "not configured")}</dd></div>
          <div><dt>Queue</dt><dd>${queue.pending || 0} pending · ${queue.running || 0} running</dd></div>
          <div><dt>Auto-resumed</dt><dd>${state.overview.recovered || 0} episode(s) on start</dd></div>
          <div><dt>Storage</dt><dd>${fmtBytes(disk.used)} used of ${fmtBytes(disk.total)}</dd></div>
        </dl>
        <p class="fine">${esc(ai.fallback || "")}</p>
      </section>
    </div>`;
}

function statCard(label, value, note, tone = "") {
  return `<article class="stat ${tone}"><span>${esc(label)}</span>
    <strong>${esc(value)}</strong><small>${esc(note)}</small></article>`;
}

function episodeTable(list) {
  if (!list.length) return `<p class="empty">No episodes yet. Upload an MP4 to begin.</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>Episode</th><th>Status</th>
    <th>Clips</th><th>Created</th><th></th></tr></thead><tbody>
    ${list.map((episode) => `<tr>
      <td><strong>${esc(episode.title || episode.original_name || episode.id)}</strong>
        <small>${esc(episode.stage || "")}</small></td>
      <td>${statusPill(episode.status)}</td>
      <td>${episode.counts ? `${episode.counts.exported || 0}/${episode.counts.clips || 0}` : "—"}</td>
      <td>${fmtDate(episode.created)}</td>
      <td><a class="link" href="#/review/${esc(episode.id)}">Open</a></td>
    </tr>`).join("")}
  </tbody></table></div>`;
}

/* ------------------------------------------------------------------ upload */
async function renderUpload(root) {
  const data = await api("/api/uploads");
  const incomplete = (data.uploads || []).filter((item) => item.status !== "complete");
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Upload episodes</h2>
        <span class="muted">MP4/MOV/MKV/WebM · one episode processed at a time</span>
      </div>
      <label id="drop" class="drop">
        <input id="file-input" type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.mkv,.m4v" multiple hidden>
        <strong>Drop episode files here</strong>
        <span>or click to choose from this computer</span>
        <small>Files upload in resumable chunks to persistent storage. Originals stay until you delete them.</small>
      </label>
      <div id="upload-list" class="upload-list"></div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Recent uploads</h2>
        <span class="muted">${incomplete.length} incomplete</span></div>
      ${incomplete.length ? `<div class="table-wrap"><table><thead><tr><th>File</th><th>Progress</th>
        <th>Started</th><th></th></tr></thead><tbody>
        ${incomplete.map((item) => `<tr>
          <td>${esc(item.filename)}</td>
          <td>${Math.round(100 * (item.received || 0) / Math.max(1, item.size || 1))}%</td>
          <td>${fmtDate(item.created)}</td>
          <td><button class="button ghost small" data-resume="${esc(item.id)}">Resume</button>
            <button class="button danger small" data-abort="${esc(item.id)}">Discard</button></td>
        </tr>`).join("")}
        </tbody></table></div>` : `<p class="empty">No incomplete uploads.</p>`}
      <p class="fine">Uploads resume by byte offset if the browser reconnects, as long as the
      same file is chosen again from this page.</p>
    </section>`;
  const drop = $("#drop");
  const input = $("#file-input");
  input.addEventListener("change", () => queueFiles(Array.from(input.files || [])));
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault(); drop.classList.remove("over");
  }));
  drop.addEventListener("drop", (event) => {
    const files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
    queueFiles(files.filter((file) => /\.(mp4|mov|mkv|webm|m4v)$/i.test(file.name)));
  });
  $$("[data-abort]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Discard this incomplete upload?")) return;
    try { await api(`/api/uploads/${button.dataset.abort}/abort`, {}); toast("Upload discarded."); render(); }
    catch (error) { toast(error.message, true); }
  }));
  $$("[data-resume]").forEach((button) => button.addEventListener("click", () => {
    const item = incomplete.find((entry) => entry.id === button.dataset.resume);
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".mp4,.mov,.mkv,.webm,.m4v";
    picker.onchange = () => {
      const file = picker.files && picker.files[0];
      if (!file) return;
      if (file.size !== item.size) {
        toast("Choose the exact same file to resume this upload.", true);
        return;
      }
      uploadFile(file, item);
    };
    picker.click();
  }));
}

function queueFiles(files) {
  if (!files.length) return;
  const list = $("#upload-list");
  const accepted = files.filter((file) => file.size > 0);
  state.uploads = accepted.map((file) => ({ file, name: file.name, size: file.size,
    progress: 0, status: "waiting", message: "Waiting" }));
  list.innerHTML = state.uploads.map((item, index) => `
    <article class="upload-item" data-up="${index}">
      <div><strong>${esc(item.name)}</strong><small>${fmtBytes(item.size)} · ${esc(item.message)}</small></div>
      <progress max="100" value="0"></progress>
    </article>`).join("");
  runUploads();
}

async function runUploads() {
  if (state.uploadBusy) return;
  state.uploadBusy = true;
  try {
    for (const item of state.uploads) {
      if (item.status === "done" || item.status === "failed") continue;
      await uploadFile(item.file, null, item);
    }
  } finally {
    state.uploadBusy = false;
  }
}

async function uploadFile(file, resumeState = null, item = null) {
  if (!item) {
    item = { file, name: file.name, size: file.size, progress: 0,
      status: "uploading", message: "Starting" };
    state.uploads = [item, ...state.uploads];
  }
  const node = $(`[data-up="${state.uploads.indexOf(item)}"]`);
  const setProgress = (value, message, status = "uploading") => {
    item.progress = value; item.message = message; item.status = status;
    if (node) {
      const bar = $("progress", node);
      const small = $("small", node);
      if (bar) bar.value = value;
      if (small) small.textContent = `${fmtBytes(item.size)} · ${message}`;
    }
  };
  try {
    let upload = resumeState;
    if (!upload) {
      setProgress(0, "Creating upload");
      upload = await api("/api/uploads", {
        filename: file.name, size: file.size,
        metadata: { title: file.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ") },
      });
    }
    const chunk = (state.config && state.config.limits.chunk_size) || 8 * 1024 * 1024;
    let offset = Number(upload.received || 0);
    while (offset < file.size) {
      const end = Math.min(file.size, offset + chunk);
      setProgress(Math.round(100 * offset / file.size),
        `Uploading ${fmtBytes(offset)} of ${fmtBytes(file.size)}`);
      const response = await fetch(`/api/uploads/${upload.id}/chunk?offset=${offset}`, {
        method: "POST",
        headers: {
          "X-CSRF-Token": state.config.csrf,
          "Content-Type": "application/octet-stream",
        },
        body: file.slice(offset, end),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Chunk upload failed.");
      offset = Number(data.received || end);
      setProgress(Math.round(100 * offset / file.size),
        `Uploading ${fmtBytes(offset)} of ${fmtBytes(file.size)}`);
    }
    setProgress(100, "Validating and storing");
    const result = await api(`/api/uploads/${upload.id}/complete`, {});
    setProgress(100, result.duplicate ? "Already uploaded — opened existing episode" : "Queued for processing", "done");
    toast(result.duplicate ? "This episode already exists; opened it." : "Upload complete. Processing queued.");
    await refreshOverview();
    if (result.episode) {
      setTimeout(() => { location.hash = `#/review/${result.episode.id}`; }, 700);
    }
  } catch (error) {
    setProgress(item.progress || 0, error.message, "failed");
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------------- queue */
async function renderQueue(root) {
  const [queue, episodes] = await Promise.all([api("/api/queue"), api("/api/episodes")]);
  const byId = {};
  (episodes.episodes || []).forEach((episode) => { byId[episode.id] = episode; });
  const jobs = queue.jobs || [];
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>Processing queue</h2>
        <span class="muted">${queue.pending || 0} pending · ${queue.running || 0} running · serial worker</span>
      </div>
      <p class="fine">Processing resumes automatically after an app restart. The source video
      is read in place from persistent storage and is never modified.</p>
      ${jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Episode</th>
        <th>Status</th><th>Attempt</th><th>Updated</th><th></th></tr></thead><tbody>
        ${jobs.map((job) => {
          const episode = byId[job.episode_id] || {};
          return `<tr>
            <td><code>${esc(job.kind)}</code><small>${esc(job.id)}</small></td>
            <td><a class="link" href="#/review/${esc(job.episode_id)}">${esc(episode.title || job.episode_id)}</a></td>
            <td>${statusPill(job.status === "running" ? "processing" : job.status === "done" ? "approved" : job.status)}</td>
            <td>${job.attempts || 0}</td>
            <td>${fmtDate(job.finished || job.started || job.created)}</td>
            <td>${job.status === "pending" ? `<button class="button ghost small" data-cancel="${esc(job.id)}">Cancel</button>` : ""}
              <button class="button ghost small" data-log="${esc(job.episode_id)}">Log</button></td>
          </tr>`;
        }).join("")}
      </tbody></table></div>` : `<p class="empty">The queue is empty.</p>`}
    </section>
    <section class="panel"><div class="panel-head"><h2>Episodes</h2></div>
      ${episodeTable(episodes.episodes || [])}</section>`;
  $$("[data-cancel]").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/api/queue/${button.dataset.cancel}/cancel`, {}); toast("Job cancelled."); render(); }
    catch (error) { toast(error.message, true); }
  }));
  $$("[data-log]").forEach((button) => button.addEventListener("click", () => showLog(button.dataset.log)));
}

async function showLog(episodeId) {
  try {
    const data = await api(`/api/episodes/${episodeId}/log`);
    const dialog = document.createElement("dialog");
    dialog.className = "log-dialog";
    dialog.innerHTML = `<div class="panel-head"><h2>Job log</h2>
      <button class="button ghost small" data-close>Close</button></div>
      <pre>${esc(data.log || "No log yet.")}</pre>`;
    document.body.appendChild(dialog);
    $("[data-close]", dialog).addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove());
    dialog.showModal();
  } catch (error) { toast(error.message, true); }
}

/* ------------------------------------------------------------------ review */
async function renderReviewIndex(root) {
  const episodes = await api("/api/episodes");
  const list = (episodes.episodes || []).filter((episode) =>
    ["clips_awaiting_review", "approved", "failed", "rendering", "processing",
      "queued", "rendering_previews"].includes(episode.status));
  const route = (location.hash || "").split("/");
  if (route.length >= 3 && route[1] === "review" && route[2]) {
    return renderReviewDetail(root, route[2]);
  }
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>Clips awaiting review</h2>
        <span class="muted">Approve the moments worth posting. Nothing publishes from this app.</span></div>
      ${list.length ? `<div class="episode-grid">${list.map((episode) => `
        <a class="episode-card" href="#/review/${esc(episode.id)}">
          <div class="episode-top">${statusPill(episode.status)}
            <span class="muted">${fmtDate(episode.created)}</span></div>
          <h3>${esc(episode.title || episode.original_name || episode.id)}</h3>
          <p>${esc(episode.stage || "")}</p>
          <div class="mini-stats">
            <span>${episode.counts ? episode.counts.clips : 0} clips</span>
            <span>${episode.counts ? episode.counts.approved : 0} approved</span>
            <span>${episode.counts ? episode.counts.exported : 0} exported</span>
          </div>
          ${episode.error ? `<p class="error-text">${esc(String(episode.error).slice(0, 180))}</p>` : ""}
        </a>`).join("")}</div>` : `<p class="empty">No episodes are waiting. Upload one to start.</p>`}
    </section>`;
}

async function renderReviewDetail(root, episodeId) {
  const data = await api(`/api/episodes/${episodeId}`);
  state.detail = data;
  const episode = data.episode;
  const clips = data.clips || [];
  const pending = clips.filter((clip) => clip.decision === "pending").length;
  const approved = clips.filter((clip) => clip.decision === "approved").length;
  const layout = episode.layout || {};
  root.innerHTML = `
    <section class="panel review-head">
      <div>
        <a class="link small" href="#/review">← All episodes</a>
        <div class="title-row">
          <h2 contenteditable="true" id="episode-title" spellcheck="false">${esc(episode.title || episode.id)}</h2>
          ${statusPill(episode.status)}
        </div>
        <p class="muted">${esc(episode.original_name || "")} · ${clips.length} clips ·
          ${pending} pending · ${approved} approved · ${esc(episode.stage || "")}</p>
        ${episode.error ? `<pre class="error-box">${esc(episode.error)}</pre>` : ""}
      </div>
      <div class="row-actions">
        <button class="button ghost small" id="save-title">Save title</button>
        <button class="button ghost small" id="preview-btn">Render previews</button>
        <button class="button ghost small" id="reprocess-btn">Reprocess</button>
        <button class="button primary small" id="approve-selected">Approve &amp; export selected</button>
      </div>
    </section>
    <div class="review-layout">
      <aside class="panel source-panel">
        <h2>Source review</h2>
        <video id="source-player" controls preload="metadata" src="${esc(data.source_url)}"></video>
        <p class="fine" id="play-hint">Choose “Play clip” on a card to preview that range from the original.</p>
        <h3>Vertical layout</h3>
        <label>Composition
          <select id="layout-select">
            ${["single", "split", "stacked"].map((value) =>
              `<option value="${value}" ${layout.layout === value ? "selected" : ""}>${value}</option>`).join("")}
          </select>
        </label>
        <label>Fit
          <select id="fit-select">
            <option value="cover" ${layout.fit === "cover" ? "selected" : ""}>cover (fill frame)</option>
            <option value="contain" ${layout.fit === "contain" ? "selected" : ""}>contain (show whole frame)</option>
          </select>
        </label>
        <button class="button small" id="layout-save">Save layout</button>
        ${episode.layout_analysis ? `<p class="fine">Detected ${episode.layout_analysis.speakers == null ?
          "no clear face count" : episode.layout_analysis.speakers + " speaker(s)"} ·
          suggested ${esc(episode.layout_analysis.suggested_layout)} /
          ${esc(episode.layout_analysis.suggested_fit)} · confidence
          ${Math.round((episode.layout_analysis.confidence || 0) * 100)}%</p>` : ""}
        <p class="fine">3+ speaker frames use “single + contain” so nobody is cropped out.
          You can override any layout before exporting.</p>
        ${data.previews && data.previews.length ? `<h3>Previews</h3>
          ${data.previews.filter((file) => file.name.endsWith(".mp4")).slice(0, 12).map((file) =>
            `<video class="preview-video" controls preload="metadata" src="${esc(file.url)}"></video>`).join("")}` : ""}
        <h3>AI editorial</h3>
        <p class="fine">${esc((data.editorial && data.editorial.generated_by) || "deterministic")} ·
          ${esc((data.editorial && data.editorial.model) || "local scoring")}</p>
        ${(data.editorial && data.editorial.warnings || []).map((warning) =>
          `<p class="warn-text">${esc(warning)}</p>`).join("")}
      </aside>
      <section class="clip-stack">
        ${clips.length ? clips.map((clip, index) => reviewClipCard(clip, index, data)).join("") :
          `<div class="panel"><p class="empty">No clips yet. Processing may still be running.</p></div>`}
      </section>
    </div>`;
  wireReview(root, episodeId, data);
}

function reviewClipCard(clip, index, data) {
  const scores = clip.scores || {};
  const scoreLabels = {
    engagement: "Engagement", agronomic_relevance: "Agronomic",
    educational_value: "Educational", timeliness: "Timeliness",
    hook_strength: "Hook",
  };
  return `<article class="panel clip-card ${esc(clip.decision)}" data-clip="${esc(clip.clip_id)}">
    <header class="clip-head">
      <label class="check"><input type="checkbox" class="clip-select" value="${esc(clip.clip_id)}">
        <span></span></label>
      <div>
        <div class="title-row"><h3>${esc(clip.title || "Untitled clip")}</h3>
          <span class="score ${clip.overall >= 75 ? "good" : clip.overall >= 55 ? "warn" : ""}">${esc(clip.overall || 0)}</span>
        </div>
        <p class="muted">${fmtClock(clip.start)}–${fmtClock(clip.end)} ·
          ${Math.round(clip.duration || 0)}s · ${statusPill(clip.decision === "approved" ? "approved" : clip.decision === "rejected" ? "rejected" : "queued")}
          ${clip.ai ? '<span class="pill subtle">AI assisted</span>' : '<span class="pill subtle">Deterministic</span>'}</p>
      </div>
      <button class="button ghost small" data-play="${esc(clip.start)}" data-end="${esc(clip.end)}">Play clip</button>
    </header>
    <div class="score-row">
      ${Object.entries(scoreLabels).map(([key, label]) => `<div class="score-bar">
        <span>${label}</span><i style="--v:${Number(scores[key] || 0)}%"></i>
        <b>${Number(scores[key] || 0)}</b></div>`).join("")}
    </div>
    <div class="clip-grid">
      <div>
        <label>Title<input class="clip-title" maxlength="120" value="${esc(clip.title || "")}"></label>
        <label>Description<textarea class="clip-description" rows="5">${esc(clip.description || "")}</textarea></label>
        <div class="platform-row">
          ${(state.config.platforms || []).map((platform) => `<label class="check">
            <input type="checkbox" class="platform" value="${esc(platform.id)}"
              ${(clip.platforms || []).includes(platform.id) ? "checked" : ""}>
            <span>${esc(platform.label)}</span></label>`).join("")}
        </div>
        <label>Reviewer notes<input class="clip-notes" maxlength="1000" value="${esc(clip.notes || "")}"></label>
      </div>
      <div>
        <h4>Selection explanation</h4>
        <p>${esc(clip.selection_explanation || "")}</p>
        <h4>Seasonal relevance</h4>
        <p>${esc(clip.seasonal_relevance || "No seasonal note.")}
          ${clip.expires ? `<span class="pill warn">expires ${esc(clip.expires)}</span>` : ""}</p>
        ${(clip.warnings || []).map((warning) => `<p class="warn-text">${esc(warning)}</p>`).join("")}
        <details><summary>Exact transcript (${Math.round(clip.duration || 0)}s)</summary>
          <p class="transcript">${esc(clip.transcript || "")}</p></details>
        <h4>Recommended platforms</h4>
        <p>${(clip.platforms || []).map((platform) => `<span class="pill subtle">${esc(platform)}</span>`).join(" ") || "—"}</p>
        ${clip.export && clip.export.urls ? `<p class="fine">Exported ${fmtDate(clip.export.exported)} ·
          <a class="link" href="${esc(clip.export.urls.master)}" download>Download master</a></p>` : ""}
        ${clip.render_error ? `<p class="error-text">${esc(clip.render_error)}</p>` : ""}
      </div>
    </div>
    <footer class="clip-actions">
      <button class="button ghost small" data-save>Save edits</button>
      <button class="button small" data-approve>Approve &amp; export</button>
      <button class="button danger small" data-reject>Reject</button>
    </footer>
  </article>`;
}

function wireReview(root, episodeId, data) {
  const player = $("#source-player");
  let stopAt = null;
  const stop = () => {
    if (stopAt != null && player.currentTime >= stopAt) {
      player.pause(); stopAt = null;
    }
  };
  if (player) player.addEventListener("timeupdate", stop);
  $$("[data-play]", root).forEach((button) => button.addEventListener("click", () => {
    const start = Number(button.dataset.play);
    const end = Number(button.dataset.end);
    stopAt = end;
    player.currentTime = start;
    player.play().catch(() => toast("Press play on the source video.", true));
    $("#play-hint").textContent = `Playing ${fmtClock(start)}–${fmtClock(end)}.`;
  }));
  $$(".clip-select", root).forEach((box) => box.addEventListener("change", () => {
    if (box.checked) state.selected.add(box.value); else state.selected.delete(box.value);
  }));
  $("#save-title", root).addEventListener("click", async () => {
    try {
      const title = $("#episode-title", root).textContent.trim();
      await api(`/api/episodes/${episodeId}`, { title });
      toast("Episode title saved.");
      await refreshOverview();
    } catch (error) { toast(error.message, true); }
  });
  $("#layout-save", root).addEventListener("click", async () => {
    try {
      await api(`/api/episodes/${episodeId}/layout`, {
        layout: $("#layout-select", root).value,
        fit: $("#fit-select", root).value,
      });
      toast("Layout saved. Approved clips will use it when exported.");
      render();
    } catch (error) { toast(error.message, true); }
  });
  $("#preview-btn", root).addEventListener("click", async () => {
    try { await api(`/api/episodes/${episodeId}/preview`, {}); toast("Preview render queued."); await refreshOverview(); }
    catch (error) { toast(error.message, true); }
  });
  $("#reprocess-btn", root).addEventListener("click", async () => {
    if (!confirm("Reprocess this episode? Existing edits are kept when clip times match.")) return;
    try { await api(`/api/episodes/${episodeId}/prepare`, {}); toast("Reprocessing queued."); await refreshOverview(); }
    catch (error) { toast(error.message, true); }
  });
  $("#approve-selected", root).addEventListener("click", async () => {
    const selected = Array.from(state.selected);
    if (!selected.length) return toast("Select at least one clip first.", true);
    try {
      await api(`/api/episodes/${episodeId}/approve`, { clip_ids: selected });
      toast("Approved clips queued for vertical export.");
      state.selected.clear();
      await refreshOverview();
      location.hash = `#/approved`;
    } catch (error) { toast(error.message, true); }
  });
  $$(".clip-card", root).forEach((card) => {
    const clipId = card.dataset.clip;
    const collect = () => ({
      title: $(".clip-title", card).value,
      description: $(".clip-description", card).value,
      notes: $(".clip-notes", card).value,
      platforms: $$(".platform", card).filter((box) => box.checked).map((box) => box.value),
    });
    $("[data-save]", card).addEventListener("click", async () => {
      try {
        await api(`/api/episodes/${episodeId}/clips/${clipId}`, collect());
        toast("Clip edits saved.");
        await refreshOverview();
      } catch (error) { toast(error.message, true); }
    });
    $("[data-approve]", card).addEventListener("click", async () => {
      try {
        await api(`/api/episodes/${episodeId}/clips/${clipId}`,
          { ...collect(), decision: "approved" });
        await api(`/api/episodes/${episodeId}/approve`, { clip_ids: [clipId] });
        toast("Clip approved and queued for export.");
        await refreshOverview();
        render();
      } catch (error) { toast(error.message, true); }
    });
    $("[data-reject]", card).addEventListener("click", async () => {
      if (!confirm("Reject this clip? It stays in the transcript but will not export.")) return;
      try {
        await api(`/api/episodes/${episodeId}/clips/${clipId}`, { decision: "rejected" });
        toast("Clip rejected.");
        await refreshOverview();
        render();
      } catch (error) { toast(error.message, true); }
    });
  });
}

/* ---------------------------------------------------------------- approved */
async function renderApproved(root) {
  const data = await api("/api/clips?decision=approved");
  const clips = (data.clips || []).filter((clip) => clip.export && clip.export.urls);
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>Approved clips</h2>
        <span class="muted">${clips.length} 1080×1920 master${clips.length === 1 ? "" : "s"} ·
          platform metadata is separate</span></div>
      ${clips.length ? `<div class="approved-grid">${clips.map((clip) => approvedCard(clip)).join("")}</div>` :
        `<p class="empty">No exported clips yet. Approve clips in Review to render masters.</p>`}
    </section>`;
  $$("[data-copy]", root).forEach((button) => button.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(button.dataset.copy); toast("Copied."); }
    catch (_) { toast("Copy failed; select the text manually.", true); }
  }));
}

function approvedCard(clip) {
  const urls = clip.export.urls || {};
  const hashtags = (clip.export && clip.export.hashtags) || clip.hashtags || [];
  const platformText = Object.entries(clip.export.platforms || {}).map(([key, value]) => {
    const label = (state.config.platforms.find((item) => item.id === key) || {}).label || key;
    return `${label}:\n${value.title || value.caption || value.message || ""}`;
  }).join("\n\n");
  return `<article class="panel approved-card">
    <video controls preload="metadata" poster="${urls.poster ? esc(urls.poster) : ""}"
      src="${esc(urls.master || "#")}"></video>
    <div class="clip-head"><div><h3>${esc(clip.title)}</h3>
      <p class="muted">${esc(clip.episode_title || "")} · ${fmtClock(clip.start)}–${fmtClock(clip.end)}
      · score ${esc(clip.overall || 0)}</p></div></div>
    <p>${esc(clip.description || "")}</p>
    <p>${hashtags.map((tag) => `<span class="pill subtle">${esc(tag)}</span>`).join(" ")}</p>
    <div class="row-actions">
      <a class="button small" href="${esc(urls.master)}" download>Download master</a>
      <a class="button ghost small" href="${esc(urls.captions || "#")}" download>SRT</a>
      <a class="button ghost small" href="${esc(urls.metadata || "#")}" download>Metadata</a>
      <button class="button ghost small" data-copy="${esc(platformText)}">Copy captions</button>
    </div>
    <details><summary>Platform metadata</summary>
      <pre>${esc(JSON.stringify(clip.export.platforms || {}, null, 2))}</pre></details>
  </article>`;
}

/* ------------------------------------------------------------------- brand */
async function renderBrand(root) {
  const brand = await api("/api/brand");
  state.brand = brand;
  const caption = brand.caption || {};
  root.innerHTML = `
    <form class="panel brand-form" id="brand-form">
      <div class="panel-head"><h2>Brand settings</h2>
        <span class="muted">Applied to new exports and caption renders</span></div>
      <div class="brand-grid">
        <div>
          <label>Podcast name<input name="podcast_name" maxlength="120" value="${esc(brand.podcast_name || "")}" required></label>
          <div class="color-row">
            <label>Primary<input type="color" name="primary_color" value="${esc(brand.primary_color || "#2D5016")}"></label>
            <label>Secondary<input type="color" name="secondary_color" value="${esc(brand.secondary_color || "#F4F1E8")}"></label>
            <label>Accent<input type="color" name="accent_color" value="${esc(brand.accent_color || "#FFE14D")}"></label>
          </div>
          <label>Speaker names (comma separated)<input name="speaker_names"
            value="${esc((brand.speaker_names || []).join(", "))}" placeholder="Cole, Megan"></label>
          <h3>Caption style</h3>
          <div class="field-grid">
            <label>Font<input name="caption_font" value="${esc(caption.font || "DejaVu Sans")}"></label>
            <label>Size (px at 1920)<input type="number" name="caption_size" min="12" max="200" value="${esc(caption.font_size || 74)}"></label>
            <label>Text color<input type="color" name="caption_color" value="${esc(caption.color || "#FFFFFF")}"></label>
            <label>Highlight<input type="color" name="caption_highlight" value="${esc(caption.highlight || "#FFE14D")}"></label>
            <label>Outline<input type="number" name="caption_outline" min="0" max="12" value="${esc(caption.outline ?? 5)}"></label>
            <label>Words per cue<input type="number" name="caption_words" min="1" max="8" value="${esc(caption.words_per_cue || 3)}"></label>
            <label>Position<select name="caption_position">
              ${["bottom", "center", "top"].map((value) => `<option value="${value}" ${caption.position === value ? "selected" : ""}>${value}</option>`).join("")}
            </select></label>
            <label>Background<select name="caption_background">
              ${["none", "box"].map((value) => `<option value="${value}" ${caption.background === value ? "selected" : ""}>${value}</option>`).join("")}
            </select></label>
          </div>
          <label class="check"><input type="checkbox" name="caption_uppercase" ${caption.uppercase ? "checked" : ""}><span>Uppercase captions</span></label>
          <h3>Custom vocabulary</h3>
          <p class="fine">One correction per line: <code>spoken form =&gt; Correct Term</code>.
            Applied to new transcriptions as whole-word checks.</p>
          <textarea name="vocabulary" rows="5" placeholder="sidedress =&gt; sidedress&#10;vomitoxin =&gt; vomitoxin">${esc((brand.vocabulary || []).map((pair) => pair[0] + " => " + pair[1]).join("\n"))}</textarea>
        </div>
        <div>
          <h3>Logo</h3>
          ${brand.logo_url ? `<img class="brand-logo" src="${esc(brand.logo_url)}" alt="Current logo">` :
            `<p class="empty">No logo uploaded.</p>`}
          <input type="file" id="logo-file" accept="image/png,image/jpeg,image/webp">
          <p class="fine">PNG/JPEG/WebP, up to 6 MB. The logo is overlaid on rendered clips.</p>
          <h3>Intro / outro</h3>
          <p class="fine">${brand.intro_url ? "Intro stored. " : "No intro. "}${brand.outro_url ? "Outro stored. " : "No outro. "}
            Stored for future stitching; current exports do not include intro/outro.</p>
          <input type="file" id="intro-file" accept="video/mp4,video/quicktime,.mp4,.mov,.mp3,.wav,.m4a">
          <input type="file" id="outro-file" accept="video/mp4,video/quicktime,.mp4,.mov,.mp3,.wav,.m4a">
          <h3>Platform-safe margins</h3>
          <div class="field-grid">
            ${["top", "bottom", "left", "right"].map((side) => `<label>${side}<input type="number"
              name="safe_${side}" min="0" max="900" value="${esc((brand.platform_safe || {})[side] ?? 0)}"></label>`).join("")}
          </div>
          <p class="fine">These margins are saved for caption placement checks. The current
            renderer uses its own vertical caption margins.</p>
        </div>
      </div>
      <div class="row-actions"><button class="button primary" type="submit">Save brand settings</button></div>
    </form>`;
  $("#brand-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const caption = {
      font: form.get("caption_font"),
      font_size: Number(form.get("caption_size")),
      color: form.get("caption_color"),
      highlight: form.get("caption_highlight"),
      outline_color: "#000000",
      outline: Number(form.get("caption_outline")),
      words_per_cue: Number(form.get("caption_words")),
      position: form.get("caption_position"),
      background: form.get("caption_background"),
      uppercase: form.get("caption_uppercase") === "on",
    };
    const vocabulary = String(form.get("vocabulary") || "").split("\n").map((line) =>
      line.split("=>").map((part) => part.trim())).filter((pair) =>
      pair.length === 2 && pair[0] && pair[1]);
    const body = {
      podcast_name: form.get("podcast_name"),
      primary_color: form.get("primary_color"),
      secondary_color: form.get("secondary_color"),
      accent_color: form.get("accent_color"),
      speaker_names: String(form.get("speaker_names") || "").split(",")
        .map((name) => name.trim()).filter(Boolean),
      caption,
      vocabulary,
      platform_safe: {
        top: Number(form.get("safe_top")), bottom: Number(form.get("safe_bottom")),
        left: Number(form.get("safe_left")), right: Number(form.get("safe_right")),
      },
    };
    try { await api("/api/brand", body, "PUT"); toast("Brand settings saved."); render(); }
    catch (error) { toast(error.message, true); }
  });
  const upload = async (kind, input) => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const response = await fetch(`/api/brand/${kind}`, {
        method: "POST",
        headers: { "X-CSRF-Token": state.config.csrf, "X-Filename": file.name,
          "Content-Type": "application/octet-stream" },
        body: file,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Upload failed.");
      toast(kind[0].toUpperCase() + kind.slice(1) + " saved.");
      render();
    } catch (error) { toast(error.message, true); }
  };
  $("#logo-file").addEventListener("change", (event) => upload("logo", event.target));
  $("#intro-file").addEventListener("change", (event) => upload("intro", event.target));
  $("#outro-file").addEventListener("change", (event) => upload("outro", event.target));
}

/* ----------------------------------------------------------------- storage */
async function renderStorage(root) {
  const data = await api("/api/storage");
  state.storage = data;
  const disk = data.disk || {};
  const percent = disk.total ? Math.min(100, 100 * disk.used / disk.total) : 0;
  const reservePercent = disk.total ? Math.min(100, 100 * disk.min_free / disk.total) : 0;
  root.innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>Storage</h2>
        <span class="muted">Persistent volume · ${fmtBytes(disk.free)} free ·
          ${fmtBytes(disk.min_free)} safety reserve</span></div>
      <div class="disk-bar"><i style="width:${percent}%"></i>
        <b style="left:${Math.max(0, 100 - reservePercent)}%"></b></div>
      <p class="fine">The safety reserve line is ${fmtBytes(disk.min_free)} free. New uploads are
        refused when accepting them would leave less than that reserve.</p>
      <div class="stat-grid small">
        ${statCard("Sources", fmtBytes(data.source_bytes), (data.sources || []).length + " originals")}
        ${statCard("Projects", fmtBytes(data.project_bytes), (data.projects || []).length + " derived")}
        ${statCard("Exports", fmtBytes(data.export_bytes), "approved masters")}
        ${statCard("Models", fmtBytes(data.model_bytes), "Parakeet cache")}
      </div>
      <div class="row-actions"><button class="button" id="cleanup-btn">Clean stale scratch files</button></div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Original sources</h2>
        <span class="muted">Originals are never modified. Delete only when you are sure.</span></div>
      ${(data.sources || []).length ? `<div class="table-wrap"><table><thead><tr>
        <th>Original</th><th>Size</th><th>Episodes</th><th>Stored</th><th></th></tr></thead><tbody>
        ${data.sources.map((source) => `<tr>
          <td><strong>${esc(source.original_name || source.filename)}</strong>
            <small><code>${esc(source.id.slice(0, 12))}…</code></small></td>
          <td>${fmtBytes(source.size_bytes || source.size)}</td>
          <td>${(source.episodes || []).map((episode) =>
            `<a class="link" href="#/review/${esc(episode.id)}">${esc(episode.title)}</a>`).join("<br>") || "—"}</td>
          <td>${fmtDate(source.created)}</td>
          <td><button class="button ghost small" data-verify="${esc(source.id)}">Verify hash</button>
            <button class="button danger small" data-delete="${esc(source.id)}">Delete…</button></td>
        </tr>`).join("")}</tbody></table></div>` : `<p class="empty">No original sources stored.</p>`}
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Incomplete uploads</h2></div>
      ${(data.uploads || []).filter((item) => item.status !== "complete").length ? `
        <div class="table-wrap"><table><thead><tr><th>File</th><th>Progress</th><th></th></tr></thead><tbody>
        ${(data.uploads || []).filter((item) => item.status !== "complete").map((item) => `<tr>
          <td>${esc(item.filename)}</td>
          <td>${Math.round(100 * (item.received || 0) / Math.max(1, item.size || 1))}%</td>
          <td><button class="button danger small" data-abort="${esc(item.id)}">Discard</button></td>
        </tr>`).join("")}</tbody></table></div>` : `<p class="empty">None.</p>`}
    </section>`;
  $("#cleanup-btn").addEventListener("click", async () => {
    try {
      const result = await api("/api/storage/cleanup", {});
      toast("Removed " + (result.removed || []).length + " stale path(s).");
      render();
    } catch (error) { toast(error.message, true); }
  });
  $$("[data-verify]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const result = await api(`/api/storage/sources/${button.dataset.verify}/verify`, {});
      toast(result.match ? "Source hash matches the upload." : "Source hash changed!", !result.match);
    } catch (error) { toast(error.message, true); }
  }));
  $$("[data-delete]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Delete this original source from persistent storage? This cannot be undone.")) return;
    const deleteProjects = confirm("Also delete processed episodes and exported clips for this source? OK = yes, Cancel = keep them.");
    try {
      await api(`/api/storage/sources/${button.dataset.delete}/delete`,
        { confirm: true, delete_projects: deleteProjects });
      toast("Original source deleted.");
      await refreshOverview();
      render();
    } catch (error) { toast(error.message, true); }
  }));
  $$("[data-abort]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Discard this incomplete upload?")) return;
    try { await api(`/api/uploads/${button.dataset.abort}/abort`, {}); render(); }
    catch (error) { toast(error.message, true); }
  }));
}

/* --------------------------------------------------------------- placeholders */
function renderComingSoon(title, copy) {
  $("#view").innerHTML = `<section class="panel coming-soon">
    <span class="eyebrow">COMING SOON</span>
    <h2>${esc(title)}</h2>
    <p>${esc(copy)}</p>
    <p class="fine">No social accounts are connected. This app does not publish anything.</p>
    <div class="row-actions"><a class="button" href="#/review">Review clips</a>
      <a class="button ghost" href="#/approved">Approved clips</a></div>
  </section>`;
}

/* -------------------------------------------------------------------- router */
function route() {
  const raw = (location.hash || "#/overview").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  state.view = parts[0] || "overview";
  state.selected.clear();
  render();
}

async function poll() {
  try {
    await refreshOverview();
    if (state.view === "overview") renderOverview($("#view")).catch(() => {});
    if (state.view === "queue") renderQueue($("#view")).catch(() => {});
  } catch (error) {
    if (!String(error.message).includes("Sign in")) toast(error.message, true);
  }
}

async function boot() {
  try {
    await loadConfig();
    await refreshOverview();
    route();
    window.addEventListener("hashchange", route);
    pollTimer = setInterval(poll, 5000);
  } catch (error) {
    $("#view").innerHTML = `<div class="panel error-panel"><h2>Could not start</h2>
      <p>${esc(error.message)}</p></div>`;
  }
}

document.addEventListener("DOMContentLoaded", boot);
