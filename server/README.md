# LinkedIn Auto-Apply Bot

This is the `server/` folder of the [Job-Applyer](../README.md) monorepo — the bot itself, plus
the local HTTP bridge (`extension_server.py`) the browser extension talks to. Sibling folder:
[`../extension/`](../extension/). **All commands below assume your working directory is
`server/`** (i.e. `cd server` first).

Automatically finds and applies to LinkedIn Easy Apply jobs using your resume.

## Features
- Parses your resume (PDF or DOCX) into structured data
- Opens one Chrome session on LinkedIn job search, walks results **one listing at a time** (parse → log file → score → cover letter → Easy Apply on the same page)
- Scores each job against your resume using DSPy / an LLM (0–100%)
- Generates a tailored cover letter per job
- Fills and submits Easy Apply forms via Selenium (Chrome)
- Tracks all applications in SQLite + exports to CSV

---

## Setup

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```
Install [Google Chrome](https://www.google.com/chrome/) (Chromium is driven automatically via `webdriver-manager`).

### 2. Configure credentials
```bash
cp .env.example .env
# Edit .env with your LinkedIn login and Anthropic API key
```

Get your Anthropic API key at https://console.anthropic.com

### 3. Run

```bash
# Apply to remote Python engineer jobs, show at least 70% match
python main.py \
  --resume resume.pdf \
  --keywords "python engineer" \
  --location "Remote" \
  --min-score 0.7 \
  --max-jobs 30

# Run without a window (default is a visible Chrome window for debugging / 2FA)
python main.py --resume resume.pdf --keywords "product manager" --headless
```

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--resume` | required | Path to your resume PDF or DOCX |
| `--keywords` | `software engineer` | Job search query |
| `--location` | `Remote` | Location filter |
| `--min-score` | `0.6` | Minimum AI match score (0–1) |
| `--max-jobs` | `20` | Max search listings to walk through per run (each may score/apply) |
| `--listings-log` | `data/listings_log.jsonl` | Append one JSON line per parsed listing |
| `--headless` | off | Hide the Chrome window |
| `--step-delay` | `0.35` | Pause between UI steps (visible mode) |
| `--no-highlight` | off | Disable click outline highlight |
| `--debug-jobs-page` | off | After login, open job search and wait for Enter (no scraping / applies) |

---

## Output

- `data/listings_log.jsonl` — One JSON record per parsed job from search (audit trail)
- `data/applications.db` — SQLite database of all tracked jobs
- `output/applications.csv` — Spreadsheet export (under `output/`, gitignored — synced via S3 between machines)
- `output/screenshots/` — Error screenshots for failed applications
- `data/bot.log` — Full log

### Optional: sync outputs to AWS S3

To sync `output/` with a private S3 bucket between machines, set `S3_OUTPUT_BUCKET` (and AWS keys) in `.env` — see **[docs/s3_outputs.md](docs/s3_outputs.md)**. `python main.py` downloads from S3 at startup and uploads when the run ends. You can also upload manually:

```bash
python scripts/upload_outputs_to_s3.py
```

---

## Docker

Build and run with Chromium inside the image (headless by default; mount your `.env`, `data/`, `output/`, and resume):

```bash
docker build -t job-applyer .

docker run --rm -it \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/resume.pdf:/app/resume.pdf:ro" \
  job-applyer \
  --resume resume.pdf --keywords "software engineer" --location "United States"
```

On Windows PowerShell, use `${PWD}` instead of `$(pwd)` for volume paths.

LinkedIn/Greenhouse sign-in and 2FA are easiest with cookies created on the host (`data/selenium_*_cookies.json`) and mounted into the container. For a visible browser in Docker you need X11 or VNC; otherwise use `--no-headless` on the host.

---

## First Run Tips

1. The browser is visible by default so you can complete LinkedIn 2FA; use `--headless` only when you do not need the UI
2. Cookies are saved to `data/selenium_linkedin_cookies.json` — subsequent runs reuse them
3. Start with `--min-score 0.8` to apply only to strong matches
4. Review `output/applications.csv` after each run

