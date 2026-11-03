# primary_market_entry_horizon

IP dimension 1. For one GLP-1 drug it works out **how many years remain before a generic or biosimilar can
realistically enter the market**, per patent and per jurisdiction, and turns that into 1-5 scores. Every other
dimension reads the table this one writes (`glp_master_loe`), so run it first.

```python
from ip_pillar.primary_market_entry_horizon import primary_market_entry_horizon

result = await primary_market_entry_horizon("semaglutide")   # the drug name is the only input
```

`config.REINDEX` (`REINDEX=true`, or `run_ip_dimension --reindex`) ignores the caches and redoes everything.

## What one run does

| # | Step | Where |
|---|---|---|
| 1 | List the drug's approved patent PDFs (GCS, filtered by `patent_master`) | `ip_pillar/patent_filter.py` |
| 2 | Download, extract text, chunk, embed and store the PDFs in AlloyDB | `ip_pillar/indexer.py` |
| 3 | Fetch the clinical development timeline (phase per geography) | `score_calculation/phase_fetcher.py` |
| 4 | **Blocking analysis** (below) | `blocking_analysis/` |
| 5-6 | Assign `phase_at_filing`, first-pass scores | `score_calculation/` |
| 7 | Real approval dates for Marketed jurisdictions (FDA / EMA) | `score_calculation/approval_date_fetcher.py` |
| 8 | Second-pass scores with the real dates | `score_calculation/patent_scores.py` |
| 9 | `glp_patent_loe` to BigQuery + a JSON snapshot in GCS | `horizon_helpers.py`, `bq_utils.py` |
| 10 | **Forecast** of future filings (below); skipped if `RUN_FORECAST` is off or the drug is Marketed / Pre-registration | `forecast/` |
| 11 | `glp_master_loe` (patents merged with `forecasted_loe`, re-scored) to BigQuery + JSON | `score_calculation/merge_forecasted_loe.py` |
| 12 | PDF report from the `glp_master_loe` rows | `generate_report_and_rationale/` |
| 13 | Blocking Analysis PDF report | `blocking_analysis/generate_report.py` |

A failed forecast or report is recorded in the result and does not fail the run.

## Blocking analysis (`blocking_analysis/`)

Decides whether each patent is **BLOCKING** or **NON-BLOCKING** for a would-be generic.

1. **Claim classification** - the primary claim goes into one of seven categories (Composition of Matter, Salt/Polymorph, ...).
2. **Claim elements** - does the patent cover the product's active ingredient, formulation, route, device or combination? (uses the optional `FORMULATION_EXCEL_PATH` file)
3. **Scientific barrier** - does the feature solve a real technical problem? (evidence from PubMed / Europe PMC / the web)
4. **Development feasibility** - would removing the feature fail approval, or force new Phase I / bridging studies?
5. **Novelty and difficulty** - is the solution novel and technically hard? Only a HIGH-confidence verdict stays BLOCKING.

The earliest-filed Composition-of-Matter patent per jurisdiction is BLOCKING straight away; the rest go through
steps 2-5 and stop at the first NON-BLOCKING answer. Patents run concurrently (`ANALYSIS_CONCURRENCY`).

## Scores (`score_calculation/`)

Pure logic on in-memory patent dicts:

1. estimated approval date (today + phase offset), replaced by the real date when known
2. exclusivity date = approval + jurisdiction offset (US +5 y, EP +10 y, other +8 y; pediatric exclusivity adds 6 months in the US)
3. controlling patent expiry = filing date + 20 years (+ PTE)
4. **years to entry** = max(patent expiry, exclusivity) - today
5. **Score** (1-5) from the average years to entry over all jurisdictions; **IP Dimension 1 Score** from US + EP only. Fewer years means a higher score (thresholds `SCORE_THRESHOLD_*` in `utils.py`).

## Forecast (`forecast/`)

Predicts how many more patents of each type the innovator will file per jurisdiction, and scores them like real
patents. Input `glp_patent_loe`; output `forecasted_loe`.

`forecast.py` holds `forecast(drug_name)`, which runs three analyses together and then the forecast:

| Folder | Produces |
|---|---|
| `patent_layering/` | IP landscape, patent layering (AlloyDB patent text + Gemini), phase at filing, typical filing stage -> `forecast_s3` |
| `patern_analysis/` | the innovator's filing pattern across its portfolio -> `filing_pattern_table` |
| `strategy_analysis/` | the company's business strategy and incentive to expand protection (SEC EDGAR + Gemini) -> `company_analysis_table` (company level, reused unless reset) |
| `forecast_pipeline/` | the Gemini forecast per patent type and jurisdiction, and its scoring -> `forecasted_loe` |

`forecast_helpers.py` holds the helpers `forecast()` calls; `generate_report/` builds its PDF. Each analysis
folder has a `<folder>.py` that can be run on its own, for example
`python -m ip_pillar.primary_market_entry_horizon.forecast.strategy_analysis.strategy_analysis`.

## Files in this folder

| File | Role |
|---|---|
| `primary_market_entry_horizon.py` | the `primary_market_entry_horizon()` function and nothing else |
| `horizon_helpers.py` | LOE-table publishing, the forecast step, the two reports |
| `bq_utils.py` | BigQuery reads and the LOE table writes (+ JSON snapshot) |
| `score_calculation/` | scores, phases, approval dates, the `glp_master_loe` merge |
| `blocking_analysis/` | the five blocking steps and their report |
| `forecast/` | the forecast |
| `generate_report_and_rationale/` | the "Rationale" column and the PDF report |

## Outputs

| Output | Location |
|---|---|
| `glp_patent_loe` | BigQuery `BQ_PATENT_LOE_TABLE` |
| `glp_master_loe` | BigQuery `BQ_MASTER_LOE_TABLE` |
| `forecast_s3`, `filing_pattern_table`, `company_analysis_table`, `forecasted_loe` | BigQuery (`BQ_FORECAST_S3_TABLE`, `BQ_FILING_PATTERN_TABLE`, `BQ_COMPANY_ANALYSIS_TABLE`, `FORECASTED_LOE_TABLE`) |
| JSON snapshots of the LOE tables | `gs://{GCS_BUCKET}/pipeline_cache/{GCS_JSON_EXPORT}/{table}/{drug}_{timestamp}.json` |
| Reports (Primary Market Entry Horizon, Blocking Analysis, Forecast Analysis) | `gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/IP/{name}.pdf` (+ `archive/`) |
| Run payload (the result dict) | `gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/IP/Primary Market Entry Horizon/output_payload.json` |

Each run replaces only that drug's rows.

