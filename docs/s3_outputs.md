# AWS S3 for outputs (between machines)

Use S3 as a remote copy of `output/` (CSVs, archives, cover letters, **form fill rules**, `consulting_companies.json`, etc.) so you do not need to git-push results when switching devices. The `output/` tree is **gitignored** (only `output/.gitkeep` is tracked) to avoid merge conflicts; use S3 or a fresh run to populate it on each machine.

**Form fill rules** live at `output/form_fill_rules/` (including `auto_rules.json` from the browser extension). Bundled defaults ship in `defaults/form_fill_rules/` for first-time seeding only — edit the runtime copy under `output/`, not git.

### Pruning old cover letters (faster sync)

Large `output/coverletters/` folders slow S3 upload/download. By default, `main.py` and `archive_applications.py` delete local and S3 cover letter `.docx` files **older than 7 days** (by file modification time locally; S3 `LastModified` remotely) before each sync, then delete the **oldest** files if more than **100** remain.

In `.env`:

```bash
COVER_LETTER_MAX_AGE_DAYS=7   # default; set 0 to disable age pruning
COVER_LETTER_MAX_COUNT=100    # default; set 0 to disable count cap
```

After S3 download, cover letter local mtimes are set from S3 `LastModified` so age/count pruning stays correct across machines (otherwise every download would look "new").

Manual prune (dry run first):

```bash
python scripts/prune_old_cover_letters.py --dry-run
python scripts/prune_old_cover_letters.py
```

## 1. Create a bucket

1. Sign in to [AWS Console](https://console.aws.amazon.com/) → **S3** → **Create bucket**.
2. Choose a **globally unique** name (e.g. `yourname-job-applyer-outputs`).
3. Pick a **Region** (e.g. `us-west-2`). Use the same region in env vars below.
4. **Block Public Access**: keep defaults (bucket private).
5. Create the bucket.

Optional: enable **Versioning** if you want accidental overwrites recoverable (extra storage cost).

## 2. IAM user (access keys for local scripts)

1. **IAM** → **Users** → **Create user** (e.g. `job-applyer-s3-sync`).
2. **Attach policies directly** → **Create policy** (JSON), minimal example:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME"
    },
    {
      "Sid": "ObjectRW",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME/*"
    }
  ]
}
```

Replace `YOUR_BUCKET_NAME`. Attach this policy to the user.

3. **Security credentials** → **Create access key** → **Command line** (or “Local code”). Save **Access key ID** and **Secret access key** once.

Put them in `.env` (never commit `.env`):

```bash
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-west-2
S3_OUTPUT_BUCKET=yourname-job-applyer-outputs
# Optional: separate laptop folders in the same bucket
S3_OUTPUT_PREFIX=devices/home-pc/
```

## 3. Automatic sync in `main.py`

When `S3_OUTPUT_BUCKET` is set in `.env` (with AWS credentials), every `python main.py` run:

1. **Downloads** from S3 into `output/` **before** reading archives/CSVs (right after `load_dotenv()`, before legacy path migration).
2. **Uploads** the full `output/` tree **after** the run finishes (including `--export-csv`, `--filter`, and Greenhouse flows), even if the run errors or you press Ctrl+C.

If `S3_OUTPUT_BUCKET` is unset, sync is skipped (no error).

The same download/upload pattern applies to ``python archive_applications.py`` (archives CSVs under ``output/``).

**Consulting company memory** lives at ``output/consulting_companies.json`` (LinkedIn slug/name cache). On first run after upgrading, if only ``data/consulting_companies.json`` exists locally it is moved into ``output/``. That file is included in the full ``output/`` S3 sync like other outputs.

## 4. Manual upload script

After installing deps (`pip install -r requirements.txt`):

```bash
# From repo root — uploads everything under output/ (recursive)
python scripts/upload_outputs_to_s3.py

# Dry run (print keys only)
python scripts/upload_outputs_to_s3.py --dry-run
```

## 5. Pull on another machine

On the other machine, run `python main.py` with the same `.env` S3 settings — it downloads at startup automatically.

Alternatively, install [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and run `aws configure` (the CLI does **not** read `.env`). Then:

```bash
aws s3 sync s3://YOUR_BUCKET_NAME/devices/home-pc/output ./output
```

Keys mirror the upload logic: `{S3_OUTPUT_PREFIX}{local_dir_name}/{relative path}` (default local dir name is `output`). Add `--dryrun` to the `aws s3 sync` command first to preview.

## Zero-Python alternative

If you only want sync and already use AWS CLI:

```bash
aws s3 sync ./output s3://YOUR_BUCKET_NAME/devices/home-pc/output
```

## Notes

- **SQLite** (`data/applications.db`) is not ideal to “sync” with two machines writing at once. Prefer uploading after a run, or move the tracker to Postgres later if you need live multi-device DB.
- **Costs**: S3 storage + requests are usually low for CSV/docx volumes; check [S3 pricing](https://aws.amazon.com/s3/pricing/) for your region.
- **Security**: prefer a dedicated IAM user with the policy above, not your root account keys.
