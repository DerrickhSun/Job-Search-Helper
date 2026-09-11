// popup.js
const browser = globalThis.browser ?? globalThis.chrome;

const fillFormBtn = document.getElementById("fill-form-btn");
const saveFieldsBtn = document.getElementById("save-fields-btn");
const statusEl = document.getElementById("status");

// Fields can live inside an embedded ATS iframe (Jobvite/iCIMS-style forms
// commonly are), not just the top-level page. content.js runs in every
// matching frame (all_frames in manifest.json), but tabs.sendMessage without
// a target only ever reaches the top frame — so we enumerate every frame on
// the tab and message each one individually, skipping any that don't have a
// content script (non-matching URL, not yet loaded, etc).
async function sendToAllFrames(message) {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    if (!tab) return [{ error: "No active tab." }];

    let frames;
    try {
        frames = await browser.webNavigation.getAllFrames({ tabId: tab.id });
    } catch (err) {
        console.log("[JobHelp] getAllFrames threw:", err.message);
        frames = null;
    }
    if (!frames || !frames.length) frames = [{ frameId: 0 }];
    console.log("[JobHelp] frames found:", frames.map((f) => f.frameId + ": " + f.url));

    const results = [];
    for (const frame of frames) {
        try {
            const res = await browser.tabs.sendMessage(tab.id, message, { frameId: frame.frameId });
            console.log("[JobHelp] frame", frame.frameId, "responded:", JSON.stringify(res));
            if (res) results.push(res);
        } catch (err) {
            console.log("[JobHelp] frame", frame.frameId, "(" + frame.url + ") sendMessage failed:", err.message);
        }
    }
    return results;
}

// Sums `sumKeys` across every frame's successful response. If every frame
// errored (or none responded), surfaces the first error instead of a
// fabricated all-zero result. A frame that errors (e.g. it found fields but
// couldn't reach the server) is never silently dropped just because some
// *other*, unrelated frame trivially "succeeded" with nothing to report —
// its error is carried along in `frameErrors` so the caller can still show it.
function aggregateResults(results, sumKeys) {
    const oks = results.filter((r) => r && !r.error);
    const errors = results.filter((r) => r && r.error);

    if (!oks.length) {
        return { error: (errors[0] && errors[0].error) || "Couldn't reach this page (try reloading it first)." };
    }

    const out = {};
    for (const key of sumKeys) {
        out[key] = oks.reduce((sum, r) => sum + (r[key] || 0), 0);
    }
    if (errors.length) out.frameErrors = errors.map((e) => e.error);
    return out;
}

fillFormBtn.addEventListener("click", async () => {
    fillFormBtn.disabled = true;
    statusEl.textContent = "Scanning page…";

    try {
        const res = aggregateResults(await sendToAllFrames({ type: "FILL_FORM" }), ["filled", "total", "flagged"]);

        if (res.error) {
            statusEl.textContent = "Failed: " + res.error;
            return;
        }

        if (!res.total) {
            statusEl.textContent = res.frameErrors
                ? "Found fields but couldn't process them: " + res.frameErrors[0]
                : "No fillable fields found on this page.";
            return;
        }

        let summary = "Filled " + res.filled + " of " + res.total +
            " field" + (res.total === 1 ? "" : "s") + ".";
        if (res.flagged) {
            summary += "\n" + res.flagged + " flagged for manual review.";
        }
        if (res.frameErrors) {
            summary += "\n" + res.frameErrors.length + " frame(s) errored: " + res.frameErrors[0];
        }
        statusEl.textContent = summary;
    } finally {
        fillFormBtn.disabled = false;
    }
});

saveFieldsBtn.addEventListener("click", async () => {
    saveFieldsBtn.disabled = true;
    statusEl.textContent = "Scanning page…";

    try {
        const res = aggregateResults(await sendToAllFrames({ type: "SAVE_FIELDS" }), ["saved"]);

        if (res.error) {
            statusEl.textContent = "Failed: " + res.error;
            return;
        }

        if (!res.saved) {
            statusEl.textContent = res.frameErrors
                ? "Found fields but couldn't save them: " + res.frameErrors[0]
                : "No fields found on this page.";
            return;
        }

        let summary = "Saved " + res.saved + " field" + (res.saved === 1 ? "" : "s") + ".";
        if (res.frameErrors) {
            summary += "\n" + res.frameErrors.length + " frame(s) errored: " + res.frameErrors[0];
        }
        statusEl.textContent = summary;
    } finally {
        saveFieldsBtn.disabled = false;
    }
});
