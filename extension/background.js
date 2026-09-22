const browser = globalThis.browser ?? globalThis.chrome;

const EXTENSION_DOWNLOAD_IDS_KEY = "extensionDownloadIds";

// Each export uses a fixed name; numbered variants are legacy copies to delete.
const EXTENSION_OUTPUT_PATTERNS = {
  "recorded_jobs.txt": /^recorded_jobs( \(\d+\))?\.txt$/i,
  "saved_job_application_questions.txt":
    /^saved_job_application_questions( \(\d+\))?\.txt$/i,
};

const LEGACY_OUTPUT_PATTERNS = {
  // Renamed from "saved_jobs.txt" when "save"/"saved" terminology for jobs was standardized to
  // "record"/"recorded" -- still clean up any pre-existing downloads under the old name.
  "recorded_jobs.txt": /^saved_jobs( \(\d+\))?\.txt$/i,
  "saved_job_application_questions.txt":
    /^job_application_questions( \(\d+\))?\.txt$/i,
};

async function removeDownloadFile(item) {
  if (!item || item.state !== "complete") return;

  try {
    await browser.downloads.removeFile(item.id);
  } catch (_) {
    // File may have been moved or deleted manually.
  }

  try {
    await browser.downloads.erase({ id: item.id });
  } catch (_) {}
}

async function removePriorExtensionDownloads(filename) {
  const pattern = EXTENSION_OUTPUT_PATTERNS[filename];
  if (!pattern) return;

  const stored = await browser.storage.local.get(EXTENSION_DOWNLOAD_IDS_KEY);
  const tracked = stored[EXTENSION_DOWNLOAD_IDS_KEY] || {};
  const trackedId = tracked[filename];

  const seen = new Set();
  const toRemove = [];

  if (trackedId !== undefined) {
    const [trackedItem] = await browser.downloads.search({ id: trackedId });
    if (trackedItem) {
      seen.add(trackedItem.id);
      toRemove.push(trackedItem);
    }
  }

  const matches = await browser.downloads.search({
    filenameRegex: pattern.source,
    orderBy: ["-startTime"],
  });

  for (const item of matches) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    toRemove.push(item);
  }

  const legacyPattern = LEGACY_OUTPUT_PATTERNS[filename];
  if (legacyPattern) {
    const legacyMatches = await browser.downloads.search({
      filenameRegex: legacyPattern.source,
      orderBy: ["-startTime"],
    });
    for (const item of legacyMatches) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      toRemove.push(item);
    }
  }

  for (const item of toRemove) {
    await removeDownloadFile(item);
  }
}

async function rememberExtensionDownload(filename, downloadId) {
  const stored = await browser.storage.local.get(EXTENSION_DOWNLOAD_IDS_KEY);
  const tracked = stored[EXTENSION_DOWNLOAD_IDS_KEY] || {};
  tracked[filename] = downloadId;
  await browser.storage.local.set({ [EXTENSION_DOWNLOAD_IDS_KEY]: tracked });
}

async function downloadTextFile(text, filename) {
  await removePriorExtensionDownloads(filename);

  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);

  try {
    const downloadId = await browser.downloads.download({
      url,
      filename,
      conflictAction: "overwrite",
      saveAs: false,
    });

    if (downloadId !== undefined) {
      await rememberExtensionDownload(filename, downloadId);
    }

    return { ok: downloadId !== undefined };
  } catch (err) {
    console.warn("download failed:", err);
    return { ok: false };
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

// Config for job-applyer's local extension_server.py — see options.html. The server is
// loopback-only by design; no token/auth today (see extension_server.py's AUTH docstring for
// why, and the planned replacement). Same server/settings for both cover-letter generation and
// form-field answers below.
const COVER_LETTER_SETTINGS_KEY = "coverLetterSettings";
const DEFAULT_COVER_LETTER_SERVER_URL = "http://127.0.0.1:8743";

async function getExtensionServerSettings() {
  const stored = await browser.storage.local.get(COVER_LETTER_SETTINGS_KEY);
  const settings = stored[COVER_LETTER_SETTINGS_KEY] || {};
  return {
    serverUrl: (settings.serverUrl || DEFAULT_COVER_LETTER_SERVER_URL).replace(/\/+$/, ""),
    // Undefined (never saved before) defaults to true -- matches options.js's checked-by-default
    // checkbox, so existing users who haven't touched the new setting keep today's behavior.
    autoSyncDownloads: settings.autoSyncDownloads !== false,
  };
}

async function generateCoverLetter(job) {
  const { serverUrl } = await getExtensionServerSettings();

  let res;
  try {
    res = await fetch(serverUrl + "/cover-letter", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        title: job.title,
        company: job.company,
        description: job.description || "",
        url: job.url || "",
      }),
    });
  } catch (err) {
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  return { coverLetter: data.cover_letter, docxPath: data.docx_path };
}

