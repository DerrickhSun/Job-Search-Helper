# Job-Applyer control panel (GitHub Pages)

A static page that talks to your own `server/extension_server.py`, running locally — the same
server the browser extension talks to (`extension/background.js`). Right now it's a single
"Ping local server" button that hits `/health`; the plan is to grow this into a config-editing
control panel later (see `server/README.md`'s Known Issues/TODO).

## Using it locally

Just open `pages/index.html` directly in a browser (or serve the folder with any static file
server). Enter your server URL (default `http://127.0.0.1:8743`) and the same token from
`COVER_LETTER_SERVER_TOKEN` in `.env` / the extension's options page, then **Save**, then
**Ping local server**.

## Deploying via GitHub Pages

This folder isn't one of GitHub's native "deploy from a branch" choices (only the branch root or
a `/docs` folder qualify) — enabling it requires either a GitHub Actions workflow that publishes
`pages/` as the Pages artifact, or renaming/duplicating this folder to `docs/`. Not set up yet.

**Before enabling it for real:** a page served from a public `https://*.github.io` origin is
subject to full CORS and Chrome's Private Network Access checks when it calls a private address
like `127.0.0.1`. `extension_server.py`'s `do_OPTIONS` already answers both (wide-open CORS +
`Access-Control-Allow-Private-Network: true`), so no server change should be needed — just make
sure whoever's `extension_server.py` you're pointing at is a version that includes that header.

The token field is never written into this repo — it's typed in by hand and kept in the
browser's `localStorage`, the same way the extension keeps it in `browser.storage.local`.
