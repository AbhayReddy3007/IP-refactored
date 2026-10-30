# Running the IP Dimension 1 pipeline on a Linux VM (AlloyDB + service account)

This guide sets the pipeline up on a Linux VM (Debian/Ubuntu) that can reach your AlloyDB instance, and runs it for a single drug. Every BigQuery and GCS call is authenticated with your **service account JSON key**.

---

## 1. What the pipeline talks to

| System | Used for | Authenticated by |
|---|---|---|
| **BigQuery** | patent_master, clinical_efficacy, vw_drug_details_full, forecasted_loe (read); `glp_patent_loe`, `glp_master_loe` (write) | Service account JSON (`GOOGLE_APPLICATION_CREDENTIALS`) |
| **GCS** | patent PDFs (read); caches, JSON snapshots, reports (write) | Service account JSON |
| **AlloyDB** | chunk embeddings (pgvector), via private IP | `ALLOYDB_USER` / `ALLOYDB_PASSWORD` |
| **Gemini API** | text extraction, embeddings, blocking analysis | `GOOGLE_API_KEY` |
| **Public web** | FDA / EMA / PubMed / Europe PMC lookups | none (outbound HTTPS) |

---

## 2. Service account permissions

Grant the service account in your JSON key these roles:

| Where | Role | Why |
|---|---|---|
| Project `cognito-prod-394707` | **BigQuery Job User** (`roles/bigquery.jobUser`) | run queries / load jobs (billed here) |
| Dataset `cognito-prod-394707.cognito_prod_datamart` | **BigQuery Data Editor** (`roles/bigquery.dataEditor`) | read `vw_drug_details_full` + `forecasted_loe`, create/replace rows in `glp_patent_loe` / `glp_master_loe` |
| Datasets `cognito-dev-380506.data_mart` and `cognito-dev-380506.stage` | **BigQuery Data Viewer** (`roles/bigquery.dataViewer`) | `clinical_efficacy_glp1`, `vw_drug_details_full`, `patent_master` |
| Bucket `GCS_BUCKET` (e.g. `cognito-gcs`) | **Storage Object Admin** (`roles/storage.objectAdmin`) | read PDFs; write/delete cache files, JSON, reports |

AlloyDB uses password authentication over the private IP, so it needs no IAM role. Gemini uses the API key, not the service account.

---

## 3. One-time VM setup

### 3.1 System packages

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git gcc libpq-dev postgresql-client
python3 --version        # must be 3.10 or newer
```

### 3.2 Put the service account key on the VM (outside the code folder)

From your laptop:

```bash
gcloud compute scp ./ip-pipeline-sa.json <vm-name>:~/ --zone <zone>
# or: scp ./ip-pipeline-sa.json <user>@<vm-ip>:~/
```

On the VM:

```bash
mkdir -p ~/.secrets && chmod 700 ~/.secrets
mv ~/ip-pipeline-sa.json ~/.secrets/
chmod 600 ~/.secrets/ip-pipeline-sa.json
realpath ~/.secrets/ip-pipeline-sa.json     # use this absolute path in .env
```

### 3.3 Get the code

```bash
cd ~
git clone <your-repo-url> ip-refactored      # or copy the "IP refactored" folder with scp -r
cd ip-refactored
```

### 3.4 Virtual environment + dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .            # installs requirements.txt + the `run-ip-dimension` command
```

(`pip install -r requirements.txt` also works if you only want the dependencies.)

### 3.5 Create `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

At minimum, set:

```dotenv
GOOGLE_API_KEY=<gemini api key>
GOOGLE_APPLICATION_CREDENTIALS=/home/<user>/.secrets/ip-pipeline-sa.json
PROJECT_ID=cognito-prod-394707
GCS_BUCKET=cognito-gcs
ALLOYDB_HOST=10.x.x.x
ALLOYDB_PASSWORD=<password>
```