async function answerFields(fields) {
  const { serverUrl } = await getExtensionServerSettings();

  let res;
  try {
    res = await fetch(serverUrl + "/answer-fields", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ fields }),
    });
  } catch (err) {
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  return { answers: data.answers || [] };
}

// Import requests can otherwise hang indefinitely if the server stalls (e.g. a slow LLM call
// while the request handler is busy elsewhere) -- the sync popup needs SOME response to move
// past its "Sending to server…" stage into a definite success/failure state.
const PROCESS_EXTENSION_TIMEOUT_MS = 30_000;

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function processExtensionRequest({ recordedJobsText, savedQuestionsText, dryRun }) {
  const { serverUrl, autoSyncDownloads } = await getExtensionServerSettings();
  if (!autoSyncDownloads) {
    // Downloads (recorded_jobs.txt / saved_job_application_questions.txt) already happened in
    // content.js before this message was sent -- this setting only controls whether we also
    // import them straight into the server, so no server call is needed at all here.
    return { type: "download_only" };
  }
  const requestId = crypto.randomUUID();

  let res;
  try {
    res = await fetchWithTimeout(
      serverUrl + "/process-extension",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          request_id: requestId,
          saved_jobs_text: recordedJobsText || "",
          saved_questions_text: savedQuestionsText || "",
          dry_run: !!dryRun,
        }),
      },
      PROCESS_EXTENSION_TIMEOUT_MS
    );
  } catch (err) {
    if (err.name === "AbortError") {
      return {
        error: "Timed out waiting for " + serverUrl + " (" + (PROCESS_EXTENSION_TIMEOUT_MS / 1000) +
          "s) — the import may or may not have completed; check the server's own log before retrying.",
      };
    }
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  return data; // {type: "extension_processed", ...} or {type: "process_conflicts", ...}
}

async function resolveExtensionConflicts({ requestId, serverRequestId, resolutions }) {
  const { serverUrl } = await getExtensionServerSettings();

  let res;
  try {
    res = await fetchWithTimeout(
      serverUrl + "/process-extension/resolve",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          request_id: requestId,
          server_request_id: serverRequestId,
          resolutions: resolutions || [],
        }),
      },
      PROCESS_EXTENSION_TIMEOUT_MS
    );
  } catch (err) {
    if (err.name === "AbortError") {
      return {
        error: "Timed out waiting for " + serverUrl + " (" + (PROCESS_EXTENSION_TIMEOUT_MS / 1000) +
          "s) — the resolution may or may not have been applied; check the server's own log before retrying.",
      };
    }
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  return data; // {type: "extension_processed", ...} or {type: "conflict_resolution_timeout", ...}
}

async function getEasyApplyCompanies() {
  const { serverUrl } = await getExtensionServerSettings();

  let res;
  try {
    res = await fetchWithTimeout(
      serverUrl + "/easy-apply-companies",
      { method: "GET" },
      PROCESS_EXTENSION_TIMEOUT_MS
    );
  } catch (err) {
    if (err.name === "AbortError") {
      return { error: "Timed out waiting for " + serverUrl };
    }
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }
  return data; // {companies: [...]}
}

