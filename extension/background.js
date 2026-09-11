const browser = globalThis.browser ?? globalThis.chrome;

const EXTENSION_DOWNLOAD_IDS_KEY = "extensionDownloadIds";

// Each export uses a fixed name; numbered variants are legacy copies to delete.
const EXTENSION_OUTPUT_PATTERNS = {
  "saved_jobs.txt": /^saved_jobs( \(\d+\))?\.txt$/i,
  "saved_job_application_questions.txt":
    /^saved_job_application_questions( \(\d+\))?\.txt$/i,
};

const LEGACY_OUTPUT_PATTERNS = {
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

// Config for job-applyer's local extension_server.py — see options.html.
// The server is loopback-only by design and requires a bearer token; both are
// set by the user via the options page rather than hardcoded here, since the
// token is a live secret. Same server/settings for both cover-letter
// generation and form-field answers below.
const COVER_LETTER_SETTINGS_KEY = "coverLetterSettings";
const DEFAULT_COVER_LETTER_SERVER_URL = "http://127.0.0.1:8743";

async function getExtensionServerSettings() {
  const stored = await browser.storage.local.get(COVER_LETTER_SETTINGS_KEY);
  const settings = stored[COVER_LETTER_SETTINGS_KEY] || {};
  return {
    serverUrl: (settings.serverUrl || DEFAULT_COVER_LETTER_SERVER_URL).replace(/\/+$/, ""),
    token: settings.token,
  };
}

async function generateCoverLetter(job) {
  const { serverUrl, token } = await getExtensionServerSettings();
  if (!token) {
    return { error: "No API token set — configure it on the extension's options page." };
  }

  let res;
  try {
    res = await fetch(serverUrl + "/cover-letter", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
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
  const { serverUrl, token } = await getExtensionServerSettings();
  if (!token) {
    return { error: "No API token set — configure it on the extension's options page." };
  }

  let res;
  try {
    res = await fetch(serverUrl + "/answer-fields", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
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

async function processExtensionRequest({ savedJobsText, savedQuestionsText, dryRun }) {
  const { serverUrl, token } = await getExtensionServerSettings();
  if (!token) {
    return { error: "No API token set — configure it on the extension's options page." };
  }
  const requestId = crypto.randomUUID();

  let res;
  try {
    res = await fetch(serverUrl + "/process-extension", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
      },
      body: JSON.stringify({
        request_id: requestId,
        saved_jobs_text: savedJobsText || "",
        saved_questions_text: savedQuestionsText || "",
        dry_run: !!dryRun,
      }),
    });
  } catch (err) {
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  return data; // {type: "extension_processed", ...} or {type: "process_conflicts", ...}
}

async function resolveExtensionConflicts({ requestId, serverRequestId, resolutions }) {
  const { serverUrl, token } = await getExtensionServerSettings();
  if (!token) {
    return { error: "No API token set — configure it on the extension's options page." };
  }

  let res;
  try {
    res = await fetch(serverUrl + "/process-extension/resolve", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
      },
      body: JSON.stringify({
        request_id: requestId,
        server_request_id: serverRequestId,
        resolutions: resolutions || [],
      }),
    });
  } catch (err) {
    return { error: "could not reach extension server at " + serverUrl + ": " + err.message };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { error: (data && data.error) || ("server responded " + res.status) };
  }

  return data; // {type: "extension_processed", ...} or {type: "conflict_resolution_timeout", ...}
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
