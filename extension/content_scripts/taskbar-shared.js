// taskbar-shared.js
// SHARED COORDINATION MODULE — keep this file BYTE-FOR-BYTE IDENTICAL in every
// extension that wants to share the taskbar.
//
// Extensions can't share JavaScript (each content script runs in its own
// isolated world), so they coordinate purely through the page DOM. The contract
// below — the host element id, the slots container, and the events — is the
// shared "API". Whichever extension's content script runs first creates the
// taskbar; the one that removes the last slot tears it down.

const SHARED_TASKBAR = {
    HOST_ID: "shared-taskbar-host",
    SLOTS_CLASS: "shared-taskbar-slots",
    SLOT_CLASS: "shared-taskbar-slot",
    HEIGHT: 50,
    // Fixed/sticky headers stacked under the main nav (e.g. LinkedIn company bars)
    // sit below our bar height but still need shifting.
    MAX_SHIFT_TOP: 200,
    SHIFTED_ATTR: "data-shared-taskbar-prev-top",
    SHIFT_BASE_ATTR: "data-shared-taskbar-shift-base",
    // Stable nav-row height captured at shift time (min seen) — live height
    // grows when a sub-header opens and breaks stacked-header detection.
    SHIFT_ROW_HEIGHT_ATTR: "data-shared-taskbar-row-height",
    CONSTRAINED_ATTR: "data-shared-taskbar-constrained",
    FIT_TRIM_ATTR: "data-shared-taskbar-fit-trim",
    PREV_MAX_HEIGHT_ATTR: "data-shared-taskbar-prev-max-height",
    PREV_MIN_HEIGHT_ATTR: "data-shared-taskbar-prev-min-height",
    PREV_HEIGHT_ATTR: "data-shared-taskbar-prev-height",
    PREV_OVERFLOW_Y_ATTR: "data-shared-taskbar-prev-overflow-y",
    // Body's own padding-top before we touched it (exact inline value, for restore)
    // and the computed baseline captured at the same time (for target math — the
    // page may have reserved space for its own fixed header via body padding, and
    // our shifted-down copy of that header needs padding = base + HEIGHT, not just
    // HEIGHT, to avoid overlapping the content below it).
    PREV_BODY_PADDING_ATTR: "data-shared-taskbar-prev-body-padding",
    BODY_PADDING_BASE_ATTR: "data-shared-taskbar-body-padding-base",
    // Some elements are actively driven by the page itself (e.g. scroll-reveal
    // chrome that toggles its own top/transform to hide at the top of the page) —
    // forcing our shift onto them fights that toggle forever. FIGHT_COUNT/TS track
    // how often we've had to re-correct a given element in a short window;
    // QUARANTINED_ATTR marks one we've given up on so we stop touching it.
    FIGHT_COUNT_ATTR: "data-shared-taskbar-fight-count",
    FIGHT_TS_ATTR: "data-shared-taskbar-fight-ts",
    QUARANTINED_ATTR: "data-shared-taskbar-quarantined",
    EVT_READY: "shared-taskbar:ready",
    EVT_REMOVED: "shared-taskbar:removed",
    EVT_PAGE_NAV: "shared-taskbar:page-nav",
};

// ---- zoom compensation ------------------------------------------------------
// Browser page zoom (ctrl+/ctrl-) scales every CSS px uniformly, so anything we
// size in px would otherwise grow or shrink on screen right along with the
// page. We want the taskbar to stay a constant physical size instead, so we
// track window.devicePixelRatio — which browsers scale by the current zoom
// factor — relative to whatever it was when this script first ran, and shrink
// or grow our own CSS px by the inverse so the on-screen result cancels the
// page's zoom back out. (This also reacts to a real DPI change, e.g. dragging
// the window to a different-scaling monitor — there's no way to tell that
// apart from a zoom change from content-script-accessible APIs alone, so a
// mid-session monitor move will be misread as a zoom change.)
const sharedZoomBaselineDpr = window.devicePixelRatio || 1;
let sharedZoomMedia = null;

function sharedZoomRatio() {
    return (window.devicePixelRatio || 1) / sharedZoomBaselineDpr;
}

// Scale a design-time px value by the inverse of the current zoom ratio.
function sharedZoomPx(px) {
    return px / sharedZoomRatio();
}

function sharedBarHeight() {
    return sharedZoomPx(SHARED_TASKBAR.HEIGHT);
}

function sharedRefreshHostChrome() {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return;
    sharedApplyHostChromeStyles(host);
    const style = sharedQueryTaskbar(host, "style[data-shared-taskbar]");
    if (style) style.textContent = sharedHostStyleSheet();
}

function sharedOnZoomChange() {
    sharedRefreshHostChrome();
    sharedApplyPageShift();
}

// There is no native "zoom changed" event. matchMedia on the current
// devicePixelRatio fires its change event the instant that ratio moves away
// from the value it was created with, which we use as a proxy zoom signal —
// then immediately re-create the query against the new ratio so it can fire
// again on the next change.
function sharedWatchZoom() {
    if (sharedZoomMedia) return;

    const onZoomMediaChange = () => {
        trackZoomMedia();
        sharedOnZoomChange();
    };

    function trackZoomMedia() {
        if (sharedZoomMedia) sharedZoomMedia.removeEventListener("change", onZoomMediaChange);
        sharedZoomMedia = window.matchMedia("(resolution: " + window.devicePixelRatio + "dppx)");
        sharedZoomMedia.addEventListener("change", onZoomMediaChange);
    }

    trackZoomMedia();
}

// ---- page shifting ---------------------------------------------------------
// All shift state is stored in the DOM (an attribute on each moved element and
// body's inline paddingTop) so that *either* extension can undo it, even the one
// that didn't create the taskbar. The MutationObserver is the only piece of
// per-extension JS state, so it self-terminates when it notices the host is gone.
let sharedTaskbarObserver = null;
let sharedMutationTimer = null;
let sharedPendingMutations = null;
// let sharedLinkedInJobSearchPage = null; // disabled — LinkedIn-specific, see below
// let sharedOrgStickyObserver = null; // disabled — LinkedIn-specific, see below
let sharedConstrainTimer = null;
let sharedResizeConstrainHandler = null;
let sharedSpaNavInstalled = false;
let sharedSlotIntegrityObserver = null;
let sharedSlotIntegrityConfig = null;
let sharedRegisteringSlot = false;
let sharedTaskbarOpenAllowed = false;
let sharedOpenEpoch = 0;
const sharedShiftedElements = new Set();
// const sharedOrgStickyWatched = new WeakSet(); // disabled — LinkedIn-specific, see below
const SHARED_MUTATION_DEBOUNCE_MS = 250;
const SHARED_CONSTRAIN_DEBOUNCE_MS = 150;
const SHARED_HEADER_MAX_DEPTH = 10;
const SHARED_MIN_CONSTRAIN_HEIGHT = 100;
const SHARED_FIGHT_LIMIT = 3;
const SHARED_FIGHT_WINDOW_MS = 3000;

// A transform/filter/etc. on an ancestor makes position:fixed relative to that
// ancestor instead of the viewport, so shifting the ancestor is enough.
function sharedCreatesFixedContainingBlock(el) {
    const cs = getComputedStyle(el);
    if (cs.transform !== "none") return true;
    if (cs.perspective !== "none") return true;
    if (cs.filter !== "none") return true;
    if (cs.backdropFilter !== "none") return true;
    const contain = cs.contain;
    if (contain && contain !== "none" && /\b(paint|layout|strict|content)\b/.test(contain)) {
        return true;
    }
    return false;
}

function sharedHasFixedContainingBlockAncestor(el) {
    let node = el.parentElement;
    while (node) {
        if (!(node instanceof HTMLElement)) break;
        if (sharedCreatesFixedContainingBlock(node)) return true;
        node = node.parentElement;
    }
    return false;
}

function sharedFindNearestShiftedFixedAncestor(el) {
    let node = el.parentElement;
    while (node) {
        if (!(node instanceof HTMLElement)) break;
        if (node.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) {
            const pcs = getComputedStyle(node);
            if (pcs.position === "fixed" || pcs.position === "sticky") return node;
        }
        node = node.parentElement;
    }
    return null;
}

function sharedFindNearestShiftedAncestor(el) {
    let node = el.parentElement;
    while (node) {
        if (node instanceof HTMLElement && node.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) {
            return node;
        }
        node = node.parentElement;
    }
    return null;
}

// Google SRP (and similar) place the search bar in position:absolute at top:20px.
// Body padding does not move it when the offset parent is the initial containing block.
function sharedIsAbsoluteTopHeader(el, cs) {
    if (!(el instanceof HTMLElement)) return false;
    cs = cs || getComputedStyle(el);
    if (cs.position !== "absolute") return false;
    if (el.id === "searchform") return true;
    const rect = el.getBoundingClientRect();
    if (rect.height <= 0 || rect.top > sharedBarHeight() + 8) return false;
    const top = parseFloat(cs.top);
    if (isNaN(top) || cs.top === "auto" || top >= SHARED_TASKBAR.MAX_SHIFT_TOP) return false;
    return rect.width > window.innerWidth * 0.35;
}