// chrome.cookies bypasses httpOnly (unlike document.cookie), so this picks up li_at and other
// session cookies a content script could never read directly. Shape matches what the server's
// utils/chrome_driver.py::load_cookies() expects (see extension_server.py's /profile/connect
// docstring) -- expirationDate (seconds, float, absent for session cookies) becomes an integer
// "expiry"; everything else is a passthrough.
async function getLinkedInCookies() {
  const raw = await browser.cookies.getAll({ domain: "linkedin.com" });
  return raw.map((c) => {
    const cookie = {
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      secure: !!c.secure,
    };
    if (c.expirationDate !== undefined) {
      cookie.expiry = Math.floor(c.expirationDate);
    }
    return cookie;
  });
}

// A stable per-account identity, resolved from LinkedIn's own "view my profile" redirect --
// distinct from cookies (each device gets its own session-cookie values even for the same
// account) so the server can tell "two devices, same account" apart from "two different
// accounts" (see extension_server.py's /profile/connect docstring). credentials: "include" is
// required since this is a cross-origin fetch from the background script; without it the
// request goes out cookie-less and LinkedIn has no idea who's asking.
async function getLinkedInIdentity() {
  try {
    const res = await fetch("https://www.linkedin.com/in/me/", {
      credentials: "include",
      redirect: "follow",
    });
    return res.url || null;
  } catch (err) {
    return null;
  }
}

// A single device-wide profile id, shared across every site this device ever connects (LinkedIn
// today, others later) -- NOT session-scoped: written once to browser.storage.local and reused
// from then on, surviving browser restarts. Never generated locally on a whim -- see
// connectToSite() below, which always defers to whatever id the server actually confirms.
const PROFILE_ID_KEY = "jobApplyerProfileId";

async function getStoredProfileId() {
  const stored = await browser.storage.local.get(PROFILE_ID_KEY);
  return stored[PROFILE_ID_KEY] || null;
}

async function storeProfileId(profileId) {
  await browser.storage.local.set({ [PROFILE_ID_KEY]: profileId });
}

// Purely local UI state (does the toolbar button say "Connect" or "Disconnect") -- separate from
// the profile id itself, which persists across a disconnect. Not authoritative for anything the
// server does; a stale/missing entry here just means the button shows "Connect" again, which is
// harmless (connecting is idempotent) rather than a real inconsistency to guard against.
const CONNECTED_SITES_KEY = "jobApplyerConnectedSites";

async function getConnectedSites() {
  const stored = await browser.storage.local.get(CONNECTED_SITES_KEY);
  return stored[CONNECTED_SITES_KEY] || {};
}

async function setSiteConnected(site, connected) {
  const sites = await getConnectedSites();
  if (connected) {
    sites[site] = true;
  } else {
    delete sites[site];
  }
  await browser.storage.local.set({ [CONNECTED_SITES_KEY]: sites });
}

async function connectToSite(site) {
  const { serverUrl } = await getExtensionServerSettings();

  let cookies;
  let identity = null;
  if (site === "linkedin") {
    cookies = await getLinkedInCookies();
    identity = await getLinkedInIdentity();
  } else {
    return { error: "Unsupported site: " + site };
  }
  if (!cookies.length) {
    return { error: "No cookies found for " + site + " — make sure you're logged in to it in this browser." };
  }

  const existingProfileId = await getStoredProfileId();

  let res;
  try {
    res = await fetchWithTimeout(
      serverUrl + "/profile/connect",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ site, profile_id: existingProfileId, cookies, identity }),
      },
      PROCESS_EXTENSION_TIMEOUT_MS
    );
  } catch (err) {
    if (err.name === "AbortError") {
      return { error: "Timed out waiting for " + serverUrl };
    }
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  if (data.status === "conflict") {
    // This LinkedIn account is already connected under a different profile -- nothing is stored
    // yet. The caller (content.js) is responsible for asking the user and following up with
    // RESOLVE_LINKEDIN_CONNECT_CONFLICT once they've chosen.
    return data; // {status: "conflict", pending_id, existing_profile_id}
  }

  // The server is the sole authority on this id (same reasoning as process-extension's
  // server-minted server_request_id) -- always overwrite our own copy with whatever it confirms,
  // even if it differs from what we just sent (e.g. our old id was no longer recognized).
  if (data.profile_id) {
    await storeProfileId(data.profile_id);
  }
  await setSiteConnected(site, true);
  return data; // {status: "connected", profile_id}
}

