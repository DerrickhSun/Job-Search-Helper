// content.js
// Uses the shared taskbar coordination module (taskbar-shared.js), which is
// loaded before this file and shares the same isolated-world scope. That module
// owns the host element and all page-shifting; we only manage our own "slot".

// MUST be unique per extension so our slot doesn't collide with the other one.
const EXT_KEY = "jobhelp";

// Slots are ordered left-to-right by ascending `order`. Our buttons should
// appear SECOND, so this must be greater than the other extension's order.
const TASKBAR_ORDER = 20;

// Uses the downloads API; prior extension exports are removed before each save.
function saveTextFile(text, filename) {
    return browser.runtime.sendMessage({
        type: "DOWNLOAD_TEXT_FILE",
        text,
        filename,
    });
}

function isLinkedInPage() {
    return location.hostname === "www.linkedin.com" || location.hostname === "linkedin.com";
}

// Mirrors job_searcher.parse_current_job_from_detail_pane: job id from URL or
// the active list row when LinkedIn does not update the address bar.
function getLinkedInJobId() {
    const url = location.href;
    let match = url.match(/currentJobId=(\d+)/i);
    if (match) return match[1];
    match = url.match(/\/jobs\/view\/(\d+)/i);
    if (match) return match[1];

    const activeRow = document.querySelector(
        "li.scaffold-layout__list-item--active[data-occludable-job-id]," +
        "li.jobs-search-results__list-item--active[data-occludable-job-id]," +
        'li[aria-current="true"][data-occludable-job-id]'
    );
    return activeRow?.getAttribute("data-occludable-job-id") || null;
}

function getLinkedInJobUrl() {
    const jobId = getLinkedInJobId();
    if (jobId) {
        return "https://www.linkedin.com/jobs/view/" + jobId + "/";
    }

    const viewLink = document.querySelector(
        'a[href*="/jobs/view/"], a[href*="currentJobId"]'
    );
    const href = viewLink?.href || "";
    if (href) {
        const fromView = href.match(/\/jobs\/view\/(\d+)/i);
        if (fromView) {
            return "https://www.linkedin.com/jobs/view/" + fromView[1] + "/";
        }
        const fromQuery = href.match(/currentJobId=(\d+)/i);
        if (fromQuery) {
            return "https://www.linkedin.com/jobs/view/" + fromQuery[1] + "/";
        }
    }

    return "";
}

function getLinkedInCompanyMarker() {
    return document.querySelector('[aria-label^="Company,"], [aria-label*="Company,"]');
}

function isLinkedInJobDisplayed() {
    if (getLinkedInJobId()) return true;
    if (getLinkedInCompanyMarker()) return true;
    return false;
}

function textFromFirstMatch(root, selectors) {
    for (const selector of selectors) {
        const el = root.querySelector(selector);
        const text = el?.textContent?.trim();
        if (text) return text;
    }
    return "";
}

function looksLikeJobMetadata(text) {
    return /applicants|Promoted|Company review|hours ago|days ago|weeks ago|months ago|·/.test(text);
}

function looksLikeLinkedInChromeText(text) {
    return /sign in|join now|join linkedin|take the next step|forgot password|agree\s*&\s*join|ai-powered|tailor my resume|am i a good fit|set alert|get notified|explore top content|evaluate your skills/i.test(text);
}

function looksLikeValidJobTitle(text) {
    if (!text || text.length > 200) return false;
    if (looksLikeJobMetadata(text)) return false;
    if (looksLikeLinkedInChromeText(text)) return false;
    return true;
}

function textFromFirstValidMatch(root, selectors) {
    for (const selector of selectors) {
        const el = root.querySelector(selector);
        const text = el?.textContent?.trim();
        if (looksLikeValidJobTitle(text)) return text;
    }
    return "";
}

function isLinkedInJobViewPage() {
    return /\/jobs\/view\/\d+/i.test(location.pathname);
}

// Standalone /jobs/view/ pages expose stable title/company in <title> and og:title
// even when the detail pane uses different markup than two-pane search.
function parseLinkedInJobFromPageTitle(pageTitle) {
    const title = (pageTitle || "").trim();
    if (!title) return null;

    const hiring = title.match(/^(.+?) hiring (.+?) in .+? \| LinkedIn$/i);
    if (hiring) {
        return { company: hiring[1].trim(), jobTitle: hiring[2].trim() };
    }

    const simple = title.match(/^(.+?) \| LinkedIn$/i);
    if (simple) {
        return { company: "", jobTitle: simple[1].trim() };
    }

    return null;
}

function parseLinkedInJobFromDocumentTitle() {
    const fromDocument = parseLinkedInJobFromPageTitle(document.title);
    if (fromDocument) return fromDocument;

    const ogTitle = document.querySelector('meta[property="og:title"]')?.content;
    return parseLinkedInJobFromPageTitle(ogTitle);
}

function textFromLinkedInDisplayBlock(block) {
    const selectors = [":scope > p", ":scope > h1", ":scope > h2", "h1", "h2"];
    for (const selector of selectors) {
        const el = block.querySelector(selector);
        if (!el || el.querySelector('a[href*="/company/"]')) continue;

        const text = el.textContent.trim();
        if (!looksLikeValidJobTitle(text)) continue;
        return text;
    }

    return null;
}

// LinkedIn's newer UI uses obfuscated classes; stable hooks are aria-label and
// data-display-contents. Title sits in the first simple block after company.
function getLinkedInJobTitleFromModernUI() {
    const blocks = [...document.querySelectorAll('[data-display-contents="true"]')];
    const companyIdx = blocks.findIndex((b) => b.querySelector('[aria-label*="Company,"]'));
    if (companyIdx < 0) return null;

    for (let i = companyIdx + 1; i < blocks.length; i++) {
        const block = blocks[i];
        if (block.querySelector('[aria-label*="Company,"]')) continue;

        const text = textFromLinkedInDisplayBlock(block);
        if (text) return text;
    }

    return null;
}

function getLinkedInCompanyFromModernUI() {
    const marker = getLinkedInCompanyMarker();
    if (!marker) return null;

    const label = marker.getAttribute("aria-label")?.trim() || "";
    const fromLabel = label.match(/^Company,\s*(.+)$/i);
    if (fromLabel) return fromLabel[1].trim();

    const link = marker.closest('[data-display-contents="true"]')
        ?.querySelector('a[href*="/company/"]');
    return link?.textContent?.trim() || null;
}

// Selectors aligned with job_searcher.py (SEL + parse_current_job_from_detail_pane).
function getLinkedInJobCompany() {
    if (!isLinkedInJobDisplayed()) return null;

    if (isLinkedInJobViewPage()) {
        const fromPageTitle = parseLinkedInJobFromDocumentTitle();
        if (fromPageTitle?.company) return fromPageTitle.company;
    }

    const modernCompany = getLinkedInCompanyFromModernUI();
    if (modernCompany) return modernCompany;

    const detailCompany = textFromFirstMatch(document, [
        ".job-details-jobs-unified-top-card__company-name a",
        ".jobs-unified-top-card__company-name a",
        ".jobs-unified-top-card__company-name",
        "a[class*='company-name']",
    ]);
    if (detailCompany) return detailCompany;

    const activeRow = document.querySelector(
        "li.scaffold-layout__list-item--active," +
        "li.jobs-search-results__list-item--active," +
        'li[aria-current="true"]'
    );
    if (activeRow) {
        const rowCompany = textFromFirstMatch(activeRow, [
            '[class*="job-card-job-posting-card-wrapper__company-name"]',
            '[class*="job-card-job-posting-card-wrapper__primary-description"]',
            ".artdeco-entity-lockup__subtitle span[dir='ltr']",
            ".artdeco-entity-lockup__subtitle span",
        ]);
        if (rowCompany) return rowCompany;
    }

    if (isLinkedInJobViewPage()) {
        const companyLink = document.querySelector("main a[href*='/company/']");
        const linkText = companyLink?.textContent?.trim();
        if (linkText) return linkText;
    }

    return null;
}

// Selectors aligned with job_searcher.py (SEL + parse_current_job_from_detail_pane).
function getLinkedInJobTitle() {
    if (!isLinkedInJobDisplayed()) return null;

    if (isLinkedInJobViewPage()) {
        const fromPageTitle = parseLinkedInJobFromDocumentTitle();
        if (fromPageTitle?.jobTitle) return fromPageTitle.jobTitle;
    }

    const modernTitle = getLinkedInJobTitleFromModernUI();
    if (modernTitle) return modernTitle;

    const detailTitle = textFromFirstValidMatch(document, [
        ".jobs-unified-top-card__job-title",
        ".jobs-details-top-card__title-text",
        "h1.jobs-unified-top-card__job-title",
        "div[class*='jobs-details-top-card'] h1",
        "h1[class*='job-title']",
        "main h1",
        "h1.top-card-layout__title",
        "h1.topcard__title",
        ".top-card-layout__title",
        "main h2",
        "main [role='heading'][aria-level='1']",
        "div[class*='jobs-details'] h1",
        "div[class*='jobs-details'] h2",
    ]);
    if (detailTitle) return detailTitle;

    const activeRow = document.querySelector(
        "li.scaffold-layout__list-item--active," +
        "li.jobs-search-results__list-item--active," +
        'li[aria-current="true"]'
    );
    if (activeRow) {
        const rowTitle = textFromFirstValidMatch(activeRow, [
            '[class*="job-card-job-posting-card-wrapper__title"]',
            "strong",
        ]);
        if (rowTitle) return rowTitle;

        const link = activeRow.querySelector('a[href*="/jobs/view/"], a[href*="currentJobId"]');
        const ariaLabel = link?.getAttribute("aria-label")?.trim();
        if (looksLikeValidJobTitle(ariaLabel)) return ariaLabel;
    }

    const h1 = document.querySelector("main h1, h1")?.textContent?.trim();
    if (looksLikeValidJobTitle(h1)) return h1;

    return null;
}

function getJobForSave() {
    if (!isLinkedInPage()) return null;

    const title = getLinkedInJobTitle();
    if (!title) return null;

    return {
        title,
        company: getLinkedInJobCompany() || "",
        url: getLinkedInJobUrl(),
    };
}