function sharedShouldSkipAbsoluteShift(el, cs) {
    if (!sharedIsAbsoluteTopHeader(el, cs)) return true;
    if (sharedFindNearestShiftedAncestor(el)) return true;
    return false;
}

function sharedFindPositionedAncestorOrSelf(el) {
    let node = el;
    while (node) {
        if (node instanceof HTMLElement) {
            const pcs = getComputedStyle(node);
            if (pcs.position === "fixed" || pcs.position === "sticky") return node;
        }
        node = node.parentElement;
    }
    return null;
}

// Disabled — LinkedIn-specific, being phased out in favor of general handling.
// function sharedIsOrgStickyCard(el) {
//     return !!el.closest(".org-sticky-top-card, .org-sticky-top-card__container");
// }

// function sharedRefreshPageFlags() {
//     sharedLinkedInJobSearchPage = !!document.querySelector('[componentkey="JobsSearchFilters"]');
// }

// Google Sheets maps clicks to cells using its own layout math. Body padding and
// shifting fixed/sticky nodes moves the grid visually but not hit-testing, so
// clicks land ~one taskbar height low (often two rows down).
function sharedIsGoogleSheetsPage() {
    return location.hostname === "docs.google.com" &&
        location.pathname.includes("/spreadsheets/");
}

function sharedShouldSkipPageShifting() {
    return sharedIsGoogleSheetsPage();
}

// Disabled — LinkedIn-specific, being phased out in favor of general handling.
// function sharedIsLinkedInJobSearchPage() {
//     if (sharedLinkedInJobSearchPage === null) sharedRefreshPageFlags();
//     return sharedLinkedInJobSearchPage;
// }

// function sharedIsJobSearchFilter(el) {
//     if (sharedIsOrgStickyCard(el)) return false;
//     if (!sharedIsLinkedInJobSearchPage()) return false;
//     if (el.closest('[componentkey="JobsSearchFilters"], [class*="jobs-search"], [class*="search-results"]')) {
//         return true;
//     }
//     // Job-search filter toolbar is sticky/fixed itself; JobsSearchFilters is nested
//     // inside it, so closest() on the toolbar element never sees that marker.
//     if (el.getAttribute("role") === "toolbar" && el.querySelector('[componentkey="JobsSearchFilters"]')) {
//         return true;
//     }
//     return false;
// }

// function sharedIsLinkedInProfilePage() {
//     return /linkedin\.com/i.test(location.hostname) &&
//         /^\/in\/[^/?#]+/i.test(location.pathname);
// }

// Profile sub-toolbar (name / Resources / Enhance profile) is scroll-reveal chrome.
// LinkedIn hides it at scroll top via its own top/transform; forcing top: 50px
// fights that toggle and flickers at the page top.
// function sharedIsLinkedInProfileScrollToolbar(el) {
//     if (!sharedIsLinkedInProfilePage()) return false;
//     const bar = el.getAttribute("role") === "toolbar" ? el : el.closest('[role="toolbar"]');
//     if (!(bar instanceof HTMLElement)) return false;
//     return !sharedIsNavChrome(bar);
// }

// Secondary sticky rows on job search (filter toolbar, "Jobs based on your
// preferences", results chrome) track the shifted main nav — only top ~0 needs
// an independent bump. Must not call sharedGetEffectiveTop (that calls
// sharedIsLikelyPrimaryNav, which calls back here).
// function sharedIsJobSearchSecondaryHeader(el, cs) {
//     if (!sharedIsLinkedInJobSearchPage()) return false;
//     cs = cs || getComputedStyle(el);
//     if (cs.position !== "fixed" && cs.position !== "sticky") return false;
//
//     const rect = el.getBoundingClientRect();
//     if (rect.height > 0) {
//         if (rect.top >= SHARED_TASKBAR.HEIGHT - 4) return true;
//         return false;
//     }
//
//     let top = parseFloat(cs.top);
//     if (!isNaN(top) && cs.top !== "auto") {
//         return top >= SHARED_TASKBAR.HEIGHT - 4;
//     }
//
//     const inset = parseFloat(cs.getPropertyValue("inset-block-start"));
//     if (!isNaN(inset) && cs.getPropertyValue("inset-block-start") !== "auto") {
//         return inset >= SHARED_TASKBAR.HEIGHT - 4;
//     }
//
//     // Unknown top — treat as secondary (prior sharedGetEffectiveTop null path).
//     return true;
// }

function sharedIsStuckInHeaderZone(el) {
    const rect = el.getBoundingClientRect();
    if (rect.height <= 0) return false;
    const min = sharedBarHeight() - 4;
    const max = SHARED_TASKBAR.MAX_SHIFT_TOP + sharedBarHeight();
    return rect.top >= min && rect.top <= max;
}

// When body padding has already moved in-flow sticky chrome to the taskbar band,
// rect.top ≈ HEIGHT but the CSS top base is still 0 — do not treat rect as base.
function sharedRectTopToShiftBase(el, cs, rectTop) {
    cs = cs || getComputedStyle(el);
    if (cs.position === "sticky" && rectTop >= sharedBarHeight() - 4 &&
        rectTop <= sharedBarHeight() + 12) {
        return 0;
    }
    if (rectTop <= sharedBarHeight() + 8) return rectTop;
    if (cs.position === "sticky") return 0;
    return rectTop;
}

function sharedGetEffectiveTop(el, cs) {
    cs = cs || getComputedStyle(el);
    let top = parseFloat(cs.top);
    if (!isNaN(top) && cs.top !== "auto") return top;

    const inset = parseFloat(cs.getPropertyValue("inset-block-start"));
    if (!isNaN(inset) && cs.getPropertyValue("inset-block-start") !== "auto") return inset;

    // Disabled — LinkedIn-specific (org-sticky-card) branch, see sharedIsOrgStickyCard.
    // if (sharedIsOrgStickyCard(el) && sharedIsStuckInHeaderZone(el)) {
    //     const rectTop = Math.round(el.getBoundingClientRect().top);
    //     if (el.hasAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR)) {
    //         return sharedGetShiftBaseTop(el) + SHARED_TASKBAR.HEIGHT;
    //     }
    //     return rectTop;
    // }

    if (sharedIsLikelyPrimaryNav(el, cs)) {
        const rectTop = Math.round(el.getBoundingClientRect().top);
        if (el.hasAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR)) {
            return sharedGetShiftBaseTop(el) + sharedBarHeight();
        }
        const base = sharedRectTopToShiftBase(el, cs, rectTop);
        return base < SHARED_TASKBAR.MAX_SHIFT_TOP ? base : 0;
    }

    if (sharedIsTopViewportChrome(el, cs)) {
        const rectTop = Math.round(el.getBoundingClientRect().top);
        if (el.hasAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR)) {
            return sharedGetShiftBaseTop(el) + sharedBarHeight();
        }
        const base = sharedRectTopToShiftBase(el, cs, rectTop);
        return base <= SHARED_TASKBAR.MAX_SHIFT_TOP ? base : 0;
    }

    if (sharedIsAbsoluteTopHeader(el, cs)) {
        const rectTop = Math.round(el.getBoundingClientRect().top);
        if (el.hasAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR)) {
            return sharedGetShiftBaseTop(el) + sharedBarHeight();
        }
        let top = parseFloat(cs.top);
        if (!isNaN(top) && cs.top !== "auto") return top;
        return sharedRectTopToShiftBase(el, cs, rectTop);
    }

    return null;
}

function sharedIsLikelyPrimaryNav(el, cs) {
    cs = cs || getComputedStyle(el);
    if (cs.position !== "fixed" && cs.position !== "sticky") return false;
    // LinkedIn-specific guards disabled — see sharedIsOrgStickyCard,
    // sharedIsJobSearchFilter, sharedIsLinkedInProfileScrollToolbar,
    // sharedIsJobSearchSecondaryHeader above.
    // if (sharedIsOrgStickyCard(el)) return false;
    // if (sharedIsJobSearchFilter(el)) return false;
    // if (sharedIsLinkedInProfileScrollToolbar(el)) return false;
    // if (sharedIsJobSearchSecondaryHeader(el, cs)) return false;
    if (el.closest("header, [role=\"banner\"]")) return true; // #global-nav, .global-nav (LinkedIn) removed
    const rect = el.getBoundingClientRect();
    return rect.top >= 0 && rect.top <= sharedBarHeight() && rect.width > window.innerWidth * 0.4;
}

function sharedOverflowCreatesScrollport(cs) {
    return cs.overflow === "auto" || cs.overflow === "scroll" || cs.overflow === "overlay" ||
        cs.overflowY === "auto" || cs.overflowY === "scroll" || cs.overflowY === "overlay";
}

// Nearest ancestor whose overflow establishes the stick container for position:sticky.
function sharedFindStickyScrollport(el) {
    let node = el.parentElement;
    while (node) {
        if (!(node instanceof HTMLElement)) break;
        if (sharedOverflowCreatesScrollport(getComputedStyle(node))) return node;
        node = node.parentElement;
    }
    return document.scrollingElement || document.documentElement;
}