async function resolveConnectConflict(site, pendingId, choice) {
  const { serverUrl } = await getExtensionServerSettings();

  let res;
  try {
    res = await fetchWithTimeout(
      serverUrl + "/profile/connect/resolve",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ pending_id: pendingId, choice }),
      },
      PROCESS_EXTENSION_TIMEOUT_MS
    );
  } catch (err) {
    if (err.name === "AbortError") {
      return { error: "Timed out waiting for " + serverUrl };
    }
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  if (data.status === "joined" || data.status === "merged") {
    if (data.profile_id) {
      await storeProfileId(data.profile_id);
    }
    await setSiteConnected(site, true);
  }
  // "cancelled" -> nothing changes locally, same as if the connect attempt had never happened.
  return data;
}

async function disconnectSite(site) {
  const { serverUrl } = await getExtensionServerSettings();

  const profileId = await getStoredProfileId();
  if (!profileId) {
    // Nothing was ever connected server-side under any id -- just clear the stale local flag,
    // if any, rather than erroring over a no-op.
    await setSiteConnected(site, false);
    return { disconnected: false };
  }

  let res;
  try {
    res = await fetchWithTimeout(
      serverUrl + "/profile/disconnect",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ profile_id: profileId, site }),
      },
      PROCESS_EXTENSION_TIMEOUT_MS
    );
  } catch (err) {
    if (err.name === "AbortError") {
      return { error: "Timed out waiting for " + serverUrl };
    }
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  // profile_id itself is deliberately left in storage -- disconnecting a site forgets that
  // site's cookies, not this device's identity (see extension_profiles.py::disconnect_site).
  await setSiteConnected(site, false);
  return data; // {disconnected: bool}
}

browser.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "GENERATE_COVER_LETTER") {
    generateCoverLetter(msg.job || {}).then(sendResponse);
    return true;
  }

  if (msg.type === "ANSWER_FIELDS") {
    answerFields(msg.fields || []).then(sendResponse);
    return true;
  }

  if (msg.type === "PROCESS_EXTENSION") {
    processExtensionRequest(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "RESOLVE_CONFLICTS") {
    resolveExtensionConflicts(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "GET_EASY_APPLY_COMPANIES") {
    getEasyApplyCompanies().then(sendResponse);
    return true;
  }

  if (msg.type === "CONNECT_LINKEDIN") {
    connectToSite("linkedin").then(sendResponse);
    return true;
  }

  if (msg.type === "DISCONNECT_LINKEDIN") {
    disconnectSite("linkedin").then(sendResponse);
    return true;
  }

  if (msg.type === "RESOLVE_LINKEDIN_CONNECT_CONFLICT") {
    resolveConnectConflict("linkedin", msg.pendingId, msg.choice).then(sendResponse);
    return true;
  }

  if (msg.type === "IS_SITE_CONNECTED") {
    getConnectedSites().then((sites) => sendResponse({ connected: !!sites[msg.site] }));
    return true;
  }

  if (msg.type === "GET_PROFILE_ID") {
    getStoredProfileId().then((profileId) => sendResponse({ profileId }));
    return true;
  }

  if (msg.type !== "DOWNLOAD_TEXT_FILE") return;

  const filename = msg.filename;
  if (!filename) {
    sendResponse({ ok: false });
    return;
  }

  downloadTextFile(msg.text ?? "", filename)
    .then(sendResponse)
    .catch((err) => {
      console.warn("download failed:", err);
      sendResponse({ ok: false });
    });

  return true;
});
