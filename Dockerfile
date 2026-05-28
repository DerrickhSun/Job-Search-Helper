# Job-Applyer: Selenium bot (LinkedIn / Greenhouse). Needs Chrome + writable data/ and output/.
#
# Build:
#   docker build -t job-applyer .
#
# Run (mount secrets, resume, and persistent state):
#   docker run --rm -it \
#     --env-file .env \
#     -v "%cd%\data:/app/data" \
#     -v "%cd%\output:/app/output" \
#     -v "%cd%\resume.pdf:/app/resume.pdf:ro" \
#     job-applyer \
#     --resume resume.pdf --keywords "software engineer" --location "United States"
#
# On Linux/macOS, replace "%cd%" with "$(pwd)".
# First-time LinkedIn/Greenhouse login often needs a visible browser; default here is headless.
# For interactive login, use --no-headless and an X11/VNC setup, or sign in on the host and reuse cookies in data/.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    CHROME_DOCKER=1 \
    JOB_APPLIER_HEADLESS=1

WORKDIR /app

# Chromium matches chromedriver from Debian; avoids webdriver-manager downloads at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        ca-certificates \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .
COPY utils ./utils
COPY data/company_blacklist.json ./data/
COPY data/form_fill_rules ./data/form_fill_rules
COPY output/.gitkeep ./output/

RUN mkdir -p data output

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
