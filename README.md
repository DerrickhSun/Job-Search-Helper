# LinkedIn Auto-Apply Bot

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