// Selectors aligned with job_searcher.py's SEL["job_description"]. Newer SDUI markup uses
// hashed/atomic class names (no stable class to hook), so prefer the stable data-testid /
// componentkey attributes; .jobs-description__content is legacy fallback. Unlike the Python
// scraper we don't scroll+wait for lazy-loaded sections — by the time someone clicks "Generate
// Cover Letter" they've already been looking at the page, so it's normally already rendered.
//
// IMPORTANT: expandable-text-box (and its "…more" toggle button) also appears on "Trending
// employee content" / promoted feed cards on the same job details page — see
// job_searcher.py's SEL["job_description"] comment. Root-scope the search to a real job
// description container first, and skip anything nested inside a feed/post/profile link.
const LINKEDIN_JOB_DESCRIPTION_ROOT_SELECTORS = [
    'div[componentkey^="JobDetails_AboutTheJob_"]',
    ".jobs-description__content",
    ".jobs-description-content",
    ".jobs-box__html-content",
    "#job-details",
];

const LINKEDIN_JOB_DESCRIPTION_SELECTORS = [
    'span[data-testid="expandable-text-box"]',
    'div[componentkey^="JobDetails_AboutTheJob_"]',
    ".jobs-description__content",
];

function elementInsideFeedOrProfileLink(el) {
    return !!el.closest(
        'a[href*="/feed/"], a[href*="/feed/update"], a[href*="urn:li:activity"], a[href*="/in/"]'
    );
}

function findLinkedInJobDescriptionElement() {
    for (const rootSelector of LINKEDIN_JOB_DESCRIPTION_ROOT_SELECTORS) {
        const root = document.querySelector(rootSelector);
        if (!root || elementInsideFeedOrProfileLink(root)) continue;
        for (const selector of LINKEDIN_JOB_DESCRIPTION_SELECTORS) {
            const el = root.matches(selector) ? root : root.querySelector(selector);
            if (el && !elementInsideFeedOrProfileLink(el)) return el;
        }
    }
    for (const selector of LINKEDIN_JOB_DESCRIPTION_SELECTORS) {
        const el = document.querySelector(selector);
        if (el && !elementInsideFeedOrProfileLink(el)) return el;
    }
    return null;
}

function getLinkedInJobDescription() {
    // No "…more" click here — innerText/textContent already holds the full un-clamped text
    // (the "…more" clamp is purely visual/CSS), and clicking is both unnecessary and unsafe:
    // that click used to run document-wide, so it could also hit a "…more" toggle on an
    // unrelated feed/promoted post elsewhere on the page and trigger that post's own click
    // handler, navigating the tab away to a /feed/ or /posts/ URL. Mirrors
    // job_searcher._read_job_description_panel / _dom_text, which reads textContent directly
    // for the same reason.
    const el = findLinkedInJobDescriptionElement();
    if (!el) return "";
    return (el.innerText || el.textContent || "").trim();
}

function getJobForCoverLetter() {
    const job = getJobForSave();
    if (!job) return null;

    return {
        ...job,
        description: getLinkedInJobDescription(),
    };
}

function formatSavedJobLabel(job) {
    const title = job.title || "";
    const company = job.company || "";
    if (company) return company + ", " + title;
    return title;
}

function formatSavedJobDate(job) {
    const ts = job.savedAt;
    if (!ts) return "";

    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";

    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return year + "-" + month + "-" + day;
}

function formatSavedJobDownloadLine(job) {
    const company = job.company || "";
    const date = formatSavedJobDate(job);
    const title = job.title || "";
    const url = job.url || "";
    return company + ", " + date + ", " + title + ", " + url;
}

function savedJobKey(job) {
    return (job.company || "") + "\0" + (job.title || "");
}

function trimLinkedInFieldLabel(text) {
    return (text || "").trim().replace(/\s*\*+\s*$/, "").trim();
}

// Collapse LinkedIn's aria-hidden + visually-hidden duplicate-text accessibility pattern:
// newer fb-dash-form-element labels/legends render the question text twice — once in an
// aria-hidden="true" span (visible copy) and once in a visually-hidden span (screen-reader
// copy, taken out of flow via position:absolute). Because that second span is out-of-flow,
// innerText inserts a line break around it, yielding "Question?\nQuestion?" instead of one
// copy. Collapse that back down. Mirrors form_filler._dedupe_repeated_label_text.
function dedupeRepeatedLabelText(text) {
    const t = (text || "").trim();
    if (!t) return t;
    const lines = t.split("\n").map((s) => s.trim()).filter(Boolean);
    if (lines.length >= 2 && new Set(lines).size === 1) return lines[0];
    return t;
}

// Prefer innerText (rendered spacing) over textContent — LinkedIn often splits
// question copy across sibling nodes with no whitespace between them.
function getLinkedInElementText(el) {
    if (!el) return "";
    const raw = typeof el.innerText === "string" ? el.innerText : (el.textContent || "");
    return dedupeRepeatedLabelText(raw).replace(/\s+/g, " ").trim();
}

function trimLinkedInFieldLabelFromElement(el) {
    return trimLinkedInFieldLabel(getLinkedInElementText(el));
}

function isVisibleElement(el) {
    if (!el) return false;
    if (el.closest('[aria-hidden="true"]')) return false;

    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;

    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 || el.getClientRects().length > 0;
}

function isLinkedInFormControlVisible(el) {
    if (!el || el.disabled) return false;
    if (el.type === "hidden") return false;
    return isVisibleElement(el);
}

function collectSearchRoots(start) {
    const roots = [start];
    const queue = [start];
    while (queue.length) {
        const node = queue.shift();
        node.querySelectorAll("*").forEach((el) => {
            if (el.shadowRoot) {
                roots.push(el.shadowRoot);
                queue.push(el.shadowRoot);
            }
        });
    }
    return roots;
}

function queryAllInDocument(selector) {
    const seen = new Set();
    const results = [];
    for (const root of collectSearchRoots(document)) {
        root.querySelectorAll(selector).forEach((el) => {
            if (seen.has(el)) return;
            seen.add(el);
            results.push(el);
        });
    }
    return results;
}

function findLinkedInEasyApplyMarker() {
    const selectors = [
        '[data-test-single-line-text-form-component]',
        '[data-test-multiline-text-form-component]',
        'input[id*="easyApplyFormElement"]',
        'textarea[id*="easyApplyFormElement"]',
        'select[id*="easyApplyFormElement"]',
        'fieldset[data-test-form-builder-radio-button-form-component="true"]',
    ];
    for (const selector of selectors) {
        const el = document.querySelector(selector);
        if (el && isVisibleElement(el)) return el;
    }

    for (const selector of selectors) {
        const matches = queryAllInDocument(selector);
        const visible = matches.find((el) => isVisibleElement(el));
        if (visible) return visible;
    }

    return null;
}

function getLinkedInApplicationRoot() {
    const containerSelectors = [
        ".jobs-easy-apply-modal",
        ".jobs-easy-apply-content",
        '[data-test-modal][class*="easy-apply"]',
    ];

    for (const selector of containerSelectors) {
        const el = document.querySelector(selector);
        if (el && isVisibleElement(el)) return el;
    }

    const marker = findLinkedInEasyApplyMarker();
    if (!marker) return null;

    const dialog = marker.closest(
        '.jobs-easy-apply-modal, .jobs-easy-apply-content, [role="dialog"], .artdeco-modal'
    );
    if (dialog && isVisibleElement(dialog)) return dialog;

    const formBlocks = queryAllInDocument("[data-test-form-element]").filter((block) => {
        return isVisibleElement(block) && isLinkedInEasyApplyFormBlock(block);
    });
    if (!formBlocks.length) return null;

    let ancestor = formBlocks[0];
    while (ancestor) {
        if (formBlocks.every((block) => ancestor.contains(block))) {
            return ancestor;
        }
        ancestor = ancestor.parentElement;
    }

    return formBlocks[0];
}

function getLinkedInEasyApplyModal() {
    return getLinkedInApplicationRoot();
}

function isLinkedInApplicationOpen() {
    return !!getLinkedInApplicationRoot();
}

function isLinkedInEasyApplyFormBlock(block) {
    if (block.querySelector('[id*="easyApplyFormElement"]')) return true;
    if (block.querySelector("[data-test-single-line-text-form-component]")) return true;
    if (block.querySelector("[data-test-multiline-text-form-component]")) return true;
    if (block.querySelector('[data-test-form-builder-radio-button-form-component]')) return true;
    if (block.querySelector('fieldset[data-test-form-builder-radio-button-form-component="true"]')) return true;
    if (block.closest(".jobs-easy-apply-modal, .jobs-easy-apply-content")) return true;
    return false;
}

function getLinkedInFormFieldLabel(element, scope) {
    const searchRoot = scope || element?.ownerDocument || document;

    const elId = element.id;
    if (elId) {
        const label = searchRoot.querySelector('label[for="' + CSS.escape(elId) + '"]');
        const fromLabel = trimLinkedInFieldLabelFromElement(label);
        if (fromLabel) return fromLabel;
    }

    const aria = trimLinkedInFieldLabel(element.getAttribute("aria-label"));
    if (aria) return aria;

    const placeholder = (element.getAttribute("placeholder") || "").trim();
    if (placeholder) return placeholder;

    return "";
}

function getLabelFromFormElementBlock(block, control) {
    if (control?.id) {
        const scopedLabel = block.querySelector('label[for="' + CSS.escape(control.id) + '"]');
        const scopedText = trimLinkedInFieldLabelFromElement(scopedLabel);
        if (scopedText) return scopedText;
    }

    for (const selector of [
        "label.artdeco-text-input--label",
        "[data-test-form-builder-radio-button-form-component__title]",
        "legend .fb-dash-form-element__label",
        "legend",
        "label",
    ]) {
        const label = block.querySelector(selector);
        const text = trimLinkedInFieldLabelFromElement(label);
        if (text) return text;
    }

    return getLinkedInFormFieldLabel(control, block);
}

function getLinkedInRadioFieldsetLabel(fieldset) {
    const selectors = [
        "[data-test-form-builder-radio-button-form-component__title]",
        "legend .fb-dash-form-element__label",
        "legend",
    ];
    for (const selector of selectors) {
        const el = fieldset.querySelector(selector);
        const text = trimLinkedInFieldLabelFromElement(el);
        if (text) return text;
    }
    return "";
}

function getLinkedInRadioFieldsetAnswer(fieldset) {
    const selected = fieldset.querySelector('input[type="radio"]:checked');
    if (!selected) return "";

    const rid = selected.id;
    if (rid) {
        const lab = fieldset.querySelector('label[for="' + CSS.escape(rid) + '"]') ||
            document.querySelector('label[for="' + CSS.escape(rid) + '"]');
        const labelText = getLinkedInElementText(lab);
        if (labelText) return labelText;
    }

    const opt = selected.closest("[data-test-text-selectable-option]");
    if (opt) {
        const lab = opt.querySelector("[data-test-text-selectable-option__label]");
        if (lab) {
            const attr = lab.getAttribute("data-test-text-selectable-option__label");
            if (attr?.trim()) return attr.trim();
            const text = getLinkedInElementText(lab);
            if (text) return text;
        }
    }

    return (selected.value || "").trim();
}

