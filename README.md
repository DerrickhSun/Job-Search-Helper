# Job-Applyer (monorepo)

This repo contains three related, independently deployable pieces:

- **[`server/`](server/README.md)** — the LinkedIn/Greenhouse Easy-Apply bot: a Python Selenium
  bot that searches, scores, and applies to jobs, plus a small local HTTP bridge
  (`extension_server.py`) that the browser extension talks to.
- **[`extension/`](extension/)** — a Manifest V3 browser extension (content scripts + popup/options
  UI) that helps fill in Easy Apply / Workday / Ashby / Greenhouse forms and forwards
  data to the local bot server.
- **[`pages/`](pages/README.md)** — a static GitHub Pages control panel that talks to the same
  local `extension_server.py`, for managing things from a browser tab without the extension.

See each folder's own README/docs for setup and usage instructions.