// Sticky in a nested scrollport (e.g. SPA main panel) is offset by body padding alone.
// Sticky on the document scrollport (e.g. forum nav) needs top so its stick point clears
// our bar — padding does not change where sticky;top:0 pins when the page scrolls.
function sharedStickyUsesDocumentScroll(el) {
    const scrollport = sharedFindStickyScrollport(el);
    return scrollport === document.body ||
        scrollport === document.documentElement ||
        (document.scrollingElement && scrollport === document.scrollingElement);
}

// Full-width bar pinned to the top band of the viewport (not a anchored popup).
function sharedIsTopViewportChrome(el, cs) {
    cs = cs || getComputedStyle(el);
    if (cs.position !== "fixed" && cs.position !== "sticky") return false;
    const rect = el.getBoundingClientRect();
    if (rect.height <= 0 || rect.width < window.innerWidth * 0.4) return false;
    return rect.top >= 0 && rect.top <= sharedBarHeight() + 8;
}

// Full-width top chrome we still shift (nav, stacked sub-headers). Narrow overlays
// and menus are usually app-positioned relative to already-shifted layout.
function sharedIsViewportHeaderCandidate(el, cs, rect) {
    cs = cs || getComputedStyle(el);
    if (sharedIsLikelyPrimaryNav(el, cs)) return true;
    rect = rect || el.getBoundingClientRect();
    if (rect.height <= 0) return false;
    return rect.top < SHARED_TASKBAR.MAX_SHIFT_TOP && rect.width > window.innerWidth * 0.4;
}

// Popups/menus positioned below the taskbar zone (often via anchor getBoundingClientRect
// after body padding or a shifted header) — shifting again double-offsets them.
function sharedIsAppPositionedOverlay(el, cs) {
    // Once we've shifted this element ourselves, its live top IS base+HEIGHT, which
    // would otherwise look identical to a page-native overlay already sitting below
    // the bar — judge it by the pre-shift base instead, or we'd mistake our own
    // shift for something to leave alone, restore it, see the un-shifted top on the
    // next pass, shift it again, and ping-pong forever.
    if (el.hasAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR)) {
        return sharedGetShiftBaseTop(el) >= sharedBarHeight() - 4;
    }

    // Not yet tracked: if it's already RENDERING at/below the bar band, some other
    // arrangement — body padding, a row stacked after an already-shifted sibling,
    // a sticky element whose un-stuck flow position was already pushed down — has
    // already cleared it. This checks the actual rendered rect, not the declared
    // CSS top (a sticky element can declare top:0 and still render lower once
    // ambient page shifting has moved it). A genuinely still-pinned top:0 bar is
    // still rendering at 0 here, so it falls through to the header-candidate
    // checks below untouched.
    const rect = el.getBoundingClientRect();
    if (rect.height > 0 && rect.top >= sharedBarHeight() - 4) return true;

    if (sharedIsViewportHeaderCandidate(el, cs)) return false;
    if (sharedIsTopViewportChrome(el, cs)) return false;

    const top = sharedGetEffectiveTop(el, cs);
    if (top !== null) return top >= sharedBarHeight() - 4;

    return rect.top >= sharedBarHeight() - 4;
}

function sharedRectsLookAnchoredToReference(popupRect, refRect) {
    const vMargin = 96;
    const overlap = Math.min(popupRect.right, refRect.right) -
        Math.max(popupRect.left, refRect.left);
    if (overlap <= Math.min(popupRect.width, refRect.width) * 0.15) return false;
    if (Math.abs(popupRect.top - refRect.bottom) <= vMargin) return true;
    if (popupRect.top >= refRect.top - 8 && popupRect.top <= refRect.bottom + vMargin) {
        return true;
    }
    return false;
}

function sharedFindAnchoredShiftedReference(el) {
    if (sharedIsViewportHeaderCandidate(el, getComputedStyle(el))) return null;
    if (sharedIsTopViewportChrome(el, getComputedStyle(el))) return null;

    const rect = el.getBoundingClientRect();
    if (rect.height <= 0 || rect.width <= 0) return null;

    for (const shifted of sharedShiftedElements) {
        if (!shifted.isConnected || shifted === el) continue;
        if (shifted.contains(el)) continue;

        const refRect = shifted.getBoundingClientRect();
        if (refRect.height <= 0) continue;
        if (sharedRectsLookAnchoredToReference(rect, refRect)) return shifted;
    }
    return null;
}

// Disabled — LinkedIn-specific, being phased out in favor of general handling.
// function sharedFindLinkedInGlobalNav() {
//     if (!/linkedin\.com/i.test(location.hostname)) return null;
//     const selectors = [
//         "#global-nav",
//         "header.global-nav",
//         "header[role=\"banner\"]",
//         ".global-nav__nav",
//         "nav[aria-label=\"Primary\"]",
//     ];
//     for (const sel of selectors) {
//         const el = document.querySelector(sel);
//         if (el instanceof HTMLElement) {
//             return sharedFindPositionedAncestorOrSelf(el) || el;
//         }
//     }
//     return null;
// }

// Cheap upkeep for the main nav and already-tracked headers — not a full-page rescan.
function sharedShiftPrimaryHeaders() {
    // Site-specific nav lookup disabled — see sharedFindLinkedInGlobalNav below.
    // General shift tracking below (sharedShiftedElements loop) covers headers
    // discovered via the full-tree walk / mutation scan instead.
    // const nav = sharedFindLinkedInGlobalNav();
    // if (nav) sharedShiftFixedElement(nav);
    const googleSearch = document.getElementById("searchform");
    if (googleSearch) sharedShiftFixedElement(googleSearch);
    for (const el of sharedShiftedElements) {
        if (el.isConnected) sharedShiftFixedElement(el);
    }
}

function sharedIsNavChrome(el) {
    return !!el.closest("header, [role=\"banner\"]"); // #global-nav, .global-nav (LinkedIn) removed
}

function sharedFindOutermostOverflowingAncestor(el, viewportH) {
    let current = el;
    while (current.parentElement && current.parentElement !== document.body) {
        const parent = current.parentElement;
        const pr = parent.getBoundingClientRect();
        const cr = current.getBoundingClientRect();
        if (pr.bottom > viewportH + 2 && Math.abs(pr.height - cr.height) < 8) {
            current = parent;
        } else {
            break;
        }
    }
    return current;
}

function sharedRestoreViewportConstraint(el) {
    const prevMax = el.getAttribute(SHARED_TASKBAR.PREV_MAX_HEIGHT_ATTR);
    el.style.removeProperty("max-height");
    if (prevMax) el.style.setProperty("max-height", prevMax);
    el.removeAttribute(SHARED_TASKBAR.PREV_MAX_HEIGHT_ATTR);

    const prevMin = el.getAttribute(SHARED_TASKBAR.PREV_MIN_HEIGHT_ATTR);
    el.style.removeProperty("min-height");
    if (prevMin) el.style.setProperty("min-height", prevMin);
    el.removeAttribute(SHARED_TASKBAR.PREV_MIN_HEIGHT_ATTR);

    const prevHeight = el.getAttribute(SHARED_TASKBAR.PREV_HEIGHT_ATTR);
    el.style.removeProperty("height");
    if (prevHeight) el.style.setProperty("height", prevHeight);
    el.removeAttribute(SHARED_TASKBAR.PREV_HEIGHT_ATTR);

    const prevOverflowY = el.getAttribute(SHARED_TASKBAR.PREV_OVERFLOW_Y_ATTR);
    el.style.removeProperty("overflow-y");
    if (prevOverflowY) el.style.setProperty("overflow-y", prevOverflowY);
    el.removeAttribute(SHARED_TASKBAR.PREV_OVERFLOW_Y_ATTR);

    el.removeAttribute(SHARED_TASKBAR.CONSTRAINED_ATTR);
}

function sharedApplyViewportConstraint(el, maxH, cs) {
    cs = cs || getComputedStyle(el);
    if (!el.hasAttribute(SHARED_TASKBAR.PREV_MAX_HEIGHT_ATTR)) {
        el.setAttribute(SHARED_TASKBAR.PREV_MAX_HEIGHT_ATTR, el.style.getPropertyValue("max-height") || "");
    }
    el.style.setProperty("max-height", maxH + "px", "important");

    const minH = parseFloat(cs.minHeight);
    if (!isNaN(minH) && minH > maxH) {
        if (!el.hasAttribute(SHARED_TASKBAR.PREV_MIN_HEIGHT_ATTR)) {
            el.setAttribute(SHARED_TASKBAR.PREV_MIN_HEIGHT_ATTR, el.style.getPropertyValue("min-height") || "");
        }
        el.style.setProperty("min-height", "0px", "important");
    }

    const height = parseFloat(cs.height);
    if (!isNaN(height) && height > maxH) {
        if (!el.hasAttribute(SHARED_TASKBAR.PREV_HEIGHT_ATTR)) {
            el.setAttribute(SHARED_TASKBAR.PREV_HEIGHT_ATTR, el.style.getPropertyValue("height") || "");
        }
        el.style.setProperty("height", maxH + "px", "important");
    }

    if (cs.overflowY === "visible" || cs.overflowY === "clip") {
        if (!el.hasAttribute(SHARED_TASKBAR.PREV_OVERFLOW_Y_ATTR)) {
            el.setAttribute(SHARED_TASKBAR.PREV_OVERFLOW_Y_ATTR, el.style.getPropertyValue("overflow-y") || "");
        }
        el.style.setProperty("overflow-y", "auto", "important");
    }

    el.setAttribute(SHARED_TASKBAR.CONSTRAINED_ATTR, "1");
}

