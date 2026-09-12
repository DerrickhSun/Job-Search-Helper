// Job-Applyer control panel — talks to the same local extension_server.py the browser
// extension does (see extension/background.js). Settings are kept in this browser's
// localStorage only; the token never leaves this page except in the Authorization header
// sent straight to the server URL below.

const SETTINGS_KEY = "jobApplyerControlPanelSettings";
const DEFAULT_SERVER_URL = "http://127.0.0.1:8743";

const serverUrlInput = document.getElementById("server-url");
const tokenInput = document.getElementById("token");
const saveButton = document.getElementById("save");
const saveStatus = document.getElementById("save-status");
const pingButton = document.getElementById("ping");
const statusEl = document.getElementById("status");

function loadSettings() {
  let settings = {};
  try {
    settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  } catch {
    settings = {};
  }
  serverUrlInput.value = settings.serverUrl || DEFAULT_SERVER_URL;
  tokenInput.value = settings.token || "";
}

function saveSettings() {
  const settings = {
    serverUrl: serverUrlInput.value.trim() || DEFAULT_SERVER_URL,
    token: tokenInput.value.trim(),
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  return settings;
}

saveButton.addEventListener("click", () => {
  saveSettings();
  saveStatus.textContent = " Saved.";
  setTimeout(() => { saveStatus.textContent = ""; }, 2000);
});

// Shared low-level call: throws a plain Error with a useful message on any failure (network,
// non-2xx, or non-JSON body) so callers only need one catch block.
async function callServer(path, options = {}) {
  const settings = saveSettings();
  if (!settings.token) {
    throw new Error("Set a token above first.");
  }

  let res;
  try {
    res = await fetch(settings.serverUrl.replace(/\/+$/, "") + path, {
      ...options,
      headers: { "Authorization": "Bearer " + settings.token, ...(options.headers || {}) },
    });
  } catch (err) {
    throw new Error(
      "Could not reach " + settings.serverUrl + ": " + err.message +
      "\n(Is extension_server.py running on that address? See pages/README.md if this " +
      "page is served over https:// — Chrome's Private Network Access check can block it.)"
    );
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error("Server responded " + res.status + ": " + JSON.stringify(data));
  }
  return data;
}

pingButton.addEventListener("click", async () => {
  statusEl.textContent = "Contacting server...";
  statusEl.className = "";
  try {
    const data = await callServer("/health", { method: "GET" });
    statusEl.textContent = "OK — server responded: " + JSON.stringify(data);
    statusEl.className = "ok";
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.className = "err";
  }
});

// --- Config (data/behavior.json + data/search.json) ---

const loadConfigButton = document.getElementById("load-config");
const loadStatus = document.getElementById("load-status");
const configForm = document.getElementById("config-form");
const saveConfigButton = document.getElementById("save-config");
const saveConfigStatus = document.getElementById("save-config-status");

const cfg = {
  skipConsulting: document.getElementById("cfg-skip-consulting"),
  consultingMemory: document.getElementById("cfg-consulting-memory"),
  ghManualNext: document.getElementById("cfg-gh-manual-next"),
  ghPrefetch: document.getElementById("cfg-gh-prefetch"),
  ghPromptClose: document.getElementById("cfg-gh-prompt-close"),
  ghDatePosted: document.getElementById("cfg-gh-date-posted"),
  ghGateProbe: document.getElementById("cfg-gh-gate-probe"),
  studentMode: document.getElementById("cfg-student-mode"),
  unpaidMode: document.getElementById("cfg-unpaid-mode"),
  keywords: document.getElementById("cfg-keywords"),
  location: document.getElementById("cfg-location"),
  postedWithin24h: document.getElementById("cfg-posted-24h"),
};

function fillSelect(selectEl, choices, current) {
  selectEl.innerHTML = "";
  for (const choice of choices) {
    const opt = document.createElement("option");
    opt.value = choice;
    opt.textContent = choice;
    selectEl.appendChild(opt);
  }
  selectEl.value = current;
}

function populateConfigForm(data) {
  const b = data.behavior;
  const s = data.search;
  cfg.skipConsulting.checked = !!b.skip_consulting;
  cfg.consultingMemory.checked = !!b.consulting_companies_memory;
  cfg.ghManualNext.checked = !!b.greenhouse_manual_next_listing;
  cfg.ghPrefetch.checked = !!b.greenhouse_prefetch;
  cfg.ghPromptClose.checked = !!b.greenhouse_prompt_before_close;
  cfg.ghDatePosted.value = b.greenhouse_date_posted || "";
  cfg.ghGateProbe.value = b.greenhouse_gate_probe_max_listings;
  fillSelect(cfg.studentMode, data.options.student_job_mode, b.student_job_mode);
  fillSelect(cfg.unpaidMode, data.options.unpaid_job_mode, b.unpaid_job_mode);

  cfg.keywords.value = (s.keywords || []).join("\n");
  cfg.location.value = s.location || "";
  cfg.postedWithin24h.checked = !!s.posted_within_24h;

  configForm.hidden = false;
}

loadConfigButton.addEventListener("click", async () => {
  loadStatus.textContent = " Loading...";
  loadStatus.className = "";
  try {
    const data = await callServer("/config", { method: "GET" });
    populateConfigForm(data);
    loadStatus.textContent = "";
  } catch (err) {
    loadStatus.textContent = " " + err.message;
    loadStatus.className = "err";
  }
});

saveConfigButton.addEventListener("click", async () => {
  const behavior = {
    skip_consulting: cfg.skipConsulting.checked,
    consulting_companies_memory: cfg.consultingMemory.checked,
    greenhouse_manual_next_listing: cfg.ghManualNext.checked,
    greenhouse_prefetch: cfg.ghPrefetch.checked,
    greenhouse_prompt_before_close: cfg.ghPromptClose.checked,
    greenhouse_date_posted: cfg.ghDatePosted.value.trim() || null,
    greenhouse_gate_probe_max_listings: parseInt(cfg.ghGateProbe.value, 10) || 0,
    student_job_mode: cfg.studentMode.value,
    unpaid_job_mode: cfg.unpaidMode.value,
  };
  const search = {
    keywords: cfg.keywords.value.split("\n").map((k) => k.trim()).filter(Boolean),
    location: cfg.location.value.trim(),
    posted_within_24h: cfg.postedWithin24h.checked,
  };

  saveConfigStatus.textContent = " Saving...";
  saveConfigStatus.className = "";
  try {
    const data = await callServer("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ behavior, search }),
    });
    populateConfigForm(data);
    saveConfigStatus.textContent = " Saved.";
    saveConfigStatus.className = "ok";
    setTimeout(() => { saveConfigStatus.textContent = ""; }, 2000);
  } catch (err) {
    saveConfigStatus.textContent = " " + err.message;
    saveConfigStatus.className = "err";
  }
});

loadSettings();