function getLinkedInSelectAnswer(select) {
    const opt = select.options[select.selectedIndex];
    if (!opt) return "";
    const text = getLinkedInElementText(opt);
    if (text && !/^(select an option|select)$/i.test(text)) return text;
    return (opt.value || "").trim();
}

function getLinkedInRadioGroupAnswer(modal, name) {
    const selected = modal.querySelector(
        'input[type="radio"][name="' + CSS.escape(name) + '"]:checked'
    );
    if (!selected) return "";

    const rid = selected.id;
    if (rid) {
        const lab = modal.querySelector('label[for="' + CSS.escape(rid) + '"]');
        const labelText = getLinkedInElementText(lab);
        if (labelText) return labelText;
    }

    return (selected.value || "").trim();
}

function getLinkedInRadioGroupLabel(modal, firstRadio) {
    try {
        const fieldset = firstRadio.closest("fieldset");
        if (fieldset) {
            const fromFieldset = getLinkedInRadioFieldsetLabel(fieldset);
            if (fromFieldset) return fromFieldset;
        }
    } catch (_) {
        // ignore
    }

    const wrap = firstRadio.closest(
        ".jobs-easy-apply-form-element, [class*='fb-dash']"
    );
    if (wrap) {
        const text = getLinkedInElementText(wrap);
        if (text) return text;
    }

    return nameFromRadioGroup(firstRadio.getAttribute("name") || "");
}

function nameFromRadioGroup(name) {
    return (name || "").replace(/[_-]+/g, " ").trim();
}

function collectLinkedInFormElementBlocks(root) {
    const blocks = [];
    const seen = new Set();

    function addBlock(block) {
        if (!block || seen.has(block)) return;
        if (!isLinkedInEasyApplyFormBlock(block)) return;
        if (!isVisibleElement(block)) return;
        seen.add(block);
        blocks.push(block);
    }

    if (root) {
        root.querySelectorAll("[data-test-form-element]").forEach(addBlock);
    }

    if (!blocks.length) {
        queryAllInDocument("[data-test-form-element]").forEach(addBlock);
    }

    return blocks;
}

function scanLinkedInFormElementBlock(block) {
    const fieldset = block.querySelector(
        'fieldset[data-test-form-builder-radio-button-form-component="true"]'
    );
    if (fieldset) {
        return {
            question: getLabelFromFormElementBlock(block, fieldset) ||
                getLinkedInRadioFieldsetLabel(fieldset),
            answer: getLinkedInRadioFieldsetAnswer(fieldset),
            fieldType: "radio",
        };
    }

    const textarea = block.querySelector("textarea");
    if (textarea && isLinkedInFormControlVisible(textarea)) {
        return {
            question: getLabelFromFormElementBlock(block, textarea),
            answer: textarea.value,
            fieldType: "textarea",
        };
    }

    const select = block.querySelector("select");
    if (select && isLinkedInFormControlVisible(select)) {
        return {
            question: getLabelFromFormElementBlock(block, select),
            answer: getLinkedInSelectAnswer(select),
            fieldType: "select",
        };
    }

    const input = block.querySelector(
        "input.artdeco-text-input--input, " +
        "input[type='text'], input[type='number'], input[type='tel'], " +
        'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"])'
    );
    if (input && isLinkedInFormControlVisible(input)) {
        return {
            question: getLabelFromFormElementBlock(block, input),
            answer: input.value,
            fieldType: "text",
        };
    }

    return null;
}

// Mirrors form_filler.py field selectors; prefers LinkedIn data-test-form-element blocks.
function scanLinkedInApplicationFields(root) {
    const pairs = [];
    const seenQuestions = new Set();
    const handledRadioNames = new Set();

    function addPair(question, answer, fieldType) {
        const q = (question || "").trim();
        if (!q || seenQuestions.has(q)) return;
        seenQuestions.add(q);
        pairs.push({
            question: q,
            answer: (answer || "").trim(),
            fieldType: fieldType || "",
        });
    }

    for (const block of collectLinkedInFormElementBlocks(root)) {
        const scanned = scanLinkedInFormElementBlock(block);
        if (scanned) addPair(scanned.question, scanned.answer, scanned.fieldType);
    }

    if (pairs.length) return pairs;

    const modal = root || document;
    modal.querySelectorAll(
        "input[type='text'], input[type='number'], input[type='tel'], input.artdeco-text-input--input"
    ).forEach((el) => {
        if (!isLinkedInFormControlVisible(el)) return;
        if (!el.id?.includes("easyApplyFormElement")) return;
        addPair(getLinkedInFormFieldLabel(el, modal), el.value, "text");
    });

    modal.querySelectorAll("textarea").forEach((el) => {
        if (!isLinkedInFormControlVisible(el)) return;
        if (!el.id?.includes("easyApplyFormElement")) return;
        addPair(getLinkedInFormFieldLabel(el, modal), el.value, "textarea");
    });

    modal.querySelectorAll("select").forEach((el) => {
        if (!isLinkedInFormControlVisible(el)) return;
        if (!el.id?.includes("easyApplyFormElement")) return;
        addPair(getLinkedInFormFieldLabel(el, modal), getLinkedInSelectAnswer(el), "select");
    });

    modal.querySelectorAll(
        'fieldset[data-test-form-builder-radio-button-form-component="true"]'
    ).forEach((fieldset) => {
        const radios = fieldset.querySelectorAll('input[type="radio"]');
        if (!radios.length) return;
        const name = radios[0].getAttribute("name");
        if (name) handledRadioNames.add(name);
        addPair(
            getLinkedInRadioFieldsetLabel(fieldset),
            getLinkedInRadioFieldsetAnswer(fieldset),
            "radio"
        );
    });

    const radiosByName = new Map();
    modal.querySelectorAll('input[type="radio"]').forEach((radio) => {
        if (!isLinkedInFormControlVisible(radio)) return;
        const name = radio.getAttribute("name");
        if (!name || handledRadioNames.has(name)) return;
        if (!radiosByName.has(name)) radiosByName.set(name, radio);
    });

    for (const [name, firstRadio] of radiosByName) {
        addPair(
            getLinkedInRadioGroupLabel(modal, firstRadio),
            getLinkedInRadioGroupAnswer(modal, name),
            "radio"
        );
    }

    return pairs;
}

const browser = globalThis.browser ?? globalThis.chrome;

const SAVED_JOBS_KEY = "savedJobs";
const SAVED_APPLICATION_QUESTIONS_KEY = "savedApplicationQuestions";
const SAVED_MENU_HOVER_CLOSE_MS = 350;
const SAVED_MENU_VIEWPORT_MARGIN = 8;
const SAVED_QUESTION_PREVIEW_LENGTH = 30;
const SAVE_BUTTON_REFRESH_MS = 1000;
let savedMenuHoverCloseTimer = null;
let questionsMenuHoverCloseTimer = null;
let saveButtonRefreshTimer = null;

function isLinkedInApplicationFormOpen() {
    return !!(getLinkedInApplicationRoot() || findLinkedInEasyApplyMarker());
}

function getJobContextForQuestions() {
    return getJobForSave() || {
        title: getLinkedInJobTitle() || "",
        company: getLinkedInJobCompany() || "",
        url: getLinkedInJobUrl() || "",
    };
}

function updateSaveButtonState(saveBtn) {
    const job = getJobForSave();
    if (job) {
        saveBtn.disabled = false;
        saveBtn.textContent = "Save job";
    } else {
        saveBtn.disabled = true;
        saveBtn.textContent = "No job found";
    }
}

function updateSaveQuestionsButtonState(saveQuestionsBtn) {
    if (isLinkedInApplicationFormOpen()) {
        saveQuestionsBtn.disabled = false;
        saveQuestionsBtn.textContent = "Save questions";
    } else {
        saveQuestionsBtn.disabled = true;
        saveQuestionsBtn.textContent = "No form open";
    }
}

// Set while a generate request is in flight so the polling refresh below
// doesn't clobber the "Generating…"/"Copied…"/error text with "Generate
// cover letter" mid-request.
let coverLetterBusy = false;

function updateCoverLetterButtonState(coverLetterBtn) {
    if (!coverLetterBtn || coverLetterBusy) return;
    if (getJobForSave()) {
        coverLetterBtn.disabled = false;
        coverLetterBtn.textContent = "Generate cover letter";
    } else {
        coverLetterBtn.disabled = true;
        coverLetterBtn.textContent = "No job found";
    }
}

function stopSaveButtonRefresh() {
    if (saveButtonRefreshTimer) {
        clearInterval(saveButtonRefreshTimer);
        saveButtonRefreshTimer = null;
    }
}

function startSaveButtonsRefresh(saveBtn, saveQuestionsBtn, coverLetterBtn) {
    stopSaveButtonRefresh();
    const refresh = () => {
        updateSaveButtonState(saveBtn);
        updateSaveQuestionsButtonState(saveQuestionsBtn);
        updateCoverLetterButtonState(coverLetterBtn);
    };
    refresh();
    saveButtonRefreshTimer = setInterval(refresh, SAVE_BUTTON_REFRESH_MS);
}

function isInsideSavedMenu(target, menuRoot, menu) {
    if (!target) return false;
    return target === menuRoot || menuRoot.contains(target) ||
        target === menu || menu.contains(target);
}

function isInsideQuestionsMenu(target, menuRoot, menu) {
    if (!target) return false;
    return target === menuRoot || menuRoot.contains(target) ||
        target === menu || menu.contains(target);
}

function renderSavedQuestionsMenu(menu, questions, menuRoot) {
    menu.replaceChildren();
    if (!questions.length) {
        const empty = document.createElement("li");
        empty.className = "jobhelp-questions-empty";
        empty.textContent = "No questions saved yet";
        menu.appendChild(empty);
        return;
    }

    for (const entry of questions) {
        const item = document.createElement("li");
        item.className = "jobhelp-questions-item";

        const fullQuestion = entry.question || "";
        const preview = formatSavedQuestionPreview(fullQuestion);

        const title = document.createElement("span");
        title.className = "jobhelp-questions-item-title";
        title.textContent = preview;
        title.title = fullQuestion;

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "jobhelp-saved-remove";
        removeBtn.setAttribute("aria-label", "Remove " + fullQuestion);
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", (event) => {
            event.stopPropagation();
            removeSavedApplicationQuestion(entry).then(() => {
                getSavedApplicationQuestions().then((updated) => {
                    const btn = menuRoot?.querySelector(".jobhelp-questions-count");
                    if (!updated.length) {
                        closeSavedQuestionsMenu(menuRoot);
                    }
                    if (btn) {
                        updateSavedQuestionsCountBtn(btn, menuRoot);
                    } else if (!updated.length) {
                        return;
                    } else {
                        renderSavedQuestionsMenu(menu, updated, menuRoot);
                        if (!menu.hidden && menuRoot) {
                            positionSavedQuestionsMenu(menuRoot, menu);
                        }
                    }
                });
            });
        });

        item.appendChild(title);
        item.appendChild(removeBtn);
        menu.appendChild(item);
    }
}

