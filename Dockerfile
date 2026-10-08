FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# No GOOGLE_APPLICATION_CREDENTIALS / BQ_SERVICE_ACCOUNT / GCS_SERVICE_ACCOUNT /
# GOOGLE_SERVICE_KEY are set here on purpose. Leaving them unset means Google's
# auth libraries never look for a key file at all — they fall straight through
# to Application Default Credentials, which on Cloud Run resolves automatically
# to whichever service account is selected in the Job's Security tab (no file,
# no env var, no IAM console step needed if that account already has the
# required roles). Do NOT re-add these env vars unless you are intentionally
# shipping a service-account.json file in the image (not recommended) or
# mounting one from Secret Manager at that exact path.

WORKDIR /app

# System deps for psycopg2 (AlloyDB)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything including service-account.json and the IP_refactored/ package
COPY . .

# Cloud Run Jobs entry point — runs dimension_1.main():
#   - DRUG_NAME env var set  -> runs only that drug (or comma-separated list)
#   - DRUG_NAME unset        -> discovers + shards every drug under GCS_PATENTS_PREFIX
ENTRYPOINT ["python", "-m", "IP_refactored.dimension_1"]
