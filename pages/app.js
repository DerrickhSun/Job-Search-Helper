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

pingButton.addEventListener("click", async () => {
  const settings = saveSettings();
  if (!settings.token) {
    statusEl.textContent = "Set a token above first.";
    statusEl.className = "err";
    return;
  }

  statusEl.textContent = "Contacting " + settings.serverUrl + " ...";
  statusEl.className = "";

  try {
    const res = await fetch(settings.serverUrl.replace(/\/+$/, "") + "/health", {
      method: "GET",
      headers: { "Authorization": "Bearer " + settings.token },
    });
    const data = await res.json().catch(() => null);
    if (res.ok) {
      statusEl.textContent = "OK — server responded: " + JSON.stringify(data);
      statusEl.className = "ok";
    } else {
      statusEl.textContent = "Server responded " + res.status + ": " + JSON.stringify(data);
      statusEl.className = "err";
    }
  } catch (err) {
    statusEl.textContent =
      "Could not reach " + settings.serverUrl + ": " + err.message +
      "\n(Is extension_server.py running on that address? See pages/README.md if this " +
      "page is served over https:// — Chrome's Private Network Access check can block it.)";
    statusEl.className = "err";
  }
});

loadSettings();
