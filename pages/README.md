# Job-Applyer control panel (GitHub Pages)

A static page that talks to your own `server/extension_server.py`, running locally — the same
server the browser extension talks to (`extension/background.js`). It has a "Ping local server"
button (`GET /health`) and a config editor for `data/behavior.json` (skip-consulting,
student/unpaid job mode, Greenhouse toggles) and `data/search.json` (keywords/location) via the
server's `GET`/`POST /config` endpoints — server-side validation rejects bad values and saves
nothing on failure, so a bad edit here can't corrupt either file.

## Using it locally

Just open `pages/index.html` directly in a browser (or serve the folder with any static file
server). Enter your server URL (default `http://127.0.0.1:8743`) and the same token from
`COVER_LETTER_SERVER_TOKEN` in `.env` / the extension's options page, then **Save**. From there:
**Ping local server** just checks connectivity; **Load config** pulls the current
behavior/search settings into the form below, and **Save updates** pushes your edits back
(always sends the full form as one update — there's no per-field diffing).

## Deploying via GitHub Pages

This folder isn't one of GitHub's native "deploy from a branch" choices (only the branch root or
a `/docs` folder qualify), so it's published via `.github/workflows/deploy-pages.yml` instead,
which uploads just `pages/` as the Pages artifact on every push to `main` that touches it.

One-time setup in the GitHub UI: **Settings → Pages → Build and deployment → Source →
"GitHub Actions"** (not "Deploy from a branch"). After that, pushing to `main` runs the workflow
automatically; it can also be triggered manually from the Actions tab (`workflow_dispatch`).

**Before enabling it for real:** a page served from a public `https://*.github.io` origin is
subject to full CORS and Chrome's Private Network Access checks when it calls a private address
like `127.0.0.1`. `extension_server.py`'s `do_OPTIONS` already answers both (wide-open CORS +
`Access-Control-Allow-Private-Network: true`), so no server change should be needed — just make
sure whoever's `extension_server.py` you're pointing at is a version that includes that header.

The token field is never written into this repo — it's typed in by hand and kept in the
browser's `localStorage`, the same way the extension keeps it in `browser.storage.local`.