---

## Extending to Other Job Boards

The modular design makes it straightforward to add new job boards:

1. Create `modules/job_searcher_indeed.py` (or similar)
2. Implement the same `.search()` interface returning the same job dict format
3. Create `modules/form_filler_indeed.py` with board-specific selectors
4. Update `main.py` to accept a `--board` flag

---

## Disclaimer

This tool automates actions on LinkedIn. Use it responsibly and in accordance
with LinkedIn's Terms of Service. Apply only to jobs you're genuinely interested in.

---

## Known Issues / TODO

- **`_peek_job_from_list_link` can mislabel `job["company"]` as job-title text** (`utils/job_searcher.py:1992-2018`).
  When a list card's title/company can't be read via the primary CSS selectors, it falls back to
  splitting the card's raw visible text by line and assuming line 0 = title, line 1+ = company —
  but never checks that the candidate it picks for `company` isn't just the title again. This has
  contaminated `output/consulting_companies.json`'s `normalized_company_names` with job-title-like
  strings (e.g. `"data scientist"`, `"full stack engineer"`) that have no corresponding entry in
  `slugs`, since the LinkedIn company slug is read separately (and correctly) from the job's detail
  pane via `selected_job_company_link`. Low-priority: worst case is an occasional false-positive
  "consulting" skip if a future job's real company name happens to substring-match one of the
  contaminated stored strings. Fix: add a `cand != title` (case-insensitive) guard to the fallback
  loop, and prune the already-contaminated title-like entries out of
  `output/consulting_companies.json`.

