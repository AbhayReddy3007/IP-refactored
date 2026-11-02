# IP-refactored

The **IP pillar** pipeline for GLP-1 drugs. For each drug it analyses the drug's patents with Gemini, works out how
long the drug is protected in each market, and writes scores, tables and PDF reports to BigQuery and GCS. It runs
as a Cloud Run Job or on a Linux VM. Python 3.11, package name `ip_pillar`.

## The five dimensions

Each dimension is a package with one function. **The only input is the drug name.**

| Dimension | Function | Question it answers | README |
|---|---|---|---|
| Primary Market Entry Horizon | `primary_market_entry_horizon(drug_name)` | How many years until a generic can enter, per patent and jurisdiction? (includes blocking analysis and a forecast of future filings) | [README](ip_pillar/primary_market_entry_horizon/README.md) |
| Patent Strength and Invalidity Opportunity | `patent_strength(drug_name)` | How legally robust is each blocking patent? | [README](ip_pillar/patent_strength/README.md) |
| Patent Thicket and Circumvention Feasibility | `patent_thicket_and_circumvention(drug_name)` | How dense is the secondary-patent fence, and can it be designed around? | [README](ip_pillar/patent_thicket_and_circumvention/README.md) |
| Global Launch Sequencing and Geographic Arbitrage | `global_launch_sequencing_and_geographic_arbitrage(drug_name)` | In what order can the drug launch across countries, and how much does expiry differ? | [README](ip_pillar/global_launch_sequencing_and_geographic_arbitrage/README.md) |
| Litigations | `litigation_analysis(drug_name)` | What patent litigation and court cases exist for the drug's patents? | [README](ip_pillar/litigations/README.md) |

```python
from ip_pillar.primary_market_entry_horizon import primary_market_entry_horizon
from ip_pillar.patent_strength import patent_strength

await primary_market_entry_horizon("semaglutide")
await patent_strength("semaglutide")
```

## How the data flows

```
GCS patent PDFs + patent_master (BigQuery)
        |
        v   primary_market_entry_horizon: index PDFs in AlloyDB -> blocking analysis -> scores
        |                                  -> glp_patent_loe -> forecast -> glp_master_loe
        v
   glp_master_loe ---> patent_strength
                  ---> patent_thicket_and_circumvention
                  ---> global_launch_sequencing_and_geographic_arbitrage

patent_master -------> litigations        (independent of glp_master_loe)
```

`primary_market_entry_horizon` must run first for a drug; the three middle dimensions read its `glp_master_loe`
rows. `litigations` reads `patent_master` only. With `IP_DIMENSION=all` the runner runs them in that order and
skips the dependents if the horizon run fails.

Every run, per dimension:
- writes its BigQuery tables, replacing only that drug's rows (so a re-run never duplicates);
- uploads a PDF report (and an archived copy) to GCS;
- saves the result dict as JSON to GCS (`output_payload.json`, plus an archived copy);
- writes one Gemini usage row per model to `GEMINI_STATS_TABLE`, even when the run fails.

## Running it

```bash
pip install -r requirements.txt          # or: pip install -e .
cp .env.example .env                     # then fill it in

python -m ip_pillar.run_ip_dimension --drug "Semaglutide"            # one drug
python -m ip_pillar.run_ip_dimension --drug "Semaglutide" --reindex  # ignore caches, redo everything
DRUG_NAME="Semaglutide, Tirzepatide" python -m ip_pillar.run_ip_dimension
python -m ip_pillar.run_ip_dimension                                 # every GLP-1 drug with patents in GCS
```

- **Which dimension:** `IP_DIMENSION` is `all`, `primary_market_entry_horizon`, `patent_strength`,
  `patent_thicket_and_circumvention`, `global_launch_sequencing_and_geographic_arbitrage` or `litigations`.
  **The default in `config.py` is `litigations`**, so set it to `all` to run everything.
- **Which drugs:** `--drug`, then `DRUG_NAME` (comma-separated list allowed), otherwise every GLP-1 drug found in GCS.
- **Sharding:** on Cloud Run Jobs, `CLOUD_RUN_TASK_INDEX` and `CLOUD_RUN_TASK_COUNT` deal the drug list out across tasks.
- **Reindex:** `--reindex` sets `config.REINDEX` (or set `REINDEX=true`); the dimension functions read it.
- **Exit code:** 0 if every drug succeeded, 1 if any failed. One drug's failure does not stop the others.