function clearQuestionsMenuHoverCloseTimer() {
    if (questionsMenuHoverCloseTimer) {
        clearTimeout(questionsMenuHoverCloseTimer);
        questionsMenuHoverCloseTimer = null;
    }
}

function scheduleQuestionsMenuHoverClose(menuRoot) {
    clearQuestionsMenuHoverCloseTimer();
    questionsMenuHoverCloseTimer = setTimeout(() => {
        questionsMenuHoverCloseTimer = null;
        closeSavedQuestionsMenu(menuRoot);
    }, SAVED_MENU_HOVER_CLOSE_MS);
}

function resetSavedQuestionsMenuPosition(menu) {
    menu.style.left = "";
    menu.style.top = "";
    menu.style.visibility = "";
}

function positionSavedQuestionsMenu(menuRoot, menu) {
    const listBtn = menuRoot.querySelector("button");
    if (!listBtn) return;

    menu.hidden = false;
    menu.style.visibility = "hidden";

    const btnRect = listBtn.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const margin = SAVED_MENU_VIEWPORT_MARGIN;

    let left = btnRect.left;
    let top = btnRect.bottom;

    if (left + menuRect.width > window.innerWidth - margin) {
        left = Math.max(margin, window.innerWidth - menuRect.width - margin);
    }
    if (left < margin) {
        left = margin;
    }

    if (top + menuRect.height > window.innerHeight - margin) {
        top = Math.max(margin, btnRect.top - menuRect.height);
    }

    menu.style.left = left + "px";
    menu.style.top = top + "px";
    menu.style.visibility = "";
}

function closeSavedQuestionsMenu(menuRoot) {
    const menu = menuRoot.querySelector(".jobhelp-questions-menu");
    clearQuestionsMenuHoverCloseTimer();

    if (menu) {
        menu.hidden = true;
        resetSavedQuestionsMenuPosition(menu);
    }
}

function openSavedQuestionsMenu(menuRoot) {
    const menu = menuRoot.querySelector(".jobhelp-questions-menu");
    if (!menu) return;

    clearQuestionsMenuHoverCloseTimer();

    getSavedApplicationQuestions().then((questions) => {
        if (!questions.length) return;

        renderSavedQuestionsMenu(menu, questions, menuRoot);
        positionSavedQuestionsMenu(menuRoot, menu);
    });
}

async function getSavedJobs() {
    const stored = await browser.storage.local.get(SAVED_JOBS_KEY);
    return stored[SAVED_JOBS_KEY] || [];
}

async function addSavedJob(job) {
    const savedJobs = await getSavedJobs();
    const entry = {
        title: job.title,
        company: job.company || "",
        url: job.url || "",
        savedAt: Date.now(),
    };
    const key = savedJobKey(entry);
    const withoutDup = savedJobs.filter((saved) => savedJobKey(saved) !== key);
    withoutDup.unshift(entry);
    await browser.storage.local.set({ [SAVED_JOBS_KEY]: withoutDup });
}

async function removeSavedJob(job) {
    const savedJobs = await getSavedJobs();
    const key = savedJobKey(job);
    const filtered = savedJobs.filter((saved) => savedJobKey(saved) !== key);
    await browser.storage.local.set({ [SAVED_JOBS_KEY]: filtered });
}

async function clearSavedJobs() {
    await browser.storage.local.set({ [SAVED_JOBS_KEY]: [] });
}

function formatSavedJobsCountLabel(count) {
    if (!count) return "No jobs saved";
    if (count === 1) return "1 job saved";
    return count + " jobs saved";
}

function updateSavedJobsCountBtn(btn, menuRoot) {
    getSavedJobs().then((jobs) => {
        btn.textContent = formatSavedJobsCountLabel(jobs.length);
        btn.title = jobs.length
            ? "Hover to preview saved jobs; click to clear"
            : "Save a job to add it here";

        if (!menuRoot) return;

        const menu = menuRoot.querySelector(".jobhelp-saved-menu");
        if (!jobs.length) {
            closeSavedJobsMenu(menuRoot);
            return;
        }
        if (menu && !menu.hidden) {
            renderSavedJobsMenu(menu, jobs, menuRoot);
            positionSavedJobsMenu(menuRoot, menu);
        }
    });
}

async function getSavedApplicationQuestions() {
    const stored = await browser.storage.local.get(SAVED_APPLICATION_QUESTIONS_KEY);
    return stored[SAVED_APPLICATION_QUESTIONS_KEY] || [];
}

function applicationQuestionKey(entry) {
    return (
        (entry.company || "") + "\0" +
        (entry.title || "") + "\0" +
        (entry.question || "")
    );
}

async function addSavedApplicationQuestions(pairs, job) {
    if (!pairs.length) return;

    const savedQuestions = await getSavedApplicationQuestions();
    const byKey = new Map(
        savedQuestions.map((entry) => [applicationQuestionKey(entry), entry])
    );
    const savedAt = Date.now();

    for (const pair of pairs) {
        const entry = {
            question: pair.question,
            answer: pair.answer,
            fieldType: pair.fieldType || "",
            company: job.company || "",
            title: job.title || "",
            url: job.url || "",
            savedAt,
        };
        byKey.set(applicationQuestionKey(entry), entry);
    }

    await browser.storage.local.set({
        [SAVED_APPLICATION_QUESTIONS_KEY]: [...byKey.values()],
    });
}

async function clearSavedApplicationQuestions() {
    await browser.storage.local.set({ [SAVED_APPLICATION_QUESTIONS_KEY]: [] });
}

async function removeSavedApplicationQuestion(entry) {
    const savedQuestions = await getSavedApplicationQuestions();
    const key = applicationQuestionKey(entry);
    const filtered = savedQuestions.filter(
        (saved) => applicationQuestionKey(saved) !== key
    );
    await browser.storage.local.set({ [SAVED_APPLICATION_QUESTIONS_KEY]: filtered });
}

function formatApplicationQuestionsDownloadLine(entry) {
    const headerParts = [];
    if (entry.company) headerParts.push(entry.company);
    if (entry.title) headerParts.push(entry.title);
    const header = headerParts.length ? headerParts.join(", ") : "Application";
    const lines = [header];
    if (entry.url) lines.push("URL: " + entry.url);
    lines.push("Q: " + (entry.question || ""));
    lines.push("A: " + (entry.answer || ""));
    return lines.join("\n");
}

function formatApplicationQuestionsDownloadText(questions) {
    return questions.map((entry) => formatApplicationQuestionsDownloadLine(entry)).join("\n\n");
}

function formatSavedQuestionsCountLabel(count) {
    if (!count) return "No questions saved";
    if (count === 1) return "1 question saved";
    return count + " questions saved";
}

function formatSavedQuestionPreview(question) {
    const text = (question || "").trim();
    if (text.length <= SAVED_QUESTION_PREVIEW_LENGTH) return text;
    return text.slice(0, SAVED_QUESTION_PREVIEW_LENGTH) + "…";
}

function updateSavedQuestionsCountBtn(btn, menuRoot) {
    getSavedApplicationQuestions().then((questions) => {
        btn.textContent = formatSavedQuestionsCountLabel(questions.length);
        btn.title = questions.length
            ? "Hover to preview questions; click to clear"
            : "Save a job while Easy Apply is open to capture questions";

        if (!menuRoot) return;

        const menu = menuRoot.querySelector(".jobhelp-questions-menu");
        if (!questions.length) {
            closeSavedQuestionsMenu(menuRoot);
            return;
        }
        if (menu && !menu.hidden) {
            renderSavedQuestionsMenu(menu, questions, menuRoot);
            positionSavedQuestionsMenu(menuRoot, menu);
        }
    });
}

async function downloadAllSavedJobs() {
    const jobs = await getSavedJobs();
    const text = jobs.map((job) => formatSavedJobDownloadLine(job)).join("\n");
    await saveTextFile(text, "saved_jobs.txt");

    const questions = await getSavedApplicationQuestions();
    if (questions.length) {
        await saveTextFile(
            formatApplicationQuestionsDownloadText(questions),
            "saved_job_application_questions.txt"
        );
    }
}

// Handles the result of a PROCESS_EXTENSION request sent after "Download jobs" writes the
// Downloads files. `btn` is the Download-jobs button itself, reused for a brief transient status
// (mirrors the cover-letter button's "Copied to clipboard!" flash) when nothing needs a decision;
// a real conflict list instead opens its own modal, independent of the button's lifecycle.
function handleProcessExtensionResult(result, btn) {
    if (!result || result.error) {
        btn.textContent = "Sync failed: " + ((result && result.error) || "unknown error");
        setTimeout(() => { btn.textContent = "Download jobs"; }, 4000);
        return;
    }
    if (result.type === "process_conflicts") {
        btn.textContent = "Download jobs";
        showConflictResolutionModal(result);
        return;
    }
    if (result.type === "extension_processed") {
        const s = result.summary || {};
        const parts = [];
        if (s.jobs_added) parts.push(s.jobs_added + " job(s)");
        const rulesChanged = (s.rules_added || 0) + (s.rules_replaced || 0) + (s.rules_combined || 0) + (s.blank_saved || 0);
        if (rulesChanged) parts.push(rulesChanged + " rule(s)");
        btn.textContent = parts.length ? "Synced: " + parts.join(", ") : "Synced (nothing new)";
        setTimeout(() => { btn.textContent = "Download jobs"; }, 3000);
        return;
    }
    btn.textContent = "Download jobs";
}

const CONFLICT_MODAL_KIND_LABELS = {
    rule_conflict: "Existing rule disagrees with extension answer",
    blank_new_rule: "Blank answer — save an empty rule?",
    reprioritize: "Already an accepted answer — just not top priority",
};