- **Future idea: let a webpage (e.g. a GitHub Pages control panel) trigger the bot programs
  themselves, not just edit config.** `extension_server.py` currently only exposes quick,
  synchronous operations (cover letters, form answers, the extension import flow). The
  long-running, browser-driving programs (`main.py`, `cleanup.py`) are a different shape of
  problem: they need their own OS process (Selenium/Chrome can't share a thread with the HTTP
  server) and can run for hours, so exposing them would mean a `POST /run-main`-style endpoint
  that launches a background `subprocess.Popen`, returns immediately, and separate `/status`/`/stop`
  endpoints (tracked in-memory, same shape as the `_PENDING` dict in
  `utils/extension_process_service.py`) for the page to poll/cancel. Quick no-browser utilities
  (`lookup_application.py`, `find_rule.py`, `sync.py`, `archive_applications.py`) would be much
  easier — same "absorb the logic into a callable, add an endpoint" pattern already used for
  `process_extension.py`. `agent.py` (the conversational Anthropic-API agent) is a third, different
  shape entirely (a multi-turn chat loop, not a fire-and-forget script) and would need its own
  chat-style endpoint design. Shelved for now — revisit once the extension/webapp split below has
  settled.

  Design notes from thinking this through further: the GIL isn't a real concern as long as
  `main.py` runs as a genuine `subprocess.Popen` (separate process, separate GIL) rather than an
  in-process function call — an in-process call would have other request threads stall during
  `main.py`'s CPU-bound stretches (DSPy scoring, regex filters), since Python only releases the
  GIL during actual I/O waits. The real risk with a subprocess is **making sure it (and everything
  under it) actually closes when it should**: `main.py` spawns chromedriver, which spawns Chrome,
  so killing just the top-level PID orphans the browser processes underneath it — same failure
  mode as the duplicate `extension_server.py` instances hit repeatedly this session, just one
  process layer deeper. Plan: track the whole process tree (the `psutil` library, not currently a
  dependency, would help here), prefer a graceful stop first (Windows: launch with
  `CREATE_NEW_PROCESS_GROUP`, then `send_signal(CTRL_BREAK_EVENT)` to trigger the same
  `except KeyboardInterrupt` / `finally` cleanup `main.py` already has at `main.py:1411-1429` —
  worth confirming `driver.quit()` is unconditionally covered there too), with a timeout that
  escalates to a full tree-kill if it doesn't exit. Also persist the launched PID to a small lock
  file (mirroring `sync.lock`'s pattern) so a server restart can rediscover/reconcile a still-running
  child instead of losing track of it, and so only one `main.py` run is ever allowed at a time
  (prevents two overlapping runs racing on the same DB/CSV/cookie files — again, the same species
  of bug as the port-8743 collisions). Status updates back to the caller (since a real user would
  only interact with the server, never the subprocess directly) can piggyback on existing state —
  capture stdout/stderr into an in-memory ring buffer (same shape as the `_PENDING` dict in
  `utils/extension_process_service.py`) plus `Popen.poll()` for liveness — with no changes needed
  to `main.py` itself; a written progress file (e.g. `data/run_status.json`) is a cleaner signal
  but needs actual instrumentation added to `main.py`'s loop, so start without it.

- **Future idea: host `extension_server.py` on an always-on cloud server**, reachable from
  brand-new devices that only have the extension/`pages/` control panel installed — never having
  run any of this code locally. The main obstacle: LinkedIn login. The existing `_login()` flow
  (`utils/job_searcher.py:3146+`) already assumes a human is watching a visible browser to solve a
  2FA/CAPTCHA checkpoint — and a fresh, cookie-less login from a cloud-datacenter IP (plus a
  Selenium fingerprint — `chrome_driver.py` only sets one anti-detection flag) is close to a
  worst-case trigger for exactly that checkpoint, which nothing server-side can solve unattended.
  Cookies have to be bootstrapped from a real, already-authenticated residential session instead of
  asking the cloud server to log in fresh. Best path found so far: the extension already has the
  right access for this that a plain webpage does not (LinkedIn blocks being framed at all, and its
  session cookie is `HttpOnly` — invisible to page JS either way) — add the `cookies` permission,
  have the extension open a real `linkedin.com/login` tab for the user to log into normally (they
  solve any checkpoint themselves, since it's genuinely them on LinkedIn's real page), then read
  the resulting cookies via `browser.cookies.getAll({domain: "linkedin.com"})`, map them into the
  same shape `chrome_driver.py` already reads/writes (`expirationDate` → `expiry`, rest matches),
  and POST them to a new endpoint that writes `data/selenium_linkedin_cookies.json` — no change
  needed to `load_cookies()`/the login flow itself. Caveat that doesn't go away: cookies established
  on the user's home IP but later used from the cloud server's IP can still occasionally get
  re-challenged by LinkedIn's risk engine; this reduces cold-start failures a lot, it doesn't
  eliminate checkpoints forever. Not started — still at the design stage.

  **Update:** the upload half of this (extension reads cookies via `browser.cookies.getAll`, maps
  `expirationDate` → `expiry`, POSTs to an authenticated endpoint) now exists — see
  `POST /profile/connect` in `extension_server.py`, triggered by the LinkedIn toolbar's "Connect to
  LinkedIn" button — but it was built for a different motivation (letting a device's browser
  session act as *whatever LinkedIn account is logged into it*, separate from main.py's own bot
  account, and eventually letting the `pages/` webpage trigger actions under that same identity via
  a server-minted `profile_id` — see `utils/extension_profiles.py`) and so writes to its own
  `data/extension_profiles/<profile_id>.json`, never `data/selenium_linkedin_cookies.json`.
  Reusing it for cloud-bootstrap would mean pointing a handler at `DEFAULT_COOKIE_PATH` instead —
  the login-tab flow described above is still unbuilt, and so is anything that actually *reads* a
  connected profile's cookies (today `/profile/connect` only stores them; `/profile/ping` only
  confirms a profile id is known).

- **Future idea: containerize the bot with Docker for real use, not just as a build check.**
  A `Dockerfile`/`.dockerignore` already exist and build correctly (context = `server/`), but
  Docker isn't actually part of the normal workflow yet — the bot is still run directly via
  `python main.py`. Come back to this later to actually adopt it (e.g. as the way this runs on a
  server/VM, or to standardize the dev environment), rather than leaving it as a dormant file.