function sharedConstrainElementToViewport(el, viewportH) {
    if (!(el instanceof HTMLElement)) return;
    if (el.id === SHARED_TASKBAR.HOST_ID || sharedIsNavChrome(el)) return;

    const rect = el.getBoundingClientRect();
    if (rect.height < SHARED_MIN_CONSTRAIN_HEIGHT || rect.top >= viewportH) return;

    const overflow = rect.bottom - viewportH;
    if (overflow <= 2) {
        if (el.hasAttribute(SHARED_TASKBAR.CONSTRAINED_ATTR)) sharedRestoreViewportConstraint(el);
        return;
    }

    const maxH = Math.max(SHARED_MIN_CONSTRAIN_HEIGHT, Math.floor(viewportH - rect.top));
    const cs = getComputedStyle(el);
    const currentMax = parseFloat(cs.maxHeight);
    if (!isNaN(currentMax) && currentMax <= maxH + 1 && el.hasAttribute(SHARED_TASKBAR.CONSTRAINED_ATTR)) return;

    sharedApplyViewportConstraint(el, maxH, cs);
}

function sharedRestoreViewportFitTrim(el) {
    sharedRestoreViewportConstraint(el);
    el.removeAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR);
}

// Shrink a viewport-sized shell without turning it into a nested scroller.
function sharedApplyViewportFitTrim(el, maxH, cs) {
    cs = cs || getComputedStyle(el);
    const forceHeight = sharedIsExplicitViewportShell(el);

    if (el.hasAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR)) {
        const curH = parseFloat(el.style.getPropertyValue("height"));
        const curMax = parseFloat(el.style.getPropertyValue("max-height"));
        if (
            (!isNaN(curH) && Math.abs(curH - maxH) <= 1) ||
            (!isNaN(curMax) && Math.abs(curMax - maxH) <= 1)
        ) {
            return;
        }
    }

    if (!el.hasAttribute(SHARED_TASKBAR.PREV_MAX_HEIGHT_ATTR)) {
        el.setAttribute(SHARED_TASKBAR.PREV_MAX_HEIGHT_ATTR, el.style.getPropertyValue("max-height") || "");
    }
    el.style.setProperty("max-height", maxH + "px", "important");

    const minH = parseFloat(cs.minHeight);
    if (!isNaN(minH) && minH > maxH) {
        if (!el.hasAttribute(SHARED_TASKBAR.PREV_MIN_HEIGHT_ATTR)) {
            el.setAttribute(SHARED_TASKBAR.PREV_MIN_HEIGHT_ATTR, el.style.getPropertyValue("min-height") || "");
        }
        el.style.setProperty("min-height", "0px", "important");
    }

    const height = parseFloat(cs.height);
    if (forceHeight || (!isNaN(height) && height > maxH)) {
        if (!el.hasAttribute(SHARED_TASKBAR.PREV_HEIGHT_ATTR)) {
            el.setAttribute(SHARED_TASKBAR.PREV_HEIGHT_ATTR, el.style.getPropertyValue("height") || "");
        }
        el.style.setProperty("height", maxH + "px", "important");
    }

    el.setAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR, "1");
}

function sharedPageIsNaturallyScrollable() {
    const doc = document.scrollingElement || document.documentElement;
    return doc.scrollHeight > window.innerHeight + sharedBarHeight() + 64;
}

// Tailwind / modern apps often pin the UI with h-svh, h-screen, etc.
function sharedIsExplicitViewportShell(el) {
    if (!(el instanceof HTMLElement)) return false;
    if (el === document.body || el === document.documentElement) return false;
    if (el.hasAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR)) return true;

    const cls = el.className;
    if (typeof cls === "string" && /\b(?:min-h-)?h-(?:s|l|d)?vh\b|\b(?:min-h-)?h-screen\b/.test(cls)) {
        return true;
    }

    const cs = getComputedStyle(el);
    return /(?:^|\s)(?:100|[1-9]\d*(?:\.\d+)?)(?:svh|lvh|dvh|vh)\b/.test(cs.height)
        || /(?:^|\s)(?:100|[1-9]\d*(?:\.\d+)?)(?:svh|lvh|dvh|vh)\b/.test(cs.minHeight);
}

// True when overflow is ~one taskbar band from a 100vh shell (ChatGPT-style),
// not a long document that genuinely exceeds the viewport.
function sharedShouldTrimViewportFit(el, rect, viewportH) {
    if (el === document.body || el === document.documentElement) return false;
    if (rect.top > sharedBarHeight() + 8) return false;

    const overflow = rect.bottom - viewportH;
    if (sharedIsExplicitViewportShell(el)) {
        // Once trimmed the shell no longer overflows, but we must stay trimmed.
        if (el.hasAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR)) return true;
        return overflow > 2 && rect.height >= viewportH - sharedBarHeight() - 32;
    }

    if (overflow <= 2 || overflow > sharedBarHeight() + 12) return false;
    if (rect.height < viewportH - 32) return false;
    if (rect.height > viewportH + sharedBarHeight() + 12) return false;
    return true;
}

function sharedCollectViewportFitShells() {
    const shells = [];
    const seen = new Set();
    const add = (el) => {
        if (!(el instanceof HTMLElement) || seen.has(el)) return;
        if (el.id === SHARED_TASKBAR.HOST_ID) return;
        seen.add(el);
        shells.push(el);
    };

    if (document.body instanceof HTMLElement) {
        const stack = [[document.body, 0]];
        while (stack.length) {
            const pair = stack.pop();
            const node = pair[0];
            const depth = pair[1];
            if (!(node instanceof HTMLElement) || depth > 6) continue;
            if (sharedIsExplicitViewportShell(node)) add(node);
            for (const child of node.children) stack.push([child, depth + 1]);
        }
    }

    for (const sel of ["#root", "#__next", "#app", "#mount"]) {
        add(document.querySelector(sel));
    }
    if (document.body instanceof HTMLElement) {
        for (const child of document.body.children) {
            if (child instanceof HTMLElement && child.id !== SHARED_TASKBAR.HOST_ID) {
                add(child);
            }
        }
    }
    return shells;
}

function sharedTrimViewportFitShells(viewportH) {
    const allShells = sharedCollectViewportFitShells();
    const explicitShells = allShells.filter(sharedIsExplicitViewportShell);
    const shells = explicitShells.length ? explicitShells : allShells;
    const naturallyScrollable = sharedPageIsNaturallyScrollable();

    const trimmed = new Set();
    for (const el of shells) {
        const explicit = sharedIsExplicitViewportShell(el);
        if (naturallyScrollable && !explicit) {
            if (el.hasAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR)) sharedRestoreViewportFitTrim(el);
            continue;
        }

        const rect = el.getBoundingClientRect();
        if (!sharedShouldTrimViewportFit(el, rect, viewportH)) {
            if (el.hasAttribute(SHARED_TASKBAR.FIT_TRIM_ATTR)) sharedRestoreViewportFitTrim(el);
            continue;
        }
        const maxH = Math.max(SHARED_MIN_CONSTRAIN_HEIGHT, Math.floor(viewportH - rect.top));
        sharedApplyViewportFitTrim(el, maxH, getComputedStyle(el));
        trimmed.add(el);
    }

    document.querySelectorAll("[" + SHARED_TASKBAR.FIT_TRIM_ATTR + "]").forEach((el) => {
        if (!trimmed.has(el)) sharedRestoreViewportFitTrim(el);
    });
}

function sharedCollectViewportConstraintCandidates() {
    const candidates = new Set();
    for (const el of sharedShiftedElements) {
        if (el.isConnected) candidates.add(el);
    }
    document.querySelectorAll("[" + SHARED_TASKBAR.CONSTRAINED_ATTR + "]").forEach((el) => candidates.add(el));
    document.querySelectorAll('[style*="vh"]').forEach((el) => candidates.add(el));

    const main = document.querySelector("main, [role=\"main\"]");
    if (main instanceof HTMLElement) {
        const stack = [[main, 0]];
        while (stack.length) {
            const pair = stack.pop();
            const node = pair[0];
            const depth = pair[1];
            if (!(node instanceof HTMLElement) || depth > 4) continue;
            candidates.add(node);
            for (const child of node.children) stack.push([child, depth + 1]);
        }
    }
    return candidates;
}

// Disabled — LinkedIn-specific, being phased out in favor of general handling.
// LinkedIn job-search panels use 100vh and may need inner scroll once shifted.
// function sharedConstrainLinkedInViewportOverflow(viewportH) {
//     const roots = new Set();
//     for (const el of sharedCollectViewportConstraintCandidates()) {
//         if (!(el instanceof HTMLElement) || sharedIsNavChrome(el)) continue;
//         const rect = el.getBoundingClientRect();
//         if (rect.bottom <= viewportH + 2 || rect.height < SHARED_MIN_CONSTRAIN_HEIGHT) continue;
//         roots.add(sharedFindOutermostOverflowingAncestor(el, viewportH));
//     }
//     for (const el of roots) sharedConstrainElementToViewport(el, viewportH);
// }

