# patent_strength

IP dimension: **Patent Strength and Invalidity Opportunity**. How legally robust is each blocking, granted
patent of a drug, and how open is it to an invalidity challenge? A higher score means a stronger patent.

```python
from ip_pillar.patent_strength import patent_strength

result = await patent_strength("semaglutide")   # the drug name is the only input
```

Input is the drug's `glp_master_loe` rows, so run `primary_market_entry_horizon` for the drug first.
`config.REINDEX` ignores the checkpoint and re-scores everything.

## What one run does

1. Load the drug's `glp_master_loe` rows.
2. **Shortlist** (`retrieval/shortlist.py`): Tag = Blocking, a Grant Date present, Type not Forecasted, and approved in `patent_master`.
3. Load already-scored sub-factors (GCS checkpoint, BigQuery fallback) so finished work is not repeated.
4. **Score** each patent on four legal-robustness sub-factors with Gemini, using patent chunks retrieved once from AlloyDB and re-ranked per sub-factor (`retrieval/chunks.py`, `scoring/`):

   | Sub-factor | Weight |
   |---|---|
   | SF1 Novelty and Non-Obviousness | 40% |
   | SF2 Obvious-to-Combine Risk | 30% |
   | SF3 Prosecution History Vulnerability | 20% |
   | SF4 Secondary Considerations | 10% |

5. **Country-weighted score** (`scoring/scoring.py`): the patents' weighted scores are averaged per jurisdiction, then combined with country weights (US 25%, EP 25%, and 5% each for CN, CA, AU, KR, IN, BR, MX, TW, JP, RU) into the drug's **final patent score**.
6. **Rationale**: a short summary (200 words at most) of the drug, stored on every country row.
7. Write the two BigQuery tables.
8. Build the PDF report and upload it to GCS.

If no patent passes the shortlist, the run returns a fixed "no qualifying patents" rationale and writes nothing.

## Folder structure

```
patent_strength/
├── patent_strength_analysis.py     the dimension function, nothing else
├── strength_helpers.py             log summaries, report builder
├── retrieval/
│   ├── shortlist.py                which glp_master_loe patents are scored
│   └── chunks.py                   AlloyDB chunks, re-ranked per sub-factor
├── scoring/
│   ├── scoring.py                  Gemini sub-factor scoring, weighted and country scores
│   ├── prompts.py                  the sub-factor prompts
│   ├── pipeline.py                 score cache and concurrent scoring
│   └── constants.py                weights, labels, keyword lists
├── storage/
│   ├── bq_utils.py                 this dimension's BigQuery reads and writes
│   └── checkpoint.py               per-drug GCS checkpoint
└── generate_report_and_rationale/
    ├── generate_rationale.py       the drug's short rationale
    └── generate_report.py          the PDF report
```

Each folder also has an `__init__.py`.

## Files

| Path | Role |
|---|---|
| `patent_strength_analysis.py` | the `patent_strength()` function and nothing else |
| `strength_helpers.py` | log summaries and the report builder |
| `retrieval/` | `shortlist.py` (which patents), `chunks.py` (AlloyDB chunks + re-ranking) |
| `scoring/` | `scoring.py` (Gemini sub-factors, weighted and country scores), `prompts.py`, `pipeline.py` (score cache + concurrency), `constants.py` (weights, labels, keywords) |
| `storage/` | `bq_utils.py` (this dimension's BigQuery I/O), `checkpoint.py` (per-drug GCS checkpoint) |
| `generate_report_and_rationale/` | `generate_rationale.py`, `generate_report.py` |

## Outputs

| Output | Location |
|---|---|
| Per-patent scores | BigQuery `BQ_PATENT_STRENGTH_TABLE` (`patent_strength_analysis_table`) |
| Country scores + rationale | BigQuery `BQ_COUNTRY_SCORE_TABLE` (`patent_strength_country_score_table`) |
| Report | `gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/IP/Patent Strength and Invalidity Opportunity.pdf` (+ `archive/`) |
| Checkpoint | GCS, `ipd2_checkpoints/scoring_checkpoint.json` per drug |
| Run payload | `gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/IP/Patent Strength and Invalidity Opportunity/output_payload.json` |

Each run replaces only that drug's rows.