function injectConflictModalStyles() {
    if (document.getElementById("jobhelp-conflict-modal-styles")) return;
    const style = document.createElement("style");
    style.id = "jobhelp-conflict-modal-styles";
    style.textContent =
        ".jobhelp-conflict-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);" +
        "z-index:2147483647;display:flex;align-items:center;justify-content:center;}" +
        ".jobhelp-conflict-card{background:#fff;color:#111;border-radius:8px;max-width:640px;" +
        "width:90vw;max-height:85vh;overflow:auto;padding:20px;box-shadow:0 8px 30px rgba(0,0,0,.3);" +
        "font-size:14px;line-height:1.4;}" +
        ".jobhelp-conflict-card h2{margin:0 0 12px;font-size:16px;}" +
        ".jobhelp-conflict-row{border:1px solid #ddd;border-radius:6px;padding:10px 12px;margin-bottom:10px;}" +
        ".jobhelp-conflict-row .q{font-weight:600;margin-bottom:4px;}" +
        ".jobhelp-conflict-row .meta{color:#555;font-size:12px;margin-bottom:6px;}" +
        ".jobhelp-conflict-row .answers{margin-bottom:8px;}" +
        ".jobhelp-conflict-options{display:flex;flex-wrap:wrap;gap:6px;}" +
        ".jobhelp-conflict-options button{border:1px solid #999;background:#f5f5f5;border-radius:4px;" +
        "padding:5px 10px;cursor:pointer;font-size:13px;}" +
        ".jobhelp-conflict-options button.selected{background:#2563eb;color:#fff;border-color:#2563eb;}" +
        ".jobhelp-conflict-footer{display:flex;justify-content:flex-end;gap:8px;margin-top:14px;}" +
        ".jobhelp-conflict-footer button{padding:7px 14px;border-radius:4px;cursor:pointer;font-size:13px;}" +
        ".jobhelp-conflict-footer .submit{background:#2563eb;color:#fff;border:1px solid #2563eb;}" +
        ".jobhelp-conflict-footer .submit:disabled{opacity:.5;cursor:not-allowed;}" +
        ".jobhelp-conflict-footer .close{background:#fff;border:1px solid #999;}" +
        ".jobhelp-conflict-unresolved{color:#b00020;font-size:13px;margin-top:4px;}";
    document.head.appendChild(style);
}

function buildConflictRow(conflict, onSelect) {
    const row = document.createElement("div");
    row.className = "jobhelp-conflict-row";

    const q = document.createElement("div");
    q.className = "q";
    q.textContent = conflict.question || "(no question text)";
    row.appendChild(q);

    const metaParts = [];
    if (conflict.job) metaParts.push(conflict.job);
    if (conflict.url) metaParts.push(conflict.url);
    if (metaParts.length) {
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = metaParts.join(" — ");
        row.appendChild(meta);
    }

    const kindLabel = document.createElement("div");
    kindLabel.className = "meta";
    kindLabel.textContent = CONFLICT_MODAL_KIND_LABELS[conflict.kind] || conflict.kind;
    row.appendChild(kindLabel);

    if (conflict.kind === "rule_conflict" || conflict.kind === "reprioritize") {
        const answers = document.createElement("div");
        answers.className = "answers";
        const label = conflict.kind === "reprioritize" ? "Current top priority" : "Existing rule answer";
        answers.textContent =
            "Extension answer: " + (conflict.extension_answer || "(blank)") +
            "  |  " + label + ": " + (conflict.existing_rule_answer || "(none)");
        row.appendChild(answers);
    }

    const options = document.createElement("div");
    options.className = "jobhelp-conflict-options";
    for (const opt of conflict.options || []) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = opt.label;
        btn.addEventListener("click", () => {
            options.querySelectorAll("button").forEach((b) => b.classList.remove("selected"));
            btn.classList.add("selected");
            onSelect(opt.choice);
        });
        options.appendChild(btn);
    }
    row.appendChild(options);

    return row;
}

function showConflictResolutionModal(result) {
    injectConflictModalStyles();

    const backdrop = document.createElement("div");
    backdrop.className = "jobhelp-conflict-backdrop";

    const card = document.createElement("div");
    card.className = "jobhelp-conflict-card";
    backdrop.appendChild(card);

    const heading = document.createElement("h2");
    const conflicts = result.conflicts || [];
    heading.textContent = "Resolve " + conflicts.length + " item(s) from your saved jobs";
    card.appendChild(heading);

    const choices = new Map(); // conflict_id -> choice
    const submitBtn = document.createElement("button");

    for (const conflict of conflicts) {
        const row = buildConflictRow(conflict, (choice) => {
            choices.set(conflict.conflict_id, choice);
            submitBtn.disabled = choices.size < conflicts.length;
        });
        card.appendChild(row);
    }

    const footer = document.createElement("div");
    footer.className = "jobhelp-conflict-footer";

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "close";
    closeBtn.textContent = "Close";
    closeBtn.addEventListener("click", () => backdrop.remove());
    footer.appendChild(closeBtn);

    submitBtn.type = "button";
    submitBtn.className = "submit";
    submitBtn.textContent = "Submit";
    submitBtn.disabled = conflicts.length > 0;
    submitBtn.addEventListener("click", async () => {
        submitBtn.disabled = true;
        closeBtn.disabled = true;
        card.querySelectorAll(".jobhelp-conflict-options button").forEach((b) => { b.disabled = true; });
        submitBtn.textContent = "Submitting…";

        const resolutions = [...choices.entries()].map(([conflict_id, choice]) => ({ conflict_id, choice }));
        let outcome;
        try {
            outcome = await browser.runtime.sendMessage({
                type: "RESOLVE_CONFLICTS",
                requestId: result.request_id,
                serverRequestId: result.server_request_id,
                resolutions,
            });
        } catch (err) {
            outcome = { error: err.message };
        }
        renderConflictOutcome(card, outcome, () => backdrop.remove());
    });
    footer.appendChild(submitBtn);

    card.appendChild(footer);
    document.body.appendChild(backdrop);
}

function renderConflictOutcome(card, outcome, onDone) {
    card.replaceChildren();

    const heading = document.createElement("h2");
    card.appendChild(heading);

    const body = document.createElement("div");
    card.appendChild(body);

    if (!outcome || outcome.error) {
        heading.textContent = "Something went wrong";
        body.textContent = (outcome && outcome.error) || "Unknown error.";
    } else if (outcome.type === "conflict_resolution_timeout") {
        heading.textContent = "Took too long";
        body.textContent = "This took too long to resolve — please press \"Download jobs\" again.";
    } else {
        const s = outcome.summary || {};
        heading.textContent = "Done";
        body.textContent =
            "Rules — replaced: " + (s.rules_replaced || 0) +
            ", kept: " + (s.rules_kept || 0) +
            ", combined: " + (s.rules_combined || 0) +
            ", blank saved: " + (s.blank_saved || 0) +
            ", blank skipped: " + (s.blank_skipped || 0);

        if (outcome.unresolved && outcome.unresolved.length) {
            const unresolved = document.createElement("div");
            unresolved.className = "jobhelp-conflict-unresolved";
            unresolved.textContent =
                outcome.unresolved.length + " item(s) could not be applied (changed since reported) — try again.";
            card.appendChild(unresolved);
        }
    }

    const footer = document.createElement("div");
    footer.className = "jobhelp-conflict-footer";
    const doneBtn = document.createElement("button");
    doneBtn.type = "button";
    doneBtn.className = "submit";
    doneBtn.textContent = "Done";
    doneBtn.addEventListener("click", onDone);
    footer.appendChild(doneBtn);
    card.appendChild(footer);
}

function injectSlotStyles(slot) {
    if (slot.querySelector("style[data-jobhelp]")) return;

    const style = document.createElement("style");
    style.setAttribute("data-jobhelp", "");
    style.textContent =
        ".jobhelp-saved-wrap,.jobhelp-questions-wrap{position:relative;display:inline-flex;}" +
        ".jobhelp-saved-menu,.jobhelp-questions-menu{position:fixed;min-width:220px;max-width:320px;" +
        "max-height:240px;overflow:auto;margin:0;padding:4px 0;list-style:none;background:#fff;" +
        "border:1px solid #ccc;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);" +
        "z-index:2147483647;font-size:13px;}" +
        ".jobhelp-saved-menu::before,.jobhelp-questions-menu::before{content:'';position:absolute;" +
        "left:0;right:0;top:-12px;height:12px;}" +
        ".jobhelp-saved-item,.jobhelp-questions-item{display:flex;align-items:center;gap:8px;" +
        "padding:6px 8px 6px 12px;color:#111;}" +
        ".jobhelp-saved-item-title,.jobhelp-questions-item-title{flex:1 1 auto;min-width:0;" +
        "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}" +
        ".jobhelp-saved-remove{flex:0 0 auto;border:none;background:transparent;cursor:pointer;" +
        "color:#666;font-size:16px;line-height:1;padding:2px 6px;border-radius:4px;}" +
        ".jobhelp-saved-remove:hover{color:#b00020;background:#fde8e8;}" +
        ".jobhelp-saved-menu li.jobhelp-saved-empty,.jobhelp-questions-menu li.jobhelp-questions-empty{" +
        "padding:8px 12px;color:#666;font-style:italic;}" +
        ".jobhelp-questions-count,.jobhelp-saved-count{cursor:pointer;}" +
        "button:disabled{opacity:0.55;cursor:not-allowed;}";
    slot.prepend(style);
}

function renderSavedJobsMenu(menu, jobs, menuRoot) {
    menu.replaceChildren();
    if (!jobs.length) {
        const empty = document.createElement("li");
        empty.className = "jobhelp-saved-empty";
        empty.textContent = "No saved jobs yet";
        menu.appendChild(empty);
        return;
    }

    for (const job of jobs) {
        const item = document.createElement("li");
        item.className = "jobhelp-saved-item";

        const label = formatSavedJobLabel(job);

        const title = document.createElement("span");
        title.className = "jobhelp-saved-item-title";
        title.textContent = label;
        title.title = label;

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "jobhelp-saved-remove";
        removeBtn.setAttribute("aria-label", "Remove " + label);
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", (event) => {
            event.stopPropagation();
            removeSavedJob(job).then(() => {
                getSavedJobs().then((updated) => {
                    const btn = menuRoot?.querySelector(".jobhelp-saved-count");
                    if (!updated.length) {
                        closeSavedJobsMenu(menuRoot);
                    }
                    if (btn) {
                        updateSavedJobsCountBtn(btn, menuRoot);
                    } else if (updated.length && !menu.hidden && menuRoot) {
                        renderSavedJobsMenu(menu, updated, menuRoot);
                        positionSavedJobsMenu(menuRoot, menu);
                    }
                });
            });
        });

        item.appendChild(title);
        item.appendChild(removeBtn);
        menu.appendChild(item);
    }
}