Step-by-step VM setup (service account roles, `.env`, AlloyDB, logging) is in
[`docs/RUN_ON_LINUX_VM.md`](docs/RUN_ON_LINUX_VM.md). Docker: `docker build -t ip-pillar .` (the entrypoint is
`python -m ip_pillar.run_ip_dimension`; on Cloud Run the job's service account is used, so leave
`GOOGLE_APPLICATION_CREDENTIALS` unset).

## Configuration

All settings are environment variables read in `ip_pillar/config.py` (`.env` is loaded automatically; see
`.env.example`). The ones you will touch most:

| Variable | Purpose |
|---|---|
| `PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS` | GCP project and service account key (unset on Cloud Run) |
| `GCS_BUCKET`, `GCS_PATENTS_PREFIX` | where the patent PDFs live: `gs://{GCS_BUCKET}/{GCS_PATENTS_PREFIX}/{Drug}/*.pdf` |
| `ALLOYDB_HOST`, `ALLOYDB_PASSWORD` (+ user, db, port) | AlloyDB with pgvector, for patent chunk embeddings |
| `PATENT_MASTER_TABLE`, `CLINICAL_EFFICACY_TABLE`, `DRUG_DETAILS_TABLE`, `DRUG_LIST_VIEW` | BigQuery inputs |
| `BQ_*_TABLE` | BigQuery outputs of each dimension |
| `REPORT_GCS_BUCKET`, `REPORT_GCS_PATH` | PDF reports: `{REPORT_GCS_PATH}/{drug}/IP/{Report}.pdf` |
| `PAYLOAD_PATH` | JSON payloads: `{PAYLOAD_PATH}/{drug}/IP/{Dimension}/output_payload.json` (in `GCS_BUCKET`) |
| `GEMINI_TEXT_MODEL`, `GEMINI_EMBEDDING_MODEL`, `GEMINI_LOCATION` | Vertex AI models (no API key; the service account needs "Vertex AI User") |
| `IP_DIMENSION`, `DRUG_NAME`, `REINDEX`, `RUN_FORECAST` | what to run and how |
| `ANALYSIS_CONCURRENCY`, `INDEXER_CONCURRENCY` | parallelism |

## Layout

```
ip_pillar/
  run_ip_dimension.py                  entry point: picks the drugs, runs the dimensions
  config.py                            every setting
  utils.py                             shared pure helpers, plus the store_payload decorator
  gcp_utils.py, gcp_helpers.py         GCS, BigQuery and Gemini calls
  bq_utils.py                          shared BigQuery reads/writes (glp_master_loe, replace-a-drug's-rows, Gemini stats)
  gemini_utils.py                      Vertex AI Gemini calls, retries, usage counters
  alloydb_utils.py, indexer.py         AlloyDB storage; PDF text extraction, chunking, embedding
  patent_filter.py, drug_list.py       which patents and drugs to process
  master_loe.py, dates.py              glp_master_loe as the dimensions read it; patent date extraction
  report_utils.py                      shared ReportLab layout and the GCS report upload
  primary_market_entry_horizon/        dimension 1 (blocking_analysis/, forecast/, score_calculation/)
  patent_strength/                     dimension 2
  patent_thicket_and_circumvention/    dimension 3
  global_launch_sequencing_and_geographic_arbitrage/   dimension 4
  litigations/                         dimension 5
docs/RUN_ON_LINUX_VM.md                VM setup and operations guide
Dockerfile, pyproject.toml, requirements.txt, .env.example
```

The shared helper modules and each dimension's own helper file are described in [`ip_pillar/README.md`](ip_pillar/README.md).

Every dimension follows the same shape: one file holding only the dimension function, a `*_helpers.py` for what it
calls, then `retrieval/`, `scoring/` (or `search/`), `storage/` or `bq_utils.py`, `constants.py` and
`generate_report_and_rationale/`.

## Conventions

- **Drug name only.** The five dimension functions take `drug_name` and nothing else. Options such as "ignore the
  caches" come from config (`REINDEX`). The name is resolved to the drug list's spelling and used for every output.
- **Failures are contained.** A failed report or forecast is recorded in the result without failing the run; a
  failed payload upload only logs a warning.
- **No tests yet.** There is no test suite in this repository.