function sharedConstrainViewportOverflow() {
    if (!document.getElementById(SHARED_TASKBAR.HOST_ID)) return;
    if (sharedShouldSkipPageShifting()) return;

    const viewportH = window.innerHeight;
    // LinkedIn-specific constrain pass disabled — see sharedConstrainLinkedInViewportOverflow.
    // if (sharedIsLinkedInJobSearchPage()) {
    //     sharedConstrainLinkedInViewportOverflow(viewportH);
    // }
    sharedTrimViewportFitShells(viewportH);
}

function sharedScheduleViewportConstrain() {
    if (!document.getElementById(SHARED_TASKBAR.HOST_ID)) return;
    if (sharedShouldSkipPageShifting()) return;
    if (sharedConstrainTimer) return;
    sharedConstrainTimer = setTimeout(() => {
        sharedConstrainTimer = null;
        sharedConstrainViewportOverflow();
    }, SHARED_CONSTRAIN_DEBOUNCE_MS);
}

function sharedApplyTop(el, topPx) {
    el.style.setProperty("top", topPx + "px", "important");
}

// Disabled block — LinkedIn-specific (org-sticky-card) IntersectionObserver
// subsystem, being phased out in favor of general handling.
// function sharedFindPositionedForRoot(root) {
//     if (!(root instanceof HTMLElement)) return null;
//     const pcs = getComputedStyle(root);
//     if (pcs.position === "fixed" || pcs.position === "sticky") return root;
//     for (const child of root.children) {
//         if (!(child instanceof HTMLElement)) continue;
//         const ccs = getComputedStyle(child);
//         if (ccs.position === "fixed" || ccs.position === "sticky") return child;
//     }
//     return sharedFindPositionedAncestorOrSelf(root);
// }

// function sharedEnsureOrgStickyObserver() {
//     if (sharedOrgStickyObserver) return;
//     sharedOrgStickyObserver = new IntersectionObserver((entries) => {
//         for (const entry of entries) {
//             const positioned = sharedFindPositionedForRoot(entry.target);
//             if (positioned) sharedShiftFixedElement(positioned);
//         }
//     }, {
//         rootMargin: (-SHARED_TASKBAR.HEIGHT) + "px 0px 0px 0px",
//         threshold: [0, 0.01, 1],
//     });
// }

// function sharedWatchOrgStickyRoot(root) {
//     if (!(root instanceof HTMLElement) || sharedOrgStickyWatched.has(root)) return;
//     sharedEnsureOrgStickyObserver();
//     sharedOrgStickyWatched.add(root);
//     sharedOrgStickyObserver.observe(root);
//     const positioned = sharedFindPositionedForRoot(root);
//     if (positioned) sharedShiftFixedElement(positioned);
// }

// function sharedDiscoverOrgStickyCards(root) {
//     if (!(root instanceof HTMLElement)) return;
//     if (root.matches(".org-sticky-top-card, .org-sticky-top-card__container")) {
//         sharedWatchOrgStickyRoot(root);
//     }
//     if (!root.querySelectorAll) return;
//     root.querySelectorAll(".org-sticky-top-card, .org-sticky-top-card__container").forEach(sharedWatchOrgStickyRoot);
// }

function sharedGetShiftBaseTop(el) {
    const base = parseFloat(el.getAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR));
    return isNaN(base) ? 0 : base;
}

function sharedGetShiftRowHeight(el) {
    const stored = parseFloat(el.getAttribute(SHARED_TASKBAR.SHIFT_ROW_HEIGHT_ATTR));
    if (!isNaN(stored) && stored > 0) return stored;
    return el.getBoundingClientRect().height;
}

// Keep the smallest height seen for a shifted header row so an opened sub-bar
// does not inflate the stored value and block re-shift after scroll up/down.
function sharedRecordShiftRowHeight(el) {
    const measured = Math.round(el.getBoundingClientRect().height);
    if (measured <= 0) return;
    const prev = parseFloat(el.getAttribute(SHARED_TASKBAR.SHIFT_ROW_HEIGHT_ATTR));
    if (isNaN(prev) || measured < prev) {
        el.setAttribute(SHARED_TASKBAR.SHIFT_ROW_HEIGHT_ATTR, String(measured));
    }
}

// Stacked sub-headers (e.g. LinkedIn company bar) pin below the main row with an
// explicit top/inset and need their own bump even inside an already-shifted ancestor.
function sharedIsStackedSubHeaderRow(el, cs) {
    // LinkedIn-specific guard disabled — see sharedIsOrgStickyCard.
    // if (sharedIsOrgStickyCard(el)) return true;

    cs = cs || getComputedStyle(el);
    let top = parseFloat(cs.top);
    if (!isNaN(top) && cs.top !== "auto" && top >= sharedBarHeight() - 4) return true;

    const inset = parseFloat(cs.getPropertyValue("inset-block-start"));
    if (!isNaN(inset) && cs.getPropertyValue("inset-block-start") !== "auto" &&
        inset >= sharedBarHeight() - 4) {
        return true;
    }
    return false;
}

// True once we've had to re-correct this element's top SHARED_FIGHT_LIMIT times
// within SHARED_FIGHT_WINDOW_MS — a sign something else (a scroll-reveal toggle,
// an animation) is actively driving the same property and will never settle.
function sharedRecordShiftFight(el) {
    const now = Date.now();
    const lastTs = parseFloat(el.getAttribute(SHARED_TASKBAR.FIGHT_TS_ATTR));
    let count = parseFloat(el.getAttribute(SHARED_TASKBAR.FIGHT_COUNT_ATTR)) || 0;
    if (isNaN(lastTs) || now - lastTs > SHARED_FIGHT_WINDOW_MS) {
        count = 0;
    }
    count += 1;
    el.setAttribute(SHARED_TASKBAR.FIGHT_COUNT_ATTR, String(count));
    el.setAttribute(SHARED_TASKBAR.FIGHT_TS_ATTR, String(now));
    return count >= SHARED_FIGHT_LIMIT;
}

// Skip when a shifted fixed/sticky ancestor already moves this element. The only
// descendant exception is a stacked sub-row with explicit top/inset ≥ nav height.
function sharedShouldSkipShift(el, cs) {
    cs = cs || getComputedStyle(el);
    if (cs.position !== "fixed" && cs.position !== "sticky") return true;
    if (el.hasAttribute(SHARED_TASKBAR.QUARANTINED_ATTR)) return true;

    // LinkedIn-specific guards disabled — see sharedIsOrgStickyCard,
    // sharedIsJobSearchFilter, sharedIsLinkedInProfileScrollToolbar,
    // sharedIsJobSearchSecondaryHeader above.
    // if (sharedIsOrgStickyCard(el)) return false;
    // if (sharedIsJobSearchFilter(el)) return true;
    // if (sharedIsLinkedInProfileScrollToolbar(el)) return true;
    // if (sharedIsJobSearchSecondaryHeader(el, cs)) return true;
    if (sharedIsAppPositionedOverlay(el, cs)) return true;
    if (sharedFindAnchoredShiftedReference(el)) return true;

    // Nested scrollport: body padding is enough. Document scrollport: set top so the
    // stick point clears our bar when the page scrolls (forum navs, etc.).
    if (cs.position === "sticky" && !sharedIsStackedSubHeaderRow(el, cs)) {
        if (!sharedStickyUsesDocumentScroll(el)) return true;
    }

    if (sharedHasFixedContainingBlockAncestor(el)) return true;

    const ancestor = sharedFindNearestShiftedFixedAncestor(el);
    if (ancestor && !sharedIsStackedSubHeaderRow(el, cs)) return true;

    return false;
}

function sharedRestoreShiftedElement(el) {
    const prev = el.getAttribute(SHARED_TASKBAR.SHIFTED_ATTR);
    el.style.removeProperty("top");
    if (prev) el.style.setProperty("top", prev);
    el.removeAttribute(SHARED_TASKBAR.SHIFTED_ATTR);
    el.removeAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR);
    sharedShiftedElements.delete(el);
}