function clearSavedMenuHoverCloseTimer() {
    if (savedMenuHoverCloseTimer) {
        clearTimeout(savedMenuHoverCloseTimer);
        savedMenuHoverCloseTimer = null;
    }
}

function scheduleSavedMenuHoverClose(menuRoot) {
    clearSavedMenuHoverCloseTimer();
    savedMenuHoverCloseTimer = setTimeout(() => {
        savedMenuHoverCloseTimer = null;
        closeSavedJobsMenu(menuRoot);
    }, SAVED_MENU_HOVER_CLOSE_MS);
}

function resetSavedJobsMenuPosition(menu) {
    menu.style.left = "";
    menu.style.top = "";
    menu.style.visibility = "";
}

function positionSavedJobsMenu(menuRoot, menu) {
    const listBtn = menuRoot.querySelector("button");
    if (!listBtn) return;

    menu.hidden = false;
    menu.style.visibility = "hidden";

    const btnRect = listBtn.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const margin = SAVED_MENU_VIEWPORT_MARGIN;

    let left = btnRect.left;
    let top = btnRect.bottom;

    if (left + menuRect.width > window.innerWidth - margin) {
        left = Math.max(margin, window.innerWidth - menuRect.width - margin);
    }
    if (left < margin) {
        left = margin;
    }

    if (top + menuRect.height > window.innerHeight - margin) {
        top = Math.max(margin, btnRect.top - menuRect.height);
    }

    menu.style.left = left + "px";
    menu.style.top = top + "px";
    menu.style.visibility = "";
}

function closeSavedJobsMenu(menuRoot) {
    const menu = menuRoot.querySelector(".jobhelp-saved-menu");
    clearSavedMenuHoverCloseTimer();

    if (menu) {
        menu.hidden = true;
        resetSavedJobsMenuPosition(menu);
    }
}

function openSavedJobsMenu(menuRoot) {
    const menu = menuRoot.querySelector(".jobhelp-saved-menu");
    if (!menu) return;

    clearSavedMenuHoverCloseTimer();

    getSavedJobs().then((jobs) => {
        if (!jobs.length) return;

        renderSavedJobsMenu(menu, jobs, menuRoot);
        positionSavedJobsMenu(menuRoot, menu);
    });
}

// Populates our slot with this extension's buttons. Called by the shared module
// with our slot element (inside the shared taskbar's open shadow root).
function buildButtons(slot) {
    injectSlotStyles(slot);
    closeSavedJobsMenu(slot);
    slot.querySelectorAll(".jobhelp-questions-wrap").forEach(closeSavedQuestionsMenu);
    clearQuestionsMenuHoverCloseTimer();
    stopSaveButtonRefresh();

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.addEventListener("click", async () => {
        const job = getJobForSave();
        if (!job) return;

        await addSavedJob(job);
        updateSavedJobsCountBtn(savedJobsBtn, menuWrap);
    });

    const saveQuestionsBtn = document.createElement("button");
    saveQuestionsBtn.type = "button";
    saveQuestionsBtn.addEventListener("click", async () => {
        const applicationRoot = getLinkedInApplicationRoot();
        if (!applicationRoot && !findLinkedInEasyApplyMarker()) return;

        const pairs = scanLinkedInApplicationFields(applicationRoot || document);
        if (!pairs.length) return;

        await addSavedApplicationQuestions(pairs, getJobContextForQuestions());
        updateSavedQuestionsCountBtn(questionsBtn, questionsWrap);
    });

    const coverLetterBtn = document.createElement("button");
    coverLetterBtn.type = "button";
    coverLetterBtn.addEventListener("click", async () => {
        const job = getJobForCoverLetter();
        if (!job) return;

        coverLetterBusy = true;
        coverLetterBtn.disabled = true;
        coverLetterBtn.textContent = "Generating…";

        let res;
        try {
            res = await browser.runtime.sendMessage({ type: "GENERATE_COVER_LETTER", job });
        } catch (err) {
            res = { error: err.message };
        }

        if (!res || res.error) {
            coverLetterBtn.textContent = "Failed: " + ((res && res.error) || "unknown error");
        } else {
            try {
                await navigator.clipboard.writeText(res.coverLetter || "");
                coverLetterBtn.textContent = "Copied to clipboard!";
            } catch (err) {
                coverLetterBtn.textContent = "Generated (copy failed)";
            }
        }

        setTimeout(() => {
            coverLetterBusy = false;
            updateCoverLetterButtonState(coverLetterBtn);
        }, 3000);
    });

    startSaveButtonsRefresh(saveBtn, saveQuestionsBtn, coverLetterBtn);

    const questionsWrap = document.createElement("div");
    questionsWrap.className = "jobhelp-questions-wrap";

    const questionsBtn = document.createElement("button");
    questionsBtn.type = "button";
    questionsBtn.className = "jobhelp-questions-count";
    questionsBtn.addEventListener("click", async () => {
        const questions = await getSavedApplicationQuestions();
        if (!questions.length) return;

        const confirmed = window.confirm(
            "Clear all " + questions.length + " saved application question" +
            (questions.length === 1 ? "" : "s") + "?"
        );
        if (!confirmed) return;

        await clearSavedApplicationQuestions();
        updateSavedQuestionsCountBtn(questionsBtn, questionsWrap);
        closeSavedQuestionsMenu(questionsWrap);
    });
    updateSavedQuestionsCountBtn(questionsBtn, questionsWrap);

    const questionsMenu = document.createElement("ul");
    questionsMenu.className = "jobhelp-questions-menu";
    questionsMenu.hidden = true;

    const cancelQuestionsHoverClose = () => clearQuestionsMenuHoverCloseTimer();

    questionsWrap.addEventListener("mouseenter", () => {
        cancelQuestionsHoverClose();
        openSavedQuestionsMenu(questionsWrap);
    });

    questionsWrap.addEventListener("mouseleave", (event) => {
        if (!isInsideQuestionsMenu(event.relatedTarget, questionsWrap, questionsMenu)) {
            scheduleQuestionsMenuHoverClose(questionsWrap);
        }
    });

    questionsMenu.addEventListener("mouseenter", cancelQuestionsHoverClose);
    questionsMenu.addEventListener("mouseleave", (event) => {
        if (!isInsideQuestionsMenu(event.relatedTarget, questionsWrap, questionsMenu)) {
            scheduleQuestionsMenuHoverClose(questionsWrap);
        }
    });

    questionsWrap.appendChild(questionsBtn);
    questionsWrap.appendChild(questionsMenu);

    const downloadBtn = document.createElement("button");
    downloadBtn.type = "button";
    downloadBtn.textContent = "Download jobs";
    downloadBtn.addEventListener("click", async () => {
        downloadBtn.disabled = true;
        downloadBtn.textContent = "Downloading…";
        try {
            const jobs = await getSavedJobs();
            const questions = await getSavedApplicationQuestions();
            const jobsText = jobs.map((job) => formatSavedJobDownloadLine(job)).join("\n");
            const questionsText = questions.length ? formatApplicationQuestionsDownloadText(questions) : "";

            await saveTextFile(jobsText, "saved_jobs.txt");
            if (questions.length) {
                await saveTextFile(questionsText, "saved_job_application_questions.txt");
            }

            let result;
            try {
                result = await browser.runtime.sendMessage({
                    type: "PROCESS_EXTENSION",
                    savedJobsText: jobsText,
                    savedQuestionsText: questionsText,
                });
            } catch (err) {
                result = { error: err.message };
            }
            downloadBtn.disabled = false;
            handleProcessExtensionResult(result, downloadBtn);
        } catch (err) {
            downloadBtn.disabled = false;
            handleProcessExtensionResult({ error: err.message }, downloadBtn);
        }
    });

    const menuWrap = document.createElement("div");
    menuWrap.className = "jobhelp-saved-wrap";

    const savedJobsBtn = document.createElement("button");
    savedJobsBtn.type = "button";
    savedJobsBtn.className = "jobhelp-saved-count";
    savedJobsBtn.addEventListener("click", async () => {
        const jobs = await getSavedJobs();
        if (!jobs.length) return;

        const confirmed = window.confirm(
            "Clear all " + jobs.length + " saved job" +
            (jobs.length === 1 ? "" : "s") + "?"
        );
        if (!confirmed) return;

        await clearSavedJobs();
        updateSavedJobsCountBtn(savedJobsBtn, menuWrap);
        closeSavedJobsMenu(menuWrap);
    });
    updateSavedJobsCountBtn(savedJobsBtn, menuWrap);

    const menu = document.createElement("ul");
    menu.className = "jobhelp-saved-menu";
    menu.hidden = true;

    const cancelSavedHoverClose = () => clearSavedMenuHoverCloseTimer();

    menuWrap.addEventListener("mouseenter", () => {
        cancelSavedHoverClose();
        openSavedJobsMenu(menuWrap);
    });

    menuWrap.addEventListener("mouseleave", (event) => {
        if (!isInsideSavedMenu(event.relatedTarget, menuWrap, menu)) {
            scheduleSavedMenuHoverClose(menuWrap);
        }
    });

    menu.addEventListener("mouseenter", cancelSavedHoverClose);
    menu.addEventListener("mouseleave", (event) => {
        if (!isInsideSavedMenu(event.relatedTarget, menuWrap, menu)) {
            scheduleSavedMenuHoverClose(menuWrap);
        }
    });

    menuWrap.appendChild(savedJobsBtn);
    menuWrap.appendChild(menu);
    slot.appendChild(saveQuestionsBtn);
    slot.appendChild(saveBtn);
    slot.appendChild(coverLetterBtn);
    slot.appendChild(questionsWrap);
    slot.appendChild(menuWrap);
    slot.appendChild(downloadBtn);
}

// Deliberately not persisted to storage: the taskbar auto-shows on LinkedIn
// on every fresh page load, and a manual toggle only overrides that for the
// lifetime of this page (SPA navigation within LinkedIn keeps it; a real
// reload or a new tab re-evaluates from scratch).
let taskbarRetryTimers = [];
let jobhelpTaskbarActive = false;

function clearTaskbarRetryTimers() {
    taskbarRetryTimers.forEach(clearTimeout);
    taskbarRetryTimers = [];
}

function jobhelpHasOtherExtensionSlots() {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    const root = host && host.shadowRoot;
    if (!root) return false;
    const slots = root.querySelector("." + SHARED_TASKBAR.SLOTS_CLASS);
    if (!slots) return false;
    return Array.from(slots.children).some(
        (slot) => slot.getAttribute("data-ext") !== EXT_KEY
    );
}

