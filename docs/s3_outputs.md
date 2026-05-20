# AWS S3 for outputs (between machines)

Use S3 as a remote copy of `output/` (CSVs, archives, cover letters, etc.) so you do not need to git-push results when switching devices.

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
      "Resource": "arn:aws:s3:::job-applyer-bucket-862361086686-us-east-2-an"
    },
    {
      "Sid": "ObjectRW",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::job-applyer-bucket-862361086686-us-east-2-an/*"
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

## 3. Upload from this repo

After installing deps (`pip install -r requirements.txt`):

```bash
# From repo root — uploads everything under output/ (recursive)
python scripts/upload_outputs_to_s3.py

# Dry run (print keys only)
python scripts/upload_outputs_to_s3.py --dry-run
```

## 4. Pull on another machine

Install [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), configure the same credentials (or a second IAM user with the same policy), then:

```bash
aws s3 sync s3://YOUR_BUCKET_NAME/devices/home-pc/output ./output
```

Keys mirror the upload script: `{S3_OUTPUT_PREFIX}{local_dir_name}/{relative path}` (default local dir name is `output`). Add `--dryrun` to the `aws s3 sync` command first to preview.

## Zero-Python alternative

If you only want sync and already use AWS CLI:

```bash
aws s3 sync ./output s3://YOUR_BUCKET_NAME/devices/home-pc/output
```

## Notes

- **SQLite** (`data/applications.db`) is not ideal to “sync” with two machines writing at once. Prefer uploading after a run, or move the tracker to Postgres later if you need live multi-device DB.
- **Costs**: S3 storage + requests are usually low for CSV/docx volumes; check [S3 pricing](https://aws.amazon.com/s3/pricing/) for your region.
- **Security**: prefer a dedicated IAM user with the policy above, not your root account keys.
