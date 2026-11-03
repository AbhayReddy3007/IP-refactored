# patent_thicket_and_circumvention

IP dimension: **Patent Thicket and Circumvention Feasibility**. How dense and varied is the fence of secondary
patents around a drug, and how feasible is it to design around it (505(b)(2))? A higher score means a weaker
thicket and an easier route around it.

```python
from ip_pillar.patent_thicket_and_circumvention import patent_thicket_and_circumvention

result = await patent_thicket_and_circumvention("semaglutide")   # the drug name is the only input
```

Input is the drug's `glp_master_loe` rows, so run `primary_market_entry_horizon` first.
`config.REINDEX` ignores the cached circumvention results and analyses every category again.

## What one run does

1. Load the drug's `glp_master_loe` rows (`master_loe.load_master_data`), keeping the patents `patent_master` approves.
2. Select the **NON-BLOCKING** patents (`retrieval/data_prep.py`). Composition-of-Matter patents are left out of the thicket count, as they are blocking patents, not fences.
3. **Thicket score** per jurisdiction (`scoring/scoring.py`), then the drug-level average:
   - *density* from the adjusted patent count: 0-5 scores 5, 6-15 scores 3, 16-30 scores 2, over 30 scores 1
   - *diversity* from the number of technology areas: 1-2 scores 5, 3 scores 3, 4-5 scores 2, 6 or more scores 1
   - base score = the lower of the two, minus 1 if both are 2 or less, plus 1 if validation is under 50%; final score is kept within 1-5
4. **Circumvention analysis** per claim category (`scoring/circumvention.py`): patent chunks from AlloyDB, Gemini and Google Search grounding. Up to 10 patents per category, 8 categories at once. Finished categories are cached in GCS.
5. **Rationale**: a summary of the scores and the design-around analysis, generated every run.
6. Build the PDF report and upload it to GCS.
7. Write the two BigQuery tables.

If the drug has no NON-BLOCKING patents, the run returns a fixed "no qualifying patents" rationale.

## Folder structure

```
patent_thicket_and_circumvention/
├── patent_thicket_circumvention_analysis.py   the dimension function, nothing else
├── thicket_helpers.py                         the report builder
├── bq_utils.py                                BigQuery writes for this dimension
├── retrieval/
│   └── data_prep.py                           claim categories, NON-BLOCKING selection
├── scoring/
│   ├── scoring.py                             density, diversity, final thicket score
│   ├── circumvention.py                       design-around analysis per claim category
│   ├── prompts.py                             the Gemini prompts
│   ├── checkpoint.py                          per-drug GCS cache of finished categories
│   └── constants.py                           excluded categories, limits, concurrency
└── generate_report_and_rationale/
    ├── generate_rationale.py                  summary of the scores and the analysis
    └── generate_report.py                     the PDF report
```

Each folder also has an `__init__.py`.

## Files

| Path | Role |
|---|---|
| `patent_thicket_circumvention_analysis.py` | the `patent_thicket_and_circumvention()` function and nothing else |
| `thicket_helpers.py` | the report builder |
| `bq_utils.py` | this dimension's BigQuery writes |
| `retrieval/data_prep.py` | claim categories and the NON-BLOCKING selection |
| `scoring/` | `scoring.py` (density, diversity, final score), `circumvention.py` (design-around analysis), `prompts.py`, `checkpoint.py` (GCS cache), `constants.py` |
| `generate_report_and_rationale/` | `generate_rationale.py`, `generate_report.py` |

## Outputs

| Output | Location |
|---|---|
| Design-around analysis | BigQuery `BQ_CIRCUMVENTION_TABLE` (`Circumvention_Table`) |
| Thicket scores | BigQuery `BQ_THICKET_SCORE_TABLE` (`Patent_Thicket_Score_Table`), with the rationale |
| Report | `gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/IP/Patent Thicket and Circumvention Feasibility.pdf` (+ `archive/`) |
| Circumvention cache | GCS, `ipd3_checkpoints/circumvention.json` per drug |
| Run payload | `gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/IP/Patent Thicket and Circumvention Feasibility/output_payload.json` |

Each run replaces only that drug's rows.