function scheduleTaskbarRetries() {
    clearTaskbarRetryTimers();
    const retry = () => {
        if (!jobhelpTaskbarActive) return;
        sharedRebuildSlotIfEmpty(EXT_KEY, buildButtons, TASKBAR_ORDER);
    };
    [100, 200, 400, 800, 1500, 2500].forEach((ms) => {
        taskbarRetryTimers.push(setTimeout(retry, ms));
    });
}

function showTaskbar() {
    jobhelpTaskbarActive = true;

    const openNow = () => {
        if (!jobhelpTaskbarActive) return;

        const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
        // Cold reset only when alone and the shell is empty (LinkedIn job search fix).
        if (host && !jobhelpHasOtherExtensionSlots() && sharedHostIsEmptyShell()) {
            sharedRemoveTaskbarHost();
        }

        registerTaskbar(EXT_KEY, buildButtons, TASKBAR_ORDER);
        sharedRescanAfterOpen();
    };

    // Join an already-open shared bar immediately; defer solo opens for SPA timing.
    if (document.getElementById(SHARED_TASKBAR.HOST_ID) && jobhelpHasOtherExtensionSlots()) {
        openNow();
    } else {
        queueMicrotask(() => {
            requestAnimationFrame(() => {
                requestAnimationFrame(openNow);
            });
        });
    }

    scheduleTaskbarRetries();
}

// Auto-show on LinkedIn for every fresh page load. The taskbar has no manual
// toggle anymore — the toolbar icon now opens the form-fill popup instead
// (browser.action.onClicked no longer fires once default_popup is set).
// Gated to the top frame only: content.js now runs in every matching iframe
// too (all_frames, for scanning fields inside embedded ATS forms), and a
// full-width fixed taskbar bar has no sensible rendering inside a nested
// iframe's own viewport.
if (window.top === window) {
  if (isLinkedInPage()) {
    showTaskbar();
  } else if (
    document.getElementById(SHARED_TASKBAR.HOST_ID) &&
    sharedHostIsEmptyShell() &&
    !jobhelpHasOtherExtensionSlots()
  ) {
    sharedRemoveTaskbarHost();
  }
}

// ---- generic form fill + save fields (any site, triggered from the popup) -
// scanLinkedInApplicationFields reads already-answered LinkedIn Easy Apply
// fields using LinkedIn-specific markup; scanFormFields below does the same
// two jobs (fill blank fields, save whatever's currently on the page) for
// any site, using generic/DOM-standard label detection instead
// (label[for], wrapping <label>, aria-*, placeholder, fieldset/legend).

const GENERIC_TEXT_INPUT_TYPES = new Set(["", "text", "email", "tel", "number", "url", "search"]);

function isGenericTextInput(el) {
    if (el.tagName !== "INPUT") return false;
    return GENERIC_TEXT_INPUT_TYPES.has((el.getAttribute("type") || "").toLowerCase());
}

function getGenericFieldLabel(el) {
    if (el.id) {
        const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        const text = label && getLinkedInElementText(label);
        if (text) return text;
    }

    const wrappingLabel = el.closest("label");
    const wrappingText = wrappingLabel && getLinkedInElementText(wrappingLabel);
    if (wrappingText) return wrappingText;

    const ariaLabel = (el.getAttribute("aria-label") || "").trim();
    if (ariaLabel) return ariaLabel;

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
        const text = labelledBy
            .split(/\s+/)
            .map((id) => document.getElementById(id))
            .filter(Boolean)
            .map((node) => getLinkedInElementText(node))
            .join(" ")
            .trim();
        if (text) return text;
    }

    const placeholder = (el.getAttribute("placeholder") || "").trim();
    if (placeholder) return placeholder;

    // Some sites (e.g. Workday) wrap a single text/textarea/select field in a
    // bare <fieldset><legend> instead of using label[for] at all — same
    // positional convention getFieldsetLegendLabel already uses for grouped
    // radio/checkbox fieldsets, just with exactly one control inside instead
    // of several.
    const fieldset = el.closest("fieldset");
    return fieldset ? getFieldsetLegendLabel(fieldset) : "";
}

function getFieldsetLegendLabel(fieldset) {
    const legend = fieldset.querySelector(":scope > legend");
    if (legend) return trimLinkedInFieldLabel(getLinkedInElementText(legend));
    // Some component libraries use a leading <label> as the group heading
    // instead of a semantic <legend> (e.g. Ashby's EEO gender/race/veteran
    // question fieldsets) — its `for` often doesn't resolve to anything
    // (matches a `data-field-path` wrapper, not an actual control), so this
    // is purely positional: first <label> that's a direct child.
    const leadingLabel = fieldset.querySelector(":scope > label");
    return leadingLabel ? trimLinkedInFieldLabel(getLinkedInElementText(leadingLabel)) : "";
}

function getRadioOrCheckboxLabel(input) {
    if (input.id) {
        const label = document.querySelector('label[for="' + CSS.escape(input.id) + '"]');
        const text = label && getLinkedInElementText(label);
        if (text) return text;
    }
    const wrappingLabel = input.closest("label");
    if (wrappingLabel) return getLinkedInElementText(wrappingLabel);
    const authoredValue = input.hasAttribute("value") ? input.value : "";
    return (input.getAttribute("aria-label") || authoredValue || "").trim();
}

// A checked radio/checkbox with no authored `value` attribute defaults its
// .value property to the literal string "on" (HTML spec) — every option in
// a group would report the same meaningless "on" if that were trusted, so
// only use .value when the attribute was actually authored (e.g. Ashby's
// EEO radios have no value attribute at all and rely on their <label> text).
function getControlValueOrLabel(c) {
    return (c.hasAttribute("value") ? c.value : "") || getRadioOrCheckboxLabel(c) || "";
}

// Joins every checked control's value/label (checkbox groups can have more
// than one checked); radio groups only ever have at most one.
function getCheckedGroupValue(controls) {
    return controls
        .filter((c) => c.checked)
        .map(getControlValueOrLabel)
        .filter(Boolean)
        .join(", ");
}

// Workday's custom "Select One" dropdown: a <button aria-haspopup="listbox">
// showing the current choice as its own text (e.g. "No", or "Select One"
// when unanswered), paired with a same-container mirror <input type="text">
// that has no id/name/label of its own and just tracks the selected option's
// internal GUID — without this check that mirror input would otherwise get
// picked up by the main loop above as an ordinary (unlabeled, GUID-valued)
// text field.
function isListboxComboboxCompanionInput(el) {
    if (el.tagName !== "INPUT") return false;
    const prev = el.previousElementSibling;
    return !!(prev && prev.tagName === "BUTTON" && prev.getAttribute("aria-haspopup") === "listbox");
}

// The button's own aria-label describes its *current value* (e.g. "No
// Required"), not the question — unlike getGenericFieldLabel, this must
// prefer the fieldset/legend text and only fall back to a cleaned-up
// aria-label when there's no fieldset ancestor.
function getComboboxButtonLabel(button) {
    const fieldset = button.closest("fieldset");
    const fieldsetLabel = fieldset && getFieldsetLegendLabel(fieldset);
    if (fieldsetLabel) return fieldsetLabel;
    const ariaLabel = (button.getAttribute("aria-label") || "").trim();
    return ariaLabel.replace(/\s*(required|optional)\s*$/i, "").trim();
}

const COMBOBOX_PLACEHOLDER_VALUES = new Set(["select one", "select...", "select"]);

function getComboboxButtonValue(button) {
    const text = (button.textContent || "").trim();
    return COMBOBOX_PLACEHOLDER_VALUES.has(text.toLowerCase()) ? "" : text;
}

// Ashby-style (and similar) Yes/No question: a single <input type="checkbox">
// that's part of the submitted form data but deliberately display:none (the
// real UI is two sibling <button>Yes</button>/<button>No</button> elements
// that toggle it) — no <fieldset> at all, so this needs its own detection
// separate from the fieldset-grouped radio/checkbox pass above. Answering it
// means clicking the right button (see fillYesNoToggle), not touching the
// hidden checkbox directly — that's what actually drives the page's own
// click handlers/state, the same way a real user would answer it.
function getYesNoToggleButtons(checkbox) {
    const container = checkbox.parentElement;
    if (!container) return null;
    const buttons = [...container.querySelectorAll(":scope > button")];
    if (buttons.length !== 2) return null;

    const yesBtn = buttons.find((b) => normalizeMatchText(b.textContent) === "yes");
    const noBtn = buttons.find((b) => normalizeMatchText(b.textContent) === "no");
    if (!yesBtn || !noBtn) return null;
    return { yesBtn, noBtn };
}

// CSS-module class names are build-hashed (e.g. "_active_1svni_57"), so match
// an "active"-ish token bounded by start/`-`/`_` rather than an exact class —
// specific enough to avoid false positives like "inactive".
function isToggleButtonActive(btn) {
    return /(^|[-_])(active|selected|checked|pressed)([-_]|$)/i.test(btn.className);
}

// The <label for=...> here references the checkbox's `name`, not an `id`
// (Ashby's checkbox has no `id` at all) — try that before falling back to the
// normal generic lookup.
function getYesNoToggleLabel(checkbox) {
    if (checkbox.name) {
        const label = document.querySelector('label[for="' + CSS.escape(checkbox.name) + '"]');
        const text = label && getLinkedInElementText(label);
        if (text) return text;
    }
    return getGenericFieldLabel(checkbox);
}