function sharedShiftFixedElement(el, cs) {
    if (!(el instanceof HTMLElement)) return;
    if (el.id === SHARED_TASKBAR.HOST_ID) return;
    cs = cs || getComputedStyle(el);

    const isAbsoluteHeader = sharedIsAbsoluteTopHeader(el, cs);

    if (isAbsoluteHeader) {
        if (sharedShouldSkipAbsoluteShift(el, cs)) {
            if (el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) sharedRestoreShiftedElement(el);
            return;
        }
    } else {
        if (sharedShouldSkipShift(el, cs)) {
            if (el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) sharedRestoreShiftedElement(el);
            return;
        }

        if (cs.position !== "fixed" && cs.position !== "sticky") {
            // Page stopped sticking this element; restore so we can shift again later.
            if (el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) sharedRestoreShiftedElement(el);
            return;
        }

        // LinkedIn-specific guard disabled — see sharedIsOrgStickyCard.
        // if (sharedIsOrgStickyCard(el) && !sharedIsStuckInHeaderZone(el)) {
        //     if (el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) sharedRestoreShiftedElement(el);
        //     return;
        // }
    }

    if (el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) {
        // Always restore to the original base + bar height. Do not add HEIGHT
        // to the current computed top (that double-shifts after Google scroll).
        let base = sharedGetShiftBaseTop(el);
        if (cs.position === "sticky" && base >= sharedBarHeight() - 4 &&
            base <= sharedBarHeight() + 12) {
            const prevTop = el.getAttribute(SHARED_TASKBAR.SHIFTED_ATTR) || "";
            if (prevTop === "" || prevTop === "0" || prevTop === "0px") {
                base = 0;
                el.setAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR, "0");
            }
        }
        const target = base + sharedBarHeight();
        const current = sharedGetEffectiveTop(el, cs);
        if (current === null || Math.abs(current - target) > 0.5) {
            // Something other than us changed this element's top since we last set
            // it (e.g. a scroll-reveal toggle). Re-applying wins the immediate
            // fight but not the next one — if it keeps happening, give up instead
            // of flickering forever.
            if (sharedRecordShiftFight(el)) {
                el.setAttribute(SHARED_TASKBAR.QUARANTINED_ATTR, "1");
                sharedRestoreShiftedElement(el);
                return;
            }
            sharedApplyTop(el, target);
        }
        if (sharedGetShiftBaseTop(el) < sharedBarHeight()) {
            sharedRecordShiftRowHeight(el);
        }
        return;
    }

    const top = sharedGetEffectiveTop(el, cs);
    if (top === null || top >= SHARED_TASKBAR.MAX_SHIFT_TOP) return;

    const targetTop = top + sharedBarHeight();
    const rectTop = Math.round(el.getBoundingClientRect().top);
    if (Math.abs(rectTop - targetTop) <= 3) {
        // Already at the intended visual offset (e.g. body padding) — track only.
        if (!el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) {
            el.setAttribute(SHARED_TASKBAR.SHIFTED_ATTR, el.style.getPropertyValue("top") || "");
            el.setAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR, String(top));
            sharedShiftedElements.add(el);
        }
        return;
    }

    el.setAttribute(SHARED_TASKBAR.SHIFTED_ATTR, el.style.getPropertyValue("top") || "");
    el.setAttribute(SHARED_TASKBAR.SHIFT_BASE_ATTR, String(top));
    sharedApplyTop(el, top + sharedBarHeight());
    sharedShiftedElements.add(el);
    if (top < sharedBarHeight()) sharedRecordShiftRowHeight(el);
}

function sharedShiftElementTree(root) {
    if (!(root instanceof HTMLElement) && !(root instanceof DocumentFragment)) return;

    const queue = [root];
    const seenRoots = new Set([root]);

    while (queue.length) {
        const node = queue.shift();
        if (!(node instanceof HTMLElement) && !(node instanceof DocumentFragment)) continue;

        if (node instanceof HTMLElement) sharedShiftFixedElement(node);

        if (!node.querySelectorAll) continue;
        node.querySelectorAll("*").forEach((el) => {
            sharedShiftFixedElement(el);
            if (el.shadowRoot && !seenRoots.has(el.shadowRoot)) {
                seenRoots.add(el.shadowRoot);
                queue.push(el.shadowRoot);
            }
        });
    }
}

function sharedShiftWithin(root) {
    sharedShiftElementTree(root);
    // LinkedIn-specific org-sticky-card discovery disabled — see sharedDiscoverOrgStickyCards.
    // if (document.getElementById(SHARED_TASKBAR.HOST_ID)) sharedDiscoverOrgStickyCards(root);
}

// Cheap filter — no getBoundingClientRect (avoids layout thrash on feed mutations).
function sharedIsHeaderMutationCandidate(el) {
    if (el.id === SHARED_TASKBAR.HOST_ID) return false;
    if (el.id === "searchform") return true;
    // LinkedIn-specific guard disabled — see sharedIsLinkedInProfileScrollToolbar.
    // if (sharedIsLinkedInProfileScrollToolbar(el)) return false;
    if (el.hasAttribute(SHARED_TASKBAR.SHIFTED_ATTR)) return true;
    if (el.closest("header, [role=\"banner\"], [role=\"toolbar\"]")) { // .org-sticky-top-card (LinkedIn) removed
        return true;
    }
    // if (el.hasAttribute("componentkey")) return true; // LinkedIn-specific attribute, disabled
    const style = el.getAttribute("style");
    if (style && /position\s*:\s*(fixed|sticky)/i.test(style)) return true;
    let depth = 0;
    let node = el;
    while (node && node !== document.body) {
        depth++;
        node = node.parentElement;
    }
    return depth <= SHARED_HEADER_MAX_DEPTH;
}

function sharedProcessMutationBatch(mutations) {
    const seen = new Set();
    for (const m of mutations) {
        if (m.type === "attributes") {
            const el = m.target;
            if (!(el instanceof HTMLElement) || seen.has(el)) continue;
            if (!sharedIsHeaderMutationCandidate(el)) continue;
            seen.add(el);
            sharedShiftFixedElement(el);
        } else {
            for (const node of m.addedNodes) {
                if (!(node instanceof HTMLElement)) continue;
                // LinkedIn-specific org-sticky-card discovery disabled — see sharedDiscoverOrgStickyCards.
                // sharedDiscoverOrgStickyCards(node);
                // Walk the whole added subtree (not just this node) — a fixed/sticky
                // header several levels down inside a freshly-mounted subtree arrives
                // as a descendant of a single addedNodes entry, never as its own
                // mutation record, so a node-only check would miss it entirely.
                sharedShiftElementTree(node);
            }
        }
    }
    sharedShiftPrimaryHeaders();
    sharedScheduleViewportConstrain();
    sharedEnsureBodyPadding();
}

// Capture the page's own padding-top exactly once, before we ever modify it —
// both the raw inline value (for exact restore) and the computed baseline (for
// target math). Idempotent: a no-op once the base attribute is present, so it's
// safe to call from either extension on every ensure/register.
function sharedCaptureBodyPadding() {
    if (document.body.hasAttribute(SHARED_TASKBAR.BODY_PADDING_BASE_ATTR)) return;
    document.body.setAttribute(
        SHARED_TASKBAR.PREV_BODY_PADDING_ATTR,
        document.body.style.getPropertyValue("padding-top") || ""
    );
    const base = parseFloat(getComputedStyle(document.body).paddingTop);
    document.body.setAttribute(
        SHARED_TASKBAR.BODY_PADDING_BASE_ATTR,
        String(isNaN(base) ? 0 : base)
    );
}

function sharedGetBodyPaddingBase() {
    const base = parseFloat(document.body.getAttribute(SHARED_TASKBAR.BODY_PADDING_BASE_ATTR));
    return isNaN(base) ? 0 : base;
}

function sharedRestoreBodyPadding() {
    const prev = document.body.getAttribute(SHARED_TASKBAR.PREV_BODY_PADDING_ATTR);
    document.body.style.removeProperty("padding-top");
    if (prev) document.body.style.setProperty("padding-top", prev);
    document.body.removeAttribute(SHARED_TASKBAR.PREV_BODY_PADDING_ATTR);
    document.body.removeAttribute(SHARED_TASKBAR.BODY_PADDING_BASE_ATTR);
}

function sharedEnsureBodyPadding() {
    if (!document.getElementById(SHARED_TASKBAR.HOST_ID)) return;
    if (sharedShouldSkipPageShifting()) return;

    const target = sharedGetBodyPaddingBase() + sharedBarHeight();
    const pad = parseFloat(getComputedStyle(document.body).paddingTop);
    if (isNaN(pad) || pad < target - 1) {
        document.body.style.setProperty(
            "padding-top", target + "px", "important"
        );
    }
}

function sharedPruneDisconnectedShiftedElements() {
    for (const el of sharedShiftedElements) {
        if (!el.isConnected) sharedShiftedElements.delete(el);
    }
}

function sharedApplyPageShift() {
    // LinkedIn-specific flag refresh disabled — see sharedRefreshPageFlags.
    // sharedRefreshPageFlags();

    if (sharedShouldSkipPageShifting()) return;

    sharedPruneDisconnectedShiftedElements();
    sharedShiftWithin(document.body);
    sharedShiftPrimaryHeaders();
    sharedScheduleViewportConstrain();
    if (document.getElementById(SHARED_TASKBAR.HOST_ID)) {
        document.body.style.setProperty(
            "padding-top", (sharedGetBodyPaddingBase() + sharedBarHeight()) + "px", "important"
        );
    }
}

function sharedRescanAfterOpen() {
    if (!document.getElementById(SHARED_TASKBAR.HOST_ID)) return;
    // sharedLinkedInJobSearchPage = null; // LinkedIn-specific, disabled
    sharedApplyPageShift();
    sharedEnsureBodyPadding();
}

