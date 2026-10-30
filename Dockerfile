FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# GOOGLE_APPLICATION_CREDENTIALS is intentionally NOT set: on Cloud Run the
# job's attached service account is used via Application Default Credentials.
# .env files and key files are excluded by .dockerignore.

WORKDIR /app

# System deps for psycopg2 (AlloyDB) and PyMuPDF (PDF rendering)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev libmupdf-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY IP_pillar ./IP_pillar

# DRUG_NAME set   -> runs only that drug (or comma-separated list)
# DRUG_NAME unset -> every GLP-1 drug, sharded across Cloud Run tasks
ENTRYPOINT ["python", "-m", "IP_pillar.run_ip_dimension"]
