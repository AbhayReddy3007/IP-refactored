# litigations

IP dimension 5: the **patent litigation and court cases** found for a drug's patents - ANDA Paragraph IV, IPR at
PTAB, EPO oppositions, India (Delhi High Court) and compounding-pharmacy suits.

```python
from ip_pillar.litigations import litigation_analysis

result = await litigation_analysis("semaglutide")   # the drug name is the only input
```

Unlike the other dimensions it reads **`patent_master`** (patents with confidence above
`PATENT_CONFIDENCE_THRESHOLD`, default 0.3), not `glp_master_loe`, so it does not depend on
`primary_market_entry_horizon`.

## What one run does

1. **Patents** (`retrieval/patents.py`): the drug's patent numbers and filing dates from `patent_master`. Patents already in the litigation table are skipped, so a patent is never searched twice. A patent with no cases gets a row with no case, so it is not searched again either. To search a patent again, delete its rows from the table.
2. **Innovator and brands** (`search/steps.py`, `retrieval/marketed.py`): the innovator, plus brand names when the drug is Marketed, found with Gemini and Google Search.
3. **Litigation search** (`search/steps.py`): patents in groups of 5 per Gemini call (4 groups at once), with Google Search grounding. A group whose reply cannot be parsed is split and searched again; patents that still fail are reported in `failed_patents` and retried next run.
4. **Clean-up** (`search/results.py`): case types normalised to ANDA / IPR / EPO / India / Compounding / Other, duplicates dropped, cases grouped by type and summarised. Totals cover earlier cases too.
5. **BigQuery** (`bq_utils.py`): the earlier rows plus this run's, with each patent's filing date; the drug's older rows are replaced.
6. **PDF report** (`generate_report/`): a two-page report from the drug's rows (Gemini + Google Search), built every run, also when every patent was already in the table.

If `patent_master` has no patents above the threshold for the drug, the run returns an error.

## Files

| Path | Role |
|---|---|
| `litigation_analysis.py` | the `litigation_analysis()` function and nothing else |
| `litigation_helpers.py` | the report builder |
| `bq_utils.py` | read earlier rows, build rows, merge cases, write the table |
| `constants.py` | search sizes, temperatures, case types and aliases |
| `retrieval/` | `patents.py`, `marketed.py` |
| `search/` | `steps.py`, `results.py`, `prompts.py`, `json_utils.py` (tolerant parsing of Gemini JSON) |
| `generate_report/generate_report.py` | the PDF report |

## Outputs

| Output | Location |
|---|---|
| Litigation rows | BigQuery `BQ_LITIGATION_TABLE` (`litigation_analysis_table`) |
| Report | `gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/IP/Litigation Analysis.pdf` (+ `archive/`) |
| Run payload | `gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/IP/Litigation Analysis/output_payload.json` |

