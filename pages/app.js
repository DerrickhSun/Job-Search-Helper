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

// --- Device profile (extension-issued profile id) ---
//
// The extension's content script bridges window.postMessage <-> its own background.js (see
// extension/content_scripts/content.js) so this page never needs the raw LinkedIn cookies --
// only an opaque profile id, which the server can later use to look up which cookie set to act
// with (see extension_server.py's /profile/connect and /profile/ping docstrings). Nothing here
// triggers an actual action with the profile id yet -- ping only confirms the server recognizes it.

const getProfileIdButton = document.getElementById("get-profile-id");
const profileIdDisplay = document.getElementById("profile-id-display");
const pingProfileButton = document.getElementById("ping-profile");
const profileStatus = document.getElementById("profile-status");

let currentProfileId = null;

function requestProfileIdFromExtension(timeoutMs = 2000) {
  return new Promise((resolve) => {
    const requestId = Math.random().toString(36).slice(2);
    let settled = false;

    const listener = (event) => {
      if (event.source !== window) return;
      if (!event.data || event.data.source !== "jobapplyer-extension" || event.data.type !== "PROFILE_ID") return;
      if (event.data.requestId !== requestId) return;
      settled = true;
      window.removeEventListener("message", listener);
      resolve(event.data.profileId || null);
    };
    window.addEventListener("message", listener);

    window.postMessage({ source: "jobapplyer-page", type: "GET_PROFILE_ID", requestId }, window.location.origin);

    // No extension installed (or an old version without this bridge) means no reply ever
    // arrives -- fall back to "no profile id" instead of waiting forever.
    setTimeout(() => {
      if (settled) return;
      window.removeEventListener("message", listener);
      resolve(null);
    }, timeoutMs);
  });
}

getProfileIdButton.addEventListener("click", async () => {
  profileStatus.textContent = "Asking the extension...";
  profileStatus.className = "";
  pingProfileButton.disabled = true;

  const profileId = await requestProfileIdFromExtension();
  if (!profileId) {
    currentProfileId = null;
    profileIdDisplay.textContent = "(none)";
    profileStatus.textContent =
      "No profile id — make sure the extension is installed and you've pressed " +
      '"Connect to LinkedIn" at least once.';
    profileStatus.className = "err";
    return;
  }

  currentProfileId = profileId;
  profileIdDisplay.textContent = profileId;
  profileStatus.textContent = "";
  pingProfileButton.disabled = false;
});

pingProfileButton.addEventListener("click", async () => {
  if (!currentProfileId) return;
  profileStatus.textContent = "Pinging server...";
  profileStatus.className = "";
  try {
    const data = await callServer("/profile/ping", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: currentProfileId }),
    });
    profileStatus.textContent = data.known
      ? "Known profile — connected site(s): " + (data.sites.join(", ") || "(none)")
      : "Server does not recognize this profile id.";
    profileStatus.className = data.known ? "ok" : "err";
  } catch (err) {
    profileStatus.textContent = err.message;
    profileStatus.className = "err";
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

// --- Company blacklist (data/company_blacklist.json + data/company_blacklist_temporary.json) ---

const loadBlacklistButton = document.getElementById("load-blacklist");
const loadBlacklistStatus = document.getElementById("load-blacklist-status");
const blacklistSection = document.getElementById("blacklist-section");
const blacklistStatus = document.getElementById("blacklist-status");
const permanentList = document.getElementById("blacklist-permanent-list");
const temporaryList = document.getElementById("blacklist-temporary-list");
const permanentInput = document.getElementById("blacklist-permanent-input");
const permanentAddButton = document.getElementById("blacklist-permanent-add");
const temporaryCompanyInput = document.getElementById("blacklist-temporary-company");
const temporaryUntilInput = document.getElementById("blacklist-temporary-until");
const temporaryAddButton = document.getElementById("blacklist-temporary-add");

function renderBlacklist(data) {
  permanentList.innerHTML = "";
  if (!data.permanent.length) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="empty">None</span>';
    permanentList.appendChild(li);
  }
  for (const company of data.permanent) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = company;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => postBlacklistAction({ action: "remove_permanent", company }));
    li.append(label, removeBtn);
    permanentList.appendChild(li);
  }

  temporaryList.innerHTML = "";
  if (!data.temporary.length) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="empty">None</span>';
    temporaryList.appendChild(li);
  }
  for (const entry of data.temporary) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = entry.company + " — until " + entry.until;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () =>
      postBlacklistAction({ action: "remove_temporary", company: entry.company })
    );
    li.append(label, removeBtn);
    temporaryList.appendChild(li);
  }

  blacklistSection.hidden = false;
}

async function postBlacklistAction(body) {
  blacklistStatus.textContent = "Saving...";
  blacklistStatus.className = "";
  try {
    const data = await callServer("/blacklist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderBlacklist(data);
    blacklistStatus.textContent = "";
  } catch (err) {
    blacklistStatus.textContent = err.message;
    blacklistStatus.className = "err";
  }
}

loadBlacklistButton.addEventListener("click", async () => {
  loadBlacklistStatus.textContent = " Loading...";
  loadBlacklistStatus.className = "";
  try {
    const data = await callServer("/blacklist", { method: "GET" });
    renderBlacklist(data);
    loadBlacklistStatus.textContent = "";
  } catch (err) {
    loadBlacklistStatus.textContent = " " + err.message;
    loadBlacklistStatus.className = "err";
  }
});

permanentAddButton.addEventListener("click", async () => {
  const company = permanentInput.value.trim();
  if (!company) return;
  await postBlacklistAction({ action: "add_permanent", company });
  permanentInput.value = "";
});

temporaryAddButton.addEventListener("click", async () => {
  const company = temporaryCompanyInput.value.trim();
  const until = temporaryUntilInput.value;
  if (!company || !until) {
    blacklistStatus.textContent = "Company and date are both required.";
    blacklistStatus.className = "err";
    return;
  }
  await postBlacklistAction({ action: "add_temporary", company, until });
  temporaryCompanyInput.value = "";
  temporaryUntilInput.value = "";
});

loadSettings();