function sharedStartShifting() {
    if (sharedShouldSkipPageShifting()) return;

    sharedApplyPageShift();

    if (sharedTaskbarObserver) return;
    sharedTaskbarObserver = new MutationObserver((mutations) => {
        // If another extension removed the host, stop and clean up after ourselves.
        if (!document.getElementById(SHARED_TASKBAR.HOST_ID)) {
            sharedStopShifting();
            return;
        }
        if (!sharedPendingMutations) sharedPendingMutations = [];
        sharedPendingMutations.push.apply(sharedPendingMutations, mutations);
        if (sharedMutationTimer) return;
        sharedMutationTimer = setTimeout(() => {
            sharedMutationTimer = null;
            const batch = sharedPendingMutations;
            sharedPendingMutations = null;
            if (!batch || !batch.length) return;
            // sharedRefreshPageFlags(); // LinkedIn-specific, disabled
            sharedProcessMutationBatch(batch);
        }, SHARED_MUTATION_DEBOUNCE_MS);
    });
    sharedTaskbarObserver.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["class", "style"],
    });

    if (!sharedResizeConstrainHandler) {
        sharedResizeConstrainHandler = () => sharedScheduleViewportConstrain();
        window.addEventListener("resize", sharedResizeConstrainHandler, { passive: true });
    }
}

function sharedStopShifting() {
    if (sharedMutationTimer) {
        clearTimeout(sharedMutationTimer);
        sharedMutationTimer = null;
    }
    if (sharedConstrainTimer) {
        clearTimeout(sharedConstrainTimer);
        sharedConstrainTimer = null;
    }
    if (sharedResizeConstrainHandler) {
        window.removeEventListener("resize", sharedResizeConstrainHandler);
        sharedResizeConstrainHandler = null;
    }
    sharedPendingMutations = null;
    // sharedLinkedInJobSearchPage = null; // LinkedIn-specific, disabled
    sharedShiftedElements.clear();
    // LinkedIn-specific org-sticky observer teardown disabled — see sharedOrgStickyObserver.
    // if (sharedOrgStickyObserver) {
    //     sharedOrgStickyObserver.disconnect();
    //     sharedOrgStickyObserver = null;
    // }
    if (sharedTaskbarObserver) {
        sharedTaskbarObserver.disconnect();
        sharedTaskbarObserver = null;
    }
    sharedStopSlotIntegrityObserver();
    sharedRestoreBodyPadding();
    document.querySelectorAll("[" + SHARED_TASKBAR.SHIFTED_ATTR + "]").forEach(sharedRestoreShiftedElement);
    document.querySelectorAll("[" + SHARED_TASKBAR.SHIFT_ROW_HEIGHT_ATTR + "]").forEach((el) => {
        el.removeAttribute(SHARED_TASKBAR.SHIFT_ROW_HEIGHT_ATTR);
    });
    document.querySelectorAll("[" + SHARED_TASKBAR.CONSTRAINED_ATTR + "]").forEach(sharedRestoreViewportConstraint);
    document.querySelectorAll("[" + SHARED_TASKBAR.FIT_TRIM_ATTR + "]").forEach(sharedRestoreViewportFitTrim);
    document.querySelectorAll("[" + SHARED_TASKBAR.QUARANTINED_ATTR + "]").forEach((el) => {
        el.removeAttribute(SHARED_TASKBAR.QUARANTINED_ATTR);
        el.removeAttribute(SHARED_TASKBAR.FIGHT_COUNT_ATTR);
        el.removeAttribute(SHARED_TASKBAR.FIGHT_TS_ATTR);
    });
}

// ---- host lifecycle --------------------------------------------------------
// Open shadow root for the bar interior. LinkedIn job search often clears
// foreign light-DOM children under <html>; shadow content survives that.
// Any cooperating extension can still reach slots via host.shadowRoot.
function sharedApplyHostChromeStyles(host) {
    const h = sharedBarHeight() + "px";
    const borderW = sharedZoomPx(2) + "px";
    host.style.setProperty("position", "fixed", "important");
    host.style.setProperty("top", "0", "important");
    host.style.setProperty("left", "0", "important");
    host.style.setProperty("width", "100%", "important");
    host.style.setProperty("height", h, "important");
    host.style.setProperty("max-height", h, "important");
    host.style.setProperty("box-sizing", "border-box", "important");
    host.style.setProperty("border-bottom", borderW + " solid #000", "important");
    host.style.setProperty("background-color", "#f0f0f0", "important");
    host.style.setProperty("z-index", "2147483647", "important");
    host.style.setProperty("overflow", "hidden", "important");
}

function sharedGetTaskbarRoot(host) {
    if (!host) return null;
    if (host.shadowRoot) return host.shadowRoot;
    host.replaceChildren();
    return host.attachShadow({ mode: "open" });
}

function sharedQueryTaskbar(host, selector) {
    const root = host && host.shadowRoot;
    return root ? root.querySelector(selector) : null;
}

function sharedHostStyleSheet() {
    const h = sharedBarHeight() + "px";
    const gap = sharedZoomPx(8) + "px";
    const slotGap = sharedZoomPx(6) + "px";
    const barPad = sharedZoomPx(12) + "px";
    const btnPadV = sharedZoomPx(6) + "px";
    const btnPadH = sharedZoomPx(12) + "px";
    const fontSize = sharedZoomPx(13) + "px";
    return (
        ":host{display:block!important;width:100%!important;height:100%!important;" +
        "max-height:" + h + "!important;box-sizing:border-box!important;" +
        "overflow:hidden!important;visibility:visible!important;opacity:1!important;}" +
        ".bar{display:flex!important;align-items:center!important;gap:" + gap + "!important;" +
        "height:100%!important;max-height:" + h + "!important;padding:0 " + barPad + "!important;" +
        "box-sizing:border-box!important;font-family:system-ui,sans-serif!important;" +
        "visibility:visible!important;overflow:hidden!important;" +
        "background-color:#f0f0f0!important;}" +
        "." + SHARED_TASKBAR.SLOTS_CLASS +
        "{display:flex!important;align-items:center!important;gap:" + gap + "!important;flex:1 1 auto!important;" +
        "min-width:0!important;visibility:visible!important;overflow:visible!important;}" +
        "." + SHARED_TASKBAR.SLOT_CLASS +
        "{display:flex!important;align-items:center!important;gap:" + slotGap + "!important;flex:0 0 auto!important;" +
        "visibility:visible!important;overflow:visible!important;}" +
        "button{display:inline-block!important;visibility:visible!important;" +
        "opacity:1!important;padding:" + btnPadV + " " + btnPadH + "!important;font-size:" + fontSize + "!important;cursor:pointer!important;}"
    );
}

function sharedBuildHostDom(host) {
    sharedApplyHostChromeStyles(host);
    const root = sharedGetTaskbarRoot(host);

    let style = root.querySelector("style[data-shared-taskbar]");
    if (!style) {
        style = document.createElement("style");
        style.setAttribute("data-shared-taskbar", "1");
        root.appendChild(style);
    }
    style.textContent = sharedHostStyleSheet();

    let bar = root.querySelector(".bar");
    if (!bar) {
        bar = document.createElement("div");
        bar.className = "bar";
        root.appendChild(bar);
    }

    let slots = bar.querySelector("." + SHARED_TASKBAR.SLOTS_CLASS);
    if (!slots) {
        slots = document.createElement("div");
        slots.className = SHARED_TASKBAR.SLOTS_CLASS;
        bar.appendChild(slots);
    }
}

function sharedEnsureHostStructure(host) {
    if (!host.shadowRoot) sharedGetTaskbarRoot(host);
    if (sharedQueryTaskbar(host, "." + SHARED_TASKBAR.SLOTS_CLASS)) return;
    sharedBuildHostDom(host);
}

function sharedGetSlot(extKey) {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return null;
    return sharedQueryTaskbar(host, sharedSlotSelector(extKey));
}

function sharedSlotHasButtons(extKey) {
    const slot = sharedGetSlot(extKey);
    return !!(slot && slot.querySelector("button"));
}

function sharedStopSlotIntegrityObserver() {
    if (sharedSlotIntegrityObserver) {
        sharedSlotIntegrityObserver.disconnect();
        sharedSlotIntegrityObserver = null;
    }
    sharedSlotIntegrityConfig = null;
}

function sharedWatchSlotIntegrity(extKey, buildFn, order) {
    sharedSlotIntegrityConfig = { extKey, buildFn, order };
    sharedStopSlotIntegrityObserver();

    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return;

    const root = sharedGetTaskbarRoot(host);
    sharedSlotIntegrityObserver = new MutationObserver(() => {
        if (sharedRegisteringSlot) return;

        const currentHost = document.getElementById(SHARED_TASKBAR.HOST_ID);
        if (!currentHost || !sharedSlotIntegrityConfig) {
            sharedStopSlotIntegrityObserver();
            return;
        }

        if (!sharedQueryTaskbar(currentHost, "." + SHARED_TASKBAR.SLOTS_CLASS)) {
            sharedBuildHostDom(currentHost);
        }

        if (!sharedTaskbarOpenAllowed) return;

        const cfg = sharedSlotIntegrityConfig;
        if (!sharedSlotHasButtons(cfg.extKey)) {
            registerTaskbar(cfg.extKey, cfg.buildFn, cfg.order);
        }
    });
    sharedSlotIntegrityObserver.observe(root, { childList: true, subtree: true });
}

