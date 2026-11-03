# Running the IP pipeline on a Linux VM

This guide covers running the pipeline on a Linux VM (Debian / Ubuntu) that is already set up and can reach your
AlloyDB instance: running it for one drug, several drugs or every GLP-1 drug, and reading the
outputs. It runs all five IP dimensions:

| Dimension | What it produces |
|---|---|
| `primary_market_entry_horizon` | years to generic entry per patent; `glp_patent_loe`, `glp_master_loe`, forecast tables |
| `patent_strength` | legal-robustness scores per patent and country |
| `patent_thicket_and_circumvention` | thicket score and design-around analysis |
| `global_launch_sequencing_and_geographic_arbitrage` | launch order, expiry per country, arbitrage score |
| `litigations` | patent litigation and court cases |

Contents: [1 How to run](#1-how-to-run) | [2 Outputs](#2-outputs) | [3 Project structure](#3-project-structure) |
[4 `.env` reference](#4-env-reference)

---

## 1. How to run

```bash
cd ~/ip-refactored && source .venv/bin/activate

python -m ip_pillar.run_ip_dimension --drug "Semaglutide"
# same thing, through the installed command:
run-ip-dimension --drug "Semaglutide"
```

### 1.1 Options

| What | How | Default |
|---|---|---|
| One drug | `--drug "Semaglutide"` | |
| Several drugs, one after another | `DRUG_NAME="Semaglutide, Tirzepatide"` | |
| Every GLP-1 drug with patents in GCS | no `--drug`, `DRUG_NAME` unset | |
| Ignore caches and redo everything | `--reindex` (or `REINDEX=true`) | off |
| Which dimension | `IP_DIMENSION=<name>` | `all` |
| Skip the forecast inside the horizon run | `RUN_FORECAST=false` | on |
| Keep forecast rows for Marketed / Pre-registration drugs | `NO_MARKETED_SKIP=true` | off |
| Lower Gemini / AlloyDB load | `ANALYSIS_CONCURRENCY`, `INDEXER_CONCURRENCY` | 5, 10 |

`--drug` overrides `DRUG_NAME`, which overrides discovery. `IP_DIMENSION` is one of `all`,
`primary_market_entry_horizon`, `patent_strength`, `patent_thicket_and_circumvention`,
`global_launch_sequencing_and_geographic_arbitrage`, `litigations`.

The functions themselves take only the drug name; `--reindex` works by setting `config.REINDEX`.

### 1.2 Examples

```bash
# everything for one drug
python -m ip_pillar.run_ip_dimension --drug "Semaglutide"

# only the horizon (must come first for a new drug)
IP_DIMENSION=primary_market_entry_horizon python -m ip_pillar.run_ip_dimension --drug "Semaglutide"

# re-run one dependent dimension on the existing glp_master_loe
IP_DIMENSION=patent_strength python -m ip_pillar.run_ip_dimension --drug "Semaglutide"

# two drugs, redoing everything
DRUG_NAME="Semaglutide, Tirzepatide" python -m ip_pillar.run_ip_dimension --reindex

# every GLP-1 drug
python -m ip_pillar.run_ip_dimension
```

### 1.3 Order and failures

With `IP_DIMENSION=all`, each drug runs in this order: `primary_market_entry_horizon`, `patent_strength`,
`patent_thicket_and_circumvention`, `global_launch_sequencing_and_geographic_arbitrage`, `litigations`. If the
horizon run fails for a drug, the rest are skipped for that drug. A failure in one later dimension does not stop the
others, and one drug failing does not stop the next drug.

The exit code is `0` if every drug succeeded and `1` if any drug failed.

### 1.4 Long runs

Use `tmux`, or run in the background with a log file, so an SSH disconnect does not kill the run:

```bash
mkdir -p logs
nohup python -m ip_pillar.run_ip_dimension --drug "Semaglutide" > "logs/semaglutide_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
tail -f logs/semaglutide_*.log
```

### 1.5 Cloud Run Jobs (optional)

Build with the `Dockerfile` (`docker build -t ip-pillar .`). Leave `GOOGLE_APPLICATION_CREDENTIALS` unset so the
job's service account is used. Set `DRUG_NAME` on the job to choose drugs, or leave it unset to run every drug;
`CLOUD_RUN_TASK_INDEX` and `CLOUD_RUN_TASK_COUNT` then split the drug list across tasks.

---

## 2. Outputs

Each run replaces **only that drug's rows**, so re-running a drug never duplicates it.

### 2.1 BigQuery (dataset `cognito-prod-394707.cognito_prod_datamart`)

| Dimension | Tables |
|---|---|
| `primary_market_entry_horizon` | `glp_patent_loe` (patents only), `glp_master_loe` (patents + forecast, re-scored); forecast: `forecast_s3`, `filing_pattern_table`, `company_analysis_table`, `forecasted_loe` |
| `patent_strength` | `patent_strength_analysis_table`, `patent_strength_country_score_table` |
| `patent_thicket_and_circumvention` | `Circumvention_Table`, `Patent_Thicket_Score_Table` |
| `global_launch_sequencing_and_geographic_arbitrage` | `shortlisted_secondary_patents_table`, `arbitrage_summary_table` |
| `litigations` | `litigation_analysis_table` |
| all | `gemini_stats` (one row per model per run, written even when a run fails) |

Table names are the defaults in `config.py`; each can be overridden with its `BQ_*_TABLE` variable.

### 2.2 GCS

| What | Location |
|---|---|
| PDF reports | `gs://<REPORT_GCS_BUCKET>/Cognito_new/reports/<drug>/IP/<Report>.pdf`, plus `archive/<Report>+<timestamp>.pdf` |
| Run payloads (JSON of each dimension's result) | `gs://<GCS_BUCKET>/Cognito_new/pipeline_cache/<drug>/IP/<Dimension>/output_payload.json`, plus `archived/output_payload_<timestamp>.json` |
| LOE table snapshots | `gs://<GCS_BUCKET>/pipeline_cache/loe_json_exports/<table>/<drug>_<YYYYMMDD_HHMMSS>.json` |
| Resume and analysis caches | `gs://<GCS_BUCKET>/pipeline_cache/indexer_progress/`, `.../analysis_cache/<drug>/`, `.../ipd2_checkpoints/`, `.../ipd3_checkpoints/` |
| AlloyDB | table `embeddings`, collection `patents_<drug>` |

Report names: Primary Market Entry Horizon, Blocking Analysis, Forecast Analysis, Patent Strength and Invalidity
Opportunity, Patent Thicket and Circumvention Feasibility, Global Launch Sequencing & Arbitrage, PTE Analysis,
Litigation Analysis. `REPORT_GCS_PATH` and `PAYLOAD_PATH` set the folders.

A failed report or forecast is recorded in the run result and does not fail the run.

### 2.3 Check the results

```sql
SELECT Type, COUNT(*) AS n, ANY_VALUE(Avg_Years_to_Entry_US_EP) AS avg_yte, ANY_VALUE(IP_Dimension_1_Score) AS score
FROM `cognito-prod-394707.cognito_prod_datamart.glp_master_loe`
WHERE LOWER(Drug_Name) = 'semaglutide'
GROUP BY Type;
```

```bash
gsutil ls "gs://<REPORT_GCS_BUCKET>/Cognito_new/reports/Semaglutide/IP/"
gsutil ls "gs://<GCS_BUCKET>/Cognito_new/pipeline_cache/Semaglutide/IP/"
```

---

## 3. Project structure

```
ip-refactored/
├── README.md                 project overview
├── .env.example              template for .env (copy, fill in, never commit .env)
├── .dockerignore / .gitignore
├── Dockerfile                Cloud Run Jobs image
├── pyproject.toml            package metadata and the `run-ip-dimension` command
├── requirements.txt
├── docs/RUN_ON_LINUX_VM.md   this guide
└── ip_pillar/
    ├── README.md             the shared helper modules
    ├── run_ip_dimension.py   entry point: picks the drugs, runs the dimensions, shards on Cloud Run
    ├── config.py             every setting / env var (loads .env)
    ├── utils.py              pure shared helpers, and the store_payload decorator
    ├── gcp_utils.py, gcp_helpers.py   GCS, BigQuery and report/payload upload
    ├── bq_utils.py           shared BigQuery reads and writes (replace a drug's rows, Gemini stats)
    ├── gemini_utils.py       Vertex AI Gemini calls, retries, usage counters
    ├── alloydb_utils.py, indexer.py, dates.py   AlloyDB storage; PDF text, chunks, embeddings; patent dates
    ├── drug_list.py, patent_filter.py, master_loe.py   which drugs and patents; glp_master_loe as the dimensions read it
    ├── report_utils.py       shared ReportLab layout and the GCS report upload
    ├── delete_rows.py        maintenance deletes on the LOE tables
    ├── primary_market_entry_horizon/        dimension 1 (README inside)
    │   ├── primary_market_entry_horizon.py  the dimension function
    │   ├── horizon_helpers.py, bq_utils.py
    │   ├── blocking_analysis/               five-step blocking analysis
    │   ├── score_calculation/               years to entry, phases, approval dates, glp_master_loe merge
    │   ├── forecast/                        forecast.py + patent_layering/, patern_analysis/, strategy_analysis/, forecast_pipeline/
    │   └── generate_report_and_rationale/
    ├── patent_strength/                     dimension 2 (README inside)
    ├── patent_thicket_and_circumvention/    dimension 3 (README inside)
    ├── global_launch_sequencing_and_geographic_arbitrage/   dimension 4 (README inside)
    └── litigations/                         dimension 5 (README inside)
```

Each dimension folder has the same shape: a file holding only the dimension function, a `*_helpers.py`, then
`retrieval/`, `scoring/` (or `search/`), `bq_utils.py` or `storage/`, `constants.py` and
`generate_report_and_rationale/`.

---

## 4. `.env` reference

**Required on a VM**

| Variable | Example | Notes |
|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | `/home/me/.secrets/ip-pipeline-sa.json` | absolute path; **leave unset on Cloud Run or a VM with an attached service account** |
| `PROJECT_ID` | `cognito-prod-394707` | billing project for BigQuery and Vertex AI |
| `GCS_BUCKET` | `cognito-gcs` | patent PDFs, caches, payloads |
| `ALLOYDB_HOST` | `10.x.x.x` | AlloyDB private IP |
| `ALLOYDB_PASSWORD` | | |

**Optional (defaults shown)**

| Variable | Default |
|---|---|
| `IP_DIMENSION` | `all` |
| `DRUG_NAME` | empty (all GLP-1 drugs) |
| `REINDEX` | `false` |
| `RUN_FORECAST` | `true` |
| `NO_MARKETED_SKIP` | `false` (forecast rows of Marketed / Pre-registration drugs are dropped) |
| `ALLOYDB_USER` / `ALLOYDB_DB` / `ALLOYDB_PORT` | `postgres` / `postgres` / `5432` |
| `PATENT_MASTER_TABLE` | `cognito-dev-380506.stage.patent_master` |
| `CLINICAL_EFFICACY_TABLE` | `cognito-dev-380506.data_mart.clinical_efficacy_glp1` |
| `DRUG_DETAILS_TABLE` | `cognito-dev-380506.data_mart.vw_drug_details_full` |
| `DRUG_LIST_VIEW` | `cognito-prod-394707.cognito_prod_datamart.vw_drug_details_full` |
| `BQ_*_TABLE` (outputs) | see section 2.1; set to override |
| `GCS_PATENTS_PREFIX` | `Cognito_new/Master_patent_list` |
| `PAYLOAD_PATH` | `Cognito_new/pipeline_cache` |
| `REPORT_GCS_BUCKET` / `REPORT_GCS_PATH` | `cognito-prod` / `Cognito_new/reports` |
| `GCS_CACHE_PREFIX` / `GCS_JSON_EXPORT` | `pipeline_cache` / `loe_json_exports` |
| `GEMINI_TEXT_MODEL` / `GEMINI_EMBEDDING_MODEL` | `gemini-2.5-flash` / `gemini-embedding-001` |
| `GEMINI_LOCATION` / `GEMINI_EMBEDDING_LOCATION` | `global` / `us-central1` |
| `FORMULATION_EXCEL_PATH`, `PUBMED_EMAIL`, `NCBI_API_KEY` | empty (used by the blocking analysis) |
| `INDEXER_CONCURRENCY`, `ANALYSIS_CONCURRENCY` | `10`, `5` |

The full list, with comments, is in `ip_pillar/config.py`.