// Only fieldset/legend-grouped radio and checkbox groups are handled —
// ungrouped/ambiguous radios are skipped rather than guessed at (a fieldset
// with a legend is the one broadly-supported, unambiguous way to associate a
// question with a set of options).
//
// `onlyBlank: true` (form-fill) only returns fields with no current value;
// `onlyBlank: false` (save-fields) returns every field found, filled or not,
// each descriptor's `value` carrying its current state — mirrors what
// scanLinkedInApplicationFields does for LinkedIn's Easy Apply forms, just
// with generic/DOM-standard label detection instead of LinkedIn markup.
function scanFormFields(root, { onlyBlank } = {}) {
    root = root || document;
    const descriptors = [];
    const elements = [];
    const handledFieldsets = new Set();

    function addField(label, type, element, value) {
        const trimmed = (label || "").trim();
        if (!trimmed) return;
        descriptors.push({ label: trimmed, type, value: value || "" });
        elements.push(element);
    }

    root.querySelectorAll("input, textarea, select").forEach((el) => {
        if (isListboxComboboxCompanionInput(el)) return; // handled via the combobox button pass below

        const type = (el.getAttribute("type") || "").toLowerCase();

        if (type === "checkbox") {
            // Checked separately from isLinkedInFormControlVisible below: this
            // pattern's checkbox is deliberately display:none, so visibility
            // is judged by its visible Yes/No buttons instead.
            const toggle = getYesNoToggleButtons(el);
            if (toggle && isVisibleElement(toggle.yesBtn) && isVisibleElement(toggle.noBtn)) {
                const answer = isToggleButtonActive(toggle.yesBtn) ? "Yes"
                    : isToggleButtonActive(toggle.noBtn) ? "No" : "";
                if (!onlyBlank || !answer) {
                    addField(getYesNoToggleLabel(el), "radio", { yesNoToggle: true, ...toggle }, answer);
                }
            }
            return; // otherwise handled via the fieldset checkbox_group pass below
        }

        if (!isLinkedInFormControlVisible(el)) return;
        if (type === "radio") return; // handled via the fieldset pass below

        let fieldType = null;
        if (isGenericTextInput(el)) fieldType = "text";
        else if (el.tagName === "TEXTAREA") fieldType = "textarea";
        else if (el.tagName === "SELECT") fieldType = "select";
        if (!fieldType) return;

        if (onlyBlank && el.value) return;
        addField(getGenericFieldLabel(el), fieldType, el, el.value);
    });

    root.querySelectorAll("fieldset").forEach((fieldset) => {
        if (handledFieldsets.has(fieldset) || !isVisibleElement(fieldset)) return;

        const label = getFieldsetLegendLabel(fieldset);
        if (!label) return;

        const radios = [...fieldset.querySelectorAll('input[type="radio"]')].filter(isLinkedInFormControlVisible);
        if (radios.length) {
            handledFieldsets.add(fieldset);
            const value = getCheckedGroupValue(radios);
            if (!onlyBlank || !value) {
                addField(label, "radio", { fieldset, controls: radios }, value);
            }
            return;
        }

        const checkboxes = [...fieldset.querySelectorAll('input[type="checkbox"]')].filter(isLinkedInFormControlVisible);
        if (checkboxes.length) {
            handledFieldsets.add(fieldset);
            const value = getCheckedGroupValue(checkboxes);
            if (!onlyBlank || !value) {
                addField(label, "checkbox_group", { fieldset, controls: checkboxes }, value);
            }
        }
    });

    // Workday-style custom "Select One" combobox (see isListboxComboboxCompanionInput).
    root.querySelectorAll('button[aria-haspopup="listbox"]').forEach((button) => {
        if (!isVisibleElement(button)) return;
        const label = getComboboxButtonLabel(button);
        if (!label) return;
        const value = getComboboxButtonValue(button);
        if (!onlyBlank || !value) {
            addField(label, "combobox", button, value);
        }
    });

    return { descriptors, elements };
}

function normalizeMatchText(text) {
    return (text || "").trim().toLowerCase();
}

// React (and similar frameworks) instruments controlled inputs by
// redefining `value`/`checked` as an own property on the DOM *node itself*,
// whose setter both writes the real value and updates React's internal
// "last known value" tracker in one step. A plain `el.value = x` goes
// through that same patched setter — so by the time the input/change event
// we dispatch afterward fires, React compares current-vs-tracked value, sees
// no difference (both already updated together), and never calls the
// component's onChange. The field ends up visibly filled in the DOM but the
// app's own state — what's actually checked as "answered" and submitted —
// never received it. Writing through the *native* prototype setter instead
// (found on HTMLInputElement.prototype etc., one level up from React's
// instance-level override) updates the real value without touching React's
// tracker, so the tracker is left stale and the dispatched event reads as a
// genuine change — same effect a real user's keystroke/click would have.
function setNativeProperty(el, prop, value) {
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), prop);
    if (descriptor && descriptor.set) {
        descriptor.set.call(el, value);
    } else {
        el[prop] = value;
    }
}

// Many sites (autocomplete/combobox widgets especially) don't treat a field
// as "answered" from .value + input/change alone — they only commit the
// value, dismiss a suggestion popup, or clear a validation error on an Enter
// keypress. Simulated (dispatchEvent) keyboard events are untrusted, so the
// browser won't run native default actions like implicit form submission for
// them — only a page's own JS keydown handler reacts, which is exactly the
// commit behavior this is trying to trigger.
function dispatchEnterKey(el) {
    const eventInit = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent("keydown", eventInit));
    el.dispatchEvent(new KeyboardEvent("keypress", eventInit));
    el.dispatchEvent(new KeyboardEvent("keyup", eventInit));
}

function fillTextLikeField(el, value) {
    el.focus();
    setNativeProperty(el, "value", value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    dispatchEnterKey(el);
}

function fillSelectField(el, value) {
    const target = normalizeMatchText(value);
    const option = [...el.options].find(
        (o) => normalizeMatchText(o.value) === target || normalizeMatchText(o.textContent) === target
    );
    if (!option) return false;
    setNativeProperty(el, "value", option.value);
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
}

function fillGroupField(group, value) {
    const target = normalizeMatchText(value);
    const match = group.controls.find(
        (input) => normalizeMatchText(input.value) === target ||
            normalizeMatchText(getRadioOrCheckboxLabel(input)) === target
    );
    if (!match) return false;
    setNativeProperty(match, "checked", true);
    match.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
}

// Click the real Yes/No button rather than touching the hidden checkbox
// directly — the page's own click handler is what actually updates its
// (likely React) state; setting .checked on a display:none input the page
// isn't watching wouldn't do anything.
function fillYesNoToggle(toggle, value) {
    const target = normalizeMatchText(value);
    if (target === "yes") {
        toggle.yesBtn.click();
        return true;
    }
    if (target === "no") {
        toggle.noBtn.click();
        return true;
    }
    return false;
}

// Polls `check` every 50ms for up to 1.5s — a popup listbox renders
// asynchronously after its trigger is clicked (state update + re-render), so
// a fixed short delay would be either too slow or too flaky depending on
// page load. Resolves with check()'s first truthy result, or null on timeout.
function waitFor(check, { timeoutMs = 1500, intervalMs = 50 } = {}) {
    return new Promise((resolve) => {
        const start = Date.now();
        const tick = () => {
            const result = check();
            if (result) {
                resolve(result);
            } else if (Date.now() - start >= timeoutMs) {
                resolve(null);
            } else {
                setTimeout(tick, intervalMs);
            }
        };
        tick();
    });
}

// Workday's custom "Select One" combobox has no value to just set directly —
// clicking the button opens a popup listbox of freshly-rendered
// [role="option"] elements (often rendered elsewhere in the DOM, not nested
// under the button), and only the page's own click handling on the chosen
// option actually drives its state. Async because that popup doesn't exist
// until after the click's state update has flushed.
async function fillComboboxField(button, value) {
    const target = normalizeMatchText(value);
    button.click();

    const option = await waitFor(() => {
        const opts = [...document.querySelectorAll('[role="option"]')].filter(isVisibleElement);
        return opts.find((o) => normalizeMatchText(o.textContent) === target) || null;
    });

    if (!option) {
        button.click(); // best-effort: close the popup we opened rather than leaving it stuck open
        return false;
    }

    option.click();
    return true;
}

// The server may offer several acceptable answers in priority order (e.g. ["No", "Not
// applicable"]) when a rule has more than one — try each until one matches an option this
// field actually has, and leave the field untouched if none do. `answer.values` is the new
// candidate-list shape; `answer.value` alone (older server / no candidates) is treated as a
// one-item list so this still works unchanged against a server that hasn't been updated.
async function applyFieldAnswer(descriptor, element, answer) {
    if (!answer) return false;
    const candidates = Array.isArray(answer.values) && answer.values.length
        ? answer.values
        : (answer.value != null ? [answer.value] : []);
    if (!candidates.length) return false;

    if (descriptor.type === "text" || descriptor.type === "textarea") {
        // Free text always "succeeds" — there's no notion of the field rejecting a value, so
        // only the top-priority candidate is meaningful here.
        fillTextLikeField(element, String(candidates[0]));
        return true;
    }

    for (const raw of candidates) {
        const value = String(raw);
        let ok = false;
        if (descriptor.type === "select") {
            ok = fillSelectField(element, value);
        } else if (descriptor.type === "radio" || descriptor.type === "checkbox_group") {
            ok = element.yesNoToggle ? fillYesNoToggle(element, value) : fillGroupField(element, value);
        } else if (descriptor.type === "combobox") {
            ok = await fillComboboxField(element, value);
        }
        if (ok) return true;
    }
    return false;
}

// On a non-LinkedIn page getJobContextForQuestions() comes back entirely
// empty (it's gated on LinkedIn-specific signals) — fall back to the page's
// own title/URL so saved entries still carry some context.
function getGenericJobContext() {
    const linkedInContext = getJobContextForQuestions();
    if (linkedInContext.title || linkedInContext.company || linkedInContext.url) {
        return linkedInContext;
    }
    return { title: document.title || "", company: "", url: location.href };
}

// Sent by popup.js (browser.tabs.sendMessage) — these are the only messages
// content.js handles now that the taskbar has no manual toggle.
browser.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "FILL_FORM") {
        (async () => {
            const { descriptors, elements } = scanFormFields(document, { onlyBlank: true });
            if (!descriptors.length) {
                sendResponse({ filled: 0, total: 0, flagged: 0 });
                return;
            }

            const fields = descriptors.map((d) => ({ label: d.label, type: d.type }));
            let res;
            try {
                res = await browser.runtime.sendMessage({ type: "ANSWER_FIELDS", fields });
            } catch (err) {
                sendResponse({ error: err.message });
                return;
            }

            if (!res || res.error) {
                sendResponse({ error: (res && res.error) || "unknown error" });
                return;
            }

            const answers = res.answers || [];
            let filled = 0;
            let flagged = 0;
            // Sequential (not Promise.all) since combobox answers open/close a
            // real popup on the page — filling two at once would race.
            for (let i = 0; i < descriptors.length; i++) {
                const answer = answers[i];
                if (answer && answer.flag === "discard") {
                    flagged += 1;
                    continue;
                }
                if (await applyFieldAnswer(descriptors[i], elements[i], answer)) {
                    filled += 1;
                }
            }

            sendResponse({ filled, total: descriptors.length, flagged });
        })();

        return true;
    }

    if (msg.type === "SAVE_FIELDS") {
        (async () => {
            const { descriptors } = scanFormFields(document, { onlyBlank: false });
            if (!descriptors.length) {
                sendResponse({ saved: 0 });
                return;
            }

            const pairs = descriptors.map((d) => ({
                question: d.label,
                answer: d.value,
                fieldType: d.type,
            }));

            await addSavedApplicationQuestions(pairs, getGenericJobContext());
            sendResponse({ saved: pairs.length });
        })();

        return true;
    }
});
