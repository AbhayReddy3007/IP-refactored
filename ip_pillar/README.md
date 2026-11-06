# ip_pillar

The package behind the IP pipeline. It holds the five dimension packages and the **shared helper modules** they
all use. This file covers the helpers; each dimension has its own README:

| Dimension package | README |
|---|---|
| `primary_market_entry_horizon/` | [README](primary_market_entry_horizon/README.md) |
| `patent_strength/` | [README](patent_strength/README.md) |
| `patent_thicket_and_circumvention/` | [README](patent_thicket_and_circumvention/README.md) |
| `global_launch_sequencing_and_geographic_arbitrage/` | [README](global_launch_sequencing_and_geographic_arbitrage/README.md) |
| `litigations/` | [README](litigations/README.md) |

For the whole project (running it, configuration, data flow) see the [root README](../README.md).

## How the helpers are organised

```
run_ip_dimension.py    entry point: picks the drugs, runs the dimensions
        |
dimension packages  -->  *_helpers.py (each dimension's own helpers)
        |
shared helpers:  config  utils  drug_list  patent_filter  master_loe  dates  indexer
                 gemini_utils  gcp_utils  gcp_helpers  bq_utils  alloydb_utils  report_utils  delete_rows
```

Two kinds of helpers exist:

- **Shared helpers** (this folder's `.py` files): used by more than one dimension.
- **Dimension helpers** (`horizon_helpers.py`, `strength_helpers.py`, `thicket_helpers.py`, `launch_helpers.py`,
  `litigation_helpers.py`, and `forecast/forecast_helpers.py`): used by one dimension only. Each dimension's main
  file holds just its one function and imports these.

## Shared helper modules

### Settings and pure helpers

| Module | What it provides |
|---|---|
| `config.py` | Every setting, read from environment variables (`.env` is loaded). Table names, buckets and paths, Gemini models, concurrency, `IP_DIMENSION`, `DRUG_NAME`, `REINDEX`, `RUN_FORECAST`, the `DIMENSION_*` report names. |
| `utils.py` | Pure functions with no Google Cloud calls. Logging: `setup_logging`. Names and text: `normalize`, `normalize_patent_number`, `normalize_drug_str`, `safe_name`, `norm_col`, `norm_ws`, `is_blank`, `norm_phase`, `safe_collection_name`. DataFrame: `find_col`, `stringify_cell`. Jurisdictions: `jurisdiction_code`, `split_jurisdiction_tokens`, `extract_jurisdiction`. Dates: `format_date_dmy`, `year_from_value`, `clean_date`. Scoring: `ip_dimension_score` (average years to entry to a 1-5 score). BigQuery naming: `bq_safe_name`. Misc: `fmt_elapsed`, `chunk_text`. Also the `store_payload` decorator (below). |

`store_payload(dimension)` wraps a dimension function. After a successful run it saves the result dict as JSON to
`gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/IP/{dimension}/output_payload.json` (plus a timestamped copy under
`archived/`) and adds `payload_uri` to the result. A result with an error is not saved; a failed upload only logs.

### Which drugs and patents

| Module | What it provides |
|---|---|
| `drug_list.py` | `drug_list` (the GLP-1 drug query against BigQuery), `resolve_drug_name` (a name as the drug list spells it, used by every dimension), `list_glp1_patent_files` (drugs with patent PDFs in GCS), `list_glp1_drugs_with_patent_master_rows` (drugs with patents in `patent_master`, for litigations), `align_drug_name` / `align_drug_names` (fix the Drug Name spelling on the LOE tables). |
| `patent_filter.py` | `patent_filter(drug)` lists the drug's patent PDFs in GCS, keeping only patents `patent_master` approves (status "PDF Downloaded", `eval_refined_confidence` above 0.3). `get_approved_patent_numbers` returns the approved numbers for one molecule or all. |
| `master_loe.py` | `load_master_data(drug)` returns the drug's `glp_master_loe` rows the way the downstream dimensions use them: friendly column names, normalised text, numeric forecast counts, approved patents only, de-duplicated. |

### Patent text and dates

| Module | What it provides |
|---|---|
| `indexer.py` | `indexer(drug)` runs the per-drug pipeline: download each PDF from GCS, extract text (Gemini, then PyMuPDF, then OCR as fallbacks), chunk, embed, and store in AlloyDB, skipping patents already indexed and copying from another drug's collection when the patent is already there. Progress is checkpointed to GCS. Also `get_or_create_collection`, `index_text`, `extract_text_via_*`, `generate_embeddings`. |
| `alloydb_utils.py` | AlloyDB (PostgreSQL + pgvector) storage. `AlloyDBClient` / `AlloyDBCollection` mimic the ChromaDB interface; `alloydb_client()` returns the shared instance; `query_collection` returns all chunks of one patent; `fetch_relevant_chunks` returns the chunks of one patent ranked by keyword relevance. |
| `dates.py` | Filing and grant dates: `extract_dates_from_pdf` (cover page, with a vision fallback), `get_master_filing_date` (from `patent_master`), `resolve_dates`, `get_dates` / `get_dates_from_alloydb`, `get_dates_with_pdf_fallback`. |

### Gemini

| Module | What it provides |
|---|---|
| `gemini_utils.py` | Every Gemini call goes through here (Vertex AI, Application Default Credentials). `gemini_generate`, `gemini_api_call` (optional search, response format, PDF inputs), `gemini_json_call` (reply parsed as one JSON object), `gemini_embed`, and `_async` versions that run in a worker thread. Usage counters: `reset_gemini_call_stats`, `get_gemini_call_stats`, `isolated_gemini_call_stats` (lets a nested run such as the forecast keep its own counters). |

### Google Cloud storage and BigQuery

| Module | What it provides |
|---|---|
| `gcp_utils.py` | Clients: `get_bq_client`, `get_gcs_client`. GCS: `write_json`, `read_json`, `write_bytes`, `read_bytes`, `blob_exists`, `delete_blob`, `list_blob_names`, `list_blobs_with_prefix` (writes retry on transient errors). Uploads: `upload_dimension_report_pdf_to_gcs`, `upload_dimension_payload_cache_to_gcs`. BigQuery: `append_dimension_score_to_bigquery`. |
| `gcp_helpers.py` | Extras on top of `gcp_utils`: `call_gemini_json`, `upload_blob` (to an absolute path in a chosen bucket), `run_query` (returns a DataFrame, with parameters), `table_exists`, `delete_rows`, `load_dataframe` (append with an explicit schema). |
| `bq_utils.py` | BigQuery work shared by every dimension. Reads: `load_master_table`, `load_master_loe`, `load_patent_loe`. Writes: `replace_drug_rows` / `write_drug_rows` (delete the drug's rows, then load the new ones typed to the table's schema, so a re-run never duplicates), `table_schema`, `coerce_to_schema`. Gemini usage: `save_gemini_stats` (one row per model per run; never raises), `append_gemini_stats_to_bigquery`. `now_iso` gives the `updated_at` format. |
| `delete_rows.py` | Maintenance deletes on the LOE tables: `delete_drug_rows`, `delete_low_confidence_rows`. As a one-off sweep: `python -m ip_pillar.delete_rows [--drug Semaglutide]`. |

### Reports

| Module | What it provides |
|---|---|
| `report_utils.py` | The ReportLab building blocks and the upload for every PDF report. Layout: `styles`, `document`, `section`, `paragraphs`, `data_table` (scores coloured by 1-5 band), `stat_strip`, `band`, `shorten`, `esc`. Upload: `report_gcs_paths`, `upload_report` (current copy and a timestamped archive copy), `publish_report` (build in a temp folder, then upload). |

## Dimension helpers

Each dimension keeps the code its main function calls in a separate file:

| File | Helpers |
|---|---|
| `primary_market_entry_horizon/horizon_helpers.py` | `marketed_in`, `split_csv`, `publish_loe_tables` (build and publish `glp_patent_loe` / `glp_master_loe`), `run_forecast`, `generate_report`, `generate_blocking_report` |
| `primary_market_entry_horizon/forecast/forecast_helpers.py` | `resolve_company`, `layering`, `filing_pattern`, `analyse_company`, `innovator_text`, `company_text`, `generate_report` |
| `patent_strength/strength_helpers.py` | `log_country_scores`, `log_sub_factor_summary`, `log_final_scores`, `log_output_tables`, `generate_report` |
| `patent_thicket_and_circumvention/thicket_helpers.py` | `generate_report` |
| `global_launch_sequencing_and_geographic_arbitrage/launch_helpers.py` | `generate_reports` (the Dimension IV report and the PTE report) |
| `litigations/litigation_helpers.py` | `generate_report` |

The `generate_report*` helpers import ReportLab only when called, so a run that builds no report does not need it.
They return `{"error": ...}` on failure instead of raising, which is why a failed report never fails a run.

## Adding a helper

- Used by one dimension: add it to that dimension's `*_helpers.py` (or a more specific module such as `scoring/`) and import it in the main file.
- Used by several: add it to the closest shared module above. Pure functions with no Google Cloud calls go in `utils.py`; BigQuery work shared by dimensions goes in `bq_utils.py`; Gemini calls go through `gemini_utils.py`.
- Read tunables from `config.py`, not from inside the helper.