`config.py` loads `.env` automatically. Anything you `export` in the shell takes priority over `.env`. See [section 8](#8-env-reference) for every variable.

---

## 4. Pre-flight checks

Run these from the project root (the folder that contains `IP_pillar/`), with the venv active.

**AlloyDB is reachable and pgvector is available:**

```bash
set -a; source .env; set +a
PGPASSWORD="$ALLOYDB_PASSWORD" psql -h "$ALLOYDB_HOST" -U "${ALLOYDB_USER:-postgres}" -d "${ALLOYDB_DB:-postgres}" \
  -c "SELECT name, installed_version FROM pg_available_extensions WHERE name = 'vector';"
```

If this hangs, the VM can't reach the private IP. Check the VM is in the AlloyDB VPC and that a firewall rule allows TCP 5432. The pipeline creates the `vector` extension and the `embeddings` table on first use.

**The service account works for BigQuery and GCS:**

```bash
python - <<'EOF'
from IP_pillar import config, gcp_utils
print("Service account:", gcp_utils.get_credentials().service_account_email)
print("BigQuery:", gcp_utils.run_query("SELECT 1 AS ok").to_dict("records"))
print("GCS:", len(gcp_utils.list_blobs_with_prefix(config.GCS_PATENTS_PREFIX + "/")), "objects under", config.GCS_PATENTS_PREFIX)
EOF
```

---

## 5. Run for a single drug

```bash
cd ~/ip-refactored && source .venv/bin/activate

python -m IP_pillar.run_ip_dimension --drug "Semaglutide"
# same thing, via the installed command:
run-ip-dimension --drug "Semaglutide"
```

Options:

| Command | Effect |
|---|---|
| `--drug "Semaglutide"` | one drug |
| `--drug "Semaglutide" --reindex` | ignore caches: re-extract, re-embed and re-analyse every patent |
| `DRUG_NAME="Semaglutide, Tirzepatide" python -m IP_pillar.run_ip_dimension` | several drugs, one after another |
| `python -m IP_pillar.run_ip_dimension` (no `DRUG_NAME`) | every GLP-1 drug with patents in GCS |

The exit code is `0` on success and `1` if the drug (or any drug in the list) failed.

**Long runs:** use `tmux`, or run it in the background with a log file, so an SSH disconnect doesn't kill it:

```bash
mkdir -p logs
nohup python -m IP_pillar.run_ip_dimension --drug "Semaglutide" > "logs/semaglutide_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
tail -f logs/semaglutide_*.log
```

---

## 6. Outputs

| Output | Location |
|---|---|
| Chunk embeddings | AlloyDB table `embeddings`, collection `patents_<drug>` |
| **Patents-only table** | BigQuery `cognito-prod-394707.cognito_prod_datamart.glp_patent_loe` |
| **Patents + forecasted table** | BigQuery `cognito-prod-394707.cognito_prod_datamart.glp_master_loe` |
| **JSON of each table** | `gs://<GCS_BUCKET>/pipeline_cache/loe_json_exports/glp_patent_loe/<drug>_<YYYYMMDD_HHMMSS>.json`<br>`gs://<GCS_BUCKET>/pipeline_cache/loe_json_exports/glp_master_loe/<drug>_<YYYYMMDD_HHMMSS>.json` |
| Resume / analysis caches | `gs://<GCS_BUCKET>/pipeline_cache/indexer_progress/`, `.../analysis_cache/<drug>/` |

Both BigQuery tables have the 26 `FINAL_COLUMNS` from `config.py` first, in order (with BigQuery-safe names, e.g. `PTE (months)` → `PTE_months`), followed by extra blocking-analysis columns. Each run **replaces that drug's rows**, so re-running a drug never duplicates it. Each JSON file is a timestamped snapshot holding exactly the rows loaded for that drug in that run.

Check the results:

```sql
SELECT Type, COUNT(*) AS n, ANY_VALUE(Avg_Years_to_Entry_US_EP) AS avg_yte, ANY_VALUE(IP_Dimension_1_Score) AS score
FROM `cognito-prod-394707.cognito_prod_datamart.glp_master_loe`
WHERE LOWER(Drug_Name) = 'semaglutide'
GROUP BY Type;
```

```bash
gsutil ls gs://<GCS_BUCKET>/pipeline_cache/loe_json_exports/glp_master_loe/
```

---

## 7. Project structure

```
ip-refactored/
├── .env.example              template for .env (copy, fill in, never commit .env)
├── .dockerignore / .gitignore
├── Dockerfile                Cloud Run Jobs image
├── pyproject.toml            package metadata + `run-ip-dimension` command
├── requirements.txt
├── docs/RUN_ON_LINUX_VM.md   this guide
└── IP_pillar/
    ├── config.py             every setting / env var (loads .env)
    ├── utils.py              shared pure helpers (normalisation, jurisdictions, dates, BQ naming)
    ├── gcp_utils.py          all GCP access: credentials, BigQuery/GCS clients, query/load/delete, GCS read/write
    ├── run_ip_dimension.py   entry point: picks the drugs, shards on Cloud Run
    ├── drug_list.py          drug_list(): the GLP-1 drug names (BigQuery) + resolve_drug_name()
    ├── delete_rows.py        delete_drug_rows(): clear a drug's rows from the LOE tables before re-writing them
    ├── patent_filter.py      patent_filter(): a drug's approved patent PDFs in GCS
    ├── chunking/             indexer (Gemini text + embeddings), dates, alloydb_client
    ├── blocking_analysis/    blocking_analysis + step1..step5
    ├── primary_market_entry_horizon/
    │   ├── primary_market_entry_horizon.py   orchestrates the whole per-drug pipeline
    │   ├── score_calculation/
    │   │   ├── patent_scores.py       per-patent years-to-entry + scores (calculate_scores)
    │   │   ├── phase_fetcher.py       clinical phase (clinical_efficacy + vw_drug_details_full)
    │   │   ├── approval_date_fetcher.py   FDA / EMA / Gemini / news approval dates
    │   │   ├── scoring.py             forecasted expiry, Years to Entry, LOE override, fallback, IP Dimension 1 Score
    │   │   └── merge_forecasted_loe.py    glp_patent_loe rows; merge with forecasted_loe → glp_master_loe rows
    │   ├── bq_utils.py                every BigQuery read; LOE table write (replace drug rows) + JSON snapshot
    │   └── generate_report_and_rationale/
    │       ├── generate_rationale.py  per-drug LOE metrics + the `Rationale` column of glp_master_loe
    │       └── generate_report.py     2-page PDF per drug from glp_master_loe → GCS (`python -m IP_pillar.primary_market_entry_horizon.generate_report_and_rationale.generate_report`)
```

---

## 8. `.env` reference

**Required on a VM**

| Variable | Example | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | `AIza...` | Gemini API key |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/home/me/.secrets/ip-pipeline-sa.json` | absolute path; **leave unset on Cloud Run** |
| `PROJECT_ID` | `cognito-prod-394707` | BigQuery billing project |
| `GCS_BUCKET` | `cognito-gcs` | PDFs + all GCS output |
| `ALLOYDB_HOST` | `10.x.x.x` | AlloyDB private IP |
| `ALLOYDB_PASSWORD` | | |

**Optional (defaults shown)**

| Variable | Default |
|---|---|
| `ALLOYDB_USER` / `ALLOYDB_DB` / `ALLOYDB_PORT` | `postgres` / `postgres` / `5432` |
| `PATENT_MASTER_TABLE` | `cognito-dev-380506.stage.patent_master` |
| `CLINICAL_EFFICACY_TABLE` | `cognito-dev-380506.data_mart.clinical_efficacy_glp1` |
| `DRUG_DETAILS_TABLE` | `cognito-dev-380506.data_mart.vw_drug_details_full` |
| `DRUG_LIST_VIEW` | `cognito-prod-394707.cognito_prod_datamart.vw_drug_details_full` |
| `DIM_SCORES_TABLE` | `cognito-prod-394707.cognito_prod_datamart.dimension_scores` |
| `FORECASTED_LOE_TABLE` | `cognito-prod-394707.cognito_prod_datamart.forecasted_loe` |
| `BQ_PATENT_LOE_TABLE` / `BQ_MASTER_LOE_TABLE` | `cognito-prod-394707.cognito_prod_datamart.glp_patent_loe` / `...glp_master_loe` |
| `GCS_PATENTS_PREFIX` | `Cognito_new/Master_patent_list` |
| `GCS_CACHE_PREFIX` | `pipeline_cache` |
| `GCS_JSON_EXPORT` | `loe_json_exports` |
| `NO_MARKETED_SKIP` | `false` (forecast rows of Marketed / Pre-reg drugs are dropped) |
| `DRUG_NAME` | empty (= all GLP-1 drugs) |
| `GEMINI_TEXT_MODEL` / `GEMINI_EMBEDDING_MODEL` | `gemini-2.5-flash` / `gemini-embedding-001` |
| `FORMULATION_EXCEL_PATH`, `PUBMED_EMAIL`, `NCBI_API_KEY` | empty |
| `INDEXER_CONCURRENCY`, `ANALYSIS_CONCURRENCY` | `10`, `5` |

The full list, with comments, is in `IP_pillar/config.py`.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `FileNotFoundError: GOOGLE_APPLICATION_CREDENTIALS=... does not exist` | Use the absolute path from `realpath` in `.env`. |
| `403 Access Denied` / `User does not have bigquery.jobs.create` | Grant the roles in [section 2](#2-service-account-permissions) to the email printed by the pre-flight check. |
| `403` on `cognito-dev-380506...` tables | Data Viewer is missing on that project's datasets. |
| AlloyDB `connection timed out` | The VM isn't in the AlloyDB VPC, or the firewall blocks 5432. |
| `could not open extension control file ... vector` | Enable the `vector` extension on the AlloyDB instance (or connect as a user allowed to `CREATE EXTENSION`). |
| `No approved PDFs found` | The GCS folder name or the patent_master approvals don't match the drug. Run `python -m IP_pillar.patent_filter "<drug>"` to see what is found. |
| Gemini `429 RESOURCE_EXHAUSTED` | Lower `ANALYSIS_CONCURRENCY` / `INDEXER_CONCURRENCY` in `.env`. |
| A BigQuery write fails but the run continues | The JSON snapshot is still written; fix the cause and re-run the drug (cached steps are skipped). |
