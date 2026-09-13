// options.js
const browser = globalThis.browser ?? globalThis.chrome;

const COVER_LETTER_SETTINGS_KEY = "coverLetterSettings";
const DEFAULT_COVER_LETTER_SERVER_URL = "http://127.0.0.1:8743";
// Only ever read/written here and in popup.js (to open it in a new tab) — never sent to the
// server or to the config page itself, so the page can't read or change where it's linked from.
const DEFAULT_CONFIG_PAGE_URL = "https://derrickhsun.github.io/Job-Search-Helper/";

const form = document.getElementById("settings-form");
const serverUrlInput = document.getElementById("server-url");
const tokenInput = document.getElementById("token");
const configPageUrlInput = document.getElementById("config-page-url");
const statusEl = document.getElementById("status");

async function loadSettings() {
    const stored = await browser.storage.local.get(COVER_LETTER_SETTINGS_KEY);
    const settings = stored[COVER_LETTER_SETTINGS_KEY] || {};
    serverUrlInput.value = settings.serverUrl || DEFAULT_COVER_LETTER_SERVER_URL;
    tokenInput.value = settings.token || "";
    configPageUrlInput.value = settings.configPageUrl || DEFAULT_CONFIG_PAGE_URL;
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const settings = {
        serverUrl: serverUrlInput.value.trim() || DEFAULT_COVER_LETTER_SERVER_URL,
        token: tokenInput.value.trim(),
        configPageUrl: configPageUrlInput.value.trim() || DEFAULT_CONFIG_PAGE_URL,
    };
    await browser.storage.local.set({ [COVER_LETTER_SETTINGS_KEY]: settings });

    statusEl.textContent = "Saved.";
    setTimeout(() => {
        statusEl.textContent = "";
    }, 2000);
});

loadSettings();
