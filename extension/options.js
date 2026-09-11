// options.js
const browser = globalThis.browser ?? globalThis.chrome;

const COVER_LETTER_SETTINGS_KEY = "coverLetterSettings";
const DEFAULT_COVER_LETTER_SERVER_URL = "http://127.0.0.1:8743";

const form = document.getElementById("settings-form");
const serverUrlInput = document.getElementById("server-url");
const tokenInput = document.getElementById("token");
const statusEl = document.getElementById("status");

async function loadSettings() {
    const stored = await browser.storage.local.get(COVER_LETTER_SETTINGS_KEY);
    const settings = stored[COVER_LETTER_SETTINGS_KEY] || {};
    serverUrlInput.value = settings.serverUrl || DEFAULT_COVER_LETTER_SERVER_URL;
    tokenInput.value = settings.token || "";
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const settings = {
        serverUrl: serverUrlInput.value.trim() || DEFAULT_COVER_LETTER_SERVER_URL,
        token: tokenInput.value.trim(),
    };
    await browser.storage.local.set({ [COVER_LETTER_SETTINGS_KEY]: settings });

    statusEl.textContent = "Saved.";
    setTimeout(() => {
        statusEl.textContent = "";
    }, 2000);
});

loadSettings();