// Remove a host that lost its slots (e.g. LinkedIn SPA clobbered the bar tree).
function sharedTeardownEmptyHost() {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return false;

    const slots = sharedQueryTaskbar(host, "." + SHARED_TASKBAR.SLOTS_CLASS);
    if (!slots || slots.children.length === 0) {
        host.remove();
        sharedStopShifting();
        sharedStopSlotIntegrityObserver();
        document.dispatchEvent(new CustomEvent(SHARED_TASKBAR.EVT_REMOVED, {
            detail: { hostId: SHARED_TASKBAR.HOST_ID },
        }));
        return true;
    }

    return false;
}

function sharedHostIsEmptyShell() {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return false;

    const slots = sharedQueryTaskbar(host, "." + SHARED_TASKBAR.SLOTS_CLASS);
    if (!slots || slots.children.length === 0) return true;

    return Array.from(slots.children).every(
        (slot) => !slot.querySelector("button")
    );
}

function sharedRemoveTaskbarHost() {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return false;

    host.remove();
    sharedStopShifting();
    sharedStopSlotIntegrityObserver();
    document.dispatchEvent(new CustomEvent(SHARED_TASKBAR.EVT_REMOVED, {
        detail: { hostId: SHARED_TASKBAR.HOST_ID },
    }));
    return true;
}

function sharedInstallSpaNavigationWatch() {
    if (sharedSpaNavInstalled) return;
    sharedSpaNavInstalled = true;

    let lastHref = location.href;
    const onNavigate = () => {
        if (location.href === lastHref) return;
        lastHref = location.href;
        // sharedLinkedInJobSearchPage = null; // LinkedIn-specific, disabled
        if (!document.getElementById(SHARED_TASKBAR.HOST_ID)) return;
        sharedStartShifting();
        document.dispatchEvent(new CustomEvent(SHARED_TASKBAR.EVT_PAGE_NAV, {
            detail: { href: location.href },
        }));
    };

    window.addEventListener("popstate", onNavigate);
    const wrapHistory = (original) => function (...args) {
        const ret = original.apply(this, args);
        onNavigate();
        return ret;
    };
    history.pushState = wrapHistory(history.pushState);
    history.replaceState = wrapHistory(history.replaceState);
}

// Idempotent and synchronous: content scripts from different extensions run as
// separate tasks on the same thread, so a synchronous check-then-create here
// guarantees the second extension reuses the first one's host (no race, no
// duplicate bars).
function sharedEnsureTaskbar() {
    sharedInstallSpaNavigationWatch();
    sharedCaptureBodyPadding();
    sharedWatchZoom();

    let existing = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (existing) {
        if (!existing.shadowRoot) {
            existing.replaceChildren();
            existing.attachShadow({ mode: "open" });
        }
        sharedEnsureHostStructure(existing);
        sharedApplyHostChromeStyles(existing);
    }
    if (existing) {
        sharedStartShifting();
        return existing;
    }

    const host = document.createElement("div");
    host.id = SHARED_TASKBAR.HOST_ID;
    sharedBuildHostDom(host);

    document.documentElement.prepend(host);
    sharedStartShifting();
    document.dispatchEvent(new CustomEvent(SHARED_TASKBAR.EVT_READY, {
        detail: { hostId: SHARED_TASKBAR.HOST_ID },
    }));
    return host;
}

function sharedRebuildSlotIfEmpty(extKey, buildFn, order) {
    if (!sharedTaskbarOpenAllowed) return false;

    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return false;

    if (!sharedQueryTaskbar(host, "." + SHARED_TASKBAR.SLOTS_CLASS)) {
        sharedBuildHostDom(host);
    }
    if (sharedSlotHasButtons(extKey)) return true;

    registerTaskbar(extKey, buildFn, order);
    return sharedSlotHasButtons(extKey);
}

function sharedSlotSelector(extKey) {
    return "." + SHARED_TASKBAR.SLOT_CLASS + '[data-ext="' + extKey + '"]';
}

// Full teardown — same end state as a page with the taskbar closed.
function sharedFullyCloseTaskbar(extKey) {
    sharedTaskbarOpenAllowed = false;
    sharedOpenEpoch += 1;
    unregisterTaskbar(extKey);
    sharedRemoveTaskbarHost();
}

// Open after a navigation-style reset. Page load restores via
// storage.get().then(show), which always runs later than script init; reopen
// used to run synchronously on click while the SPA was mid-update.
function sharedResetAndOpenTaskbar(extKey, buildFn, order) {
    sharedTaskbarOpenAllowed = true;
    sharedOpenEpoch += 1;
    const openEpoch = sharedOpenEpoch;

    unregisterTaskbar(extKey);
    sharedRemoveTaskbarHost();
    sharedStopShifting();

    const openNow = () => {
        if (!sharedTaskbarOpenAllowed || openEpoch !== sharedOpenEpoch) return;
        registerTaskbar(extKey, buildFn, order);
        queueMicrotask(() => {
            if (!sharedTaskbarOpenAllowed || openEpoch !== sharedOpenEpoch) return;
            sharedRescanAfterOpen();
        });
    };

    queueMicrotask(() => {
        requestAnimationFrame(() => {
            requestAnimationFrame(openNow);
        });
    });
}

// ---- public API ------------------------------------------------------------

// Ensure the shared taskbar exists and (re)build this extension's slot.
// extKey MUST be unique per extension. buildFn(slot, shadowRoot) populates the
// slot with the caller's own buttons. order controls left-to-right placement
// (lower = further left); slots with equal order fall back to insertion order.
// Returns the slot element.
function registerTaskbar(extKey, buildFn, order) {
    sharedTaskbarOpenAllowed = true;

    sharedRegisteringSlot = true;
    try {
        const host = sharedEnsureTaskbar();
        if (!host) return null;

        sharedEnsureHostStructure(host);
        const slots = sharedQueryTaskbar(host, "." + SHARED_TASKBAR.SLOTS_CLASS);
        if (!slots) return null;

        let slot = slots.querySelector(sharedSlotSelector(extKey));
        if (!slot) {
            slot = document.createElement("div");
            slot.className = SHARED_TASKBAR.SLOT_CLASS;
            slot.setAttribute("data-ext", extKey);
            slot.setAttribute("data-order", String(Number(order) || 0));
            sharedInsertSlotOrdered(slots, slot);
        } else {
            slot.setAttribute("data-order", String(Number(order) || 0));
            sharedInsertSlotOrdered(slots, slot); // re-place in case order changed
            slot.replaceChildren(); // idempotent re-register
        }

        if (typeof buildFn === "function") {
            try {
                buildFn(slot, host.shadowRoot || sharedGetTaskbarRoot(host));
            } catch (err) {
                console.warn("shared taskbar buildFn failed:", err);
            }
        }

        // LinkedIn-specific delayed re-shift disabled — see sharedIsLinkedInJobSearchPage.
        // if (sharedIsLinkedInJobSearchPage()) {
        //     setTimeout(() => {
        //         if (!sharedTaskbarOpenAllowed) return;
        //         sharedApplyPageShift();
        //     }, 120);
        // } else {
        sharedApplyPageShift();
        // }
        sharedWatchSlotIntegrity(extKey, buildFn, order);
        return slot;
    } finally {
        sharedRegisteringSlot = false;
    }
}

// Insert/move slot so siblings stay sorted by data-order. Insertion order is
// preserved among equal orders, so this is stable regardless of which extension
// loaded first.
function sharedInsertSlotOrdered(slots, slot) {
    const order = Number(slot.getAttribute("data-order")) || 0;
    const siblings = Array.from(slots.children).filter((c) => c !== slot);
    const before = siblings.find((c) => (Number(c.getAttribute("data-order")) || 0) > order);
    slots.insertBefore(slot, before || null);
}

// Remove this extension's slot. If it was the last one, tear down the whole
// taskbar and restore the page — safe to call from either extension.
function unregisterTaskbar(extKey) {
    const host = document.getElementById(SHARED_TASKBAR.HOST_ID);
    if (!host) return false;

    const slots = sharedQueryTaskbar(host, "." + SHARED_TASKBAR.SLOTS_CLASS);
    const slot = slots && slots.querySelector(sharedSlotSelector(extKey));
    if (slot) slot.remove();

    if (slots) {
        Array.from(slots.children).forEach((child) => {
            if (!child.querySelector("button")) child.remove();
        });
    }

    if (!slots || slots.children.length === 0) {
        host.remove();
        sharedStopShifting();
        sharedStopSlotIntegrityObserver();
        document.dispatchEvent(new CustomEvent(SHARED_TASKBAR.EVT_REMOVED, {
            detail: { hostId: SHARED_TASKBAR.HOST_ID },
        }));
    }
    return true;
}

// Whether this extension currently has a slot in the taskbar.
function isTaskbarRegistered(extKey) {
    return !!sharedGetSlot(extKey);
}
