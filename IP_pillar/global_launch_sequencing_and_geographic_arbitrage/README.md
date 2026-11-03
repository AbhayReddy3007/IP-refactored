# global_launch_sequencing_and_geographic_arbitrage

IP dimension IV: in which order a drug's patents let it launch across jurisdictions, and how much **geographic
arbitrage** the differences in expiry allow.

```python
from ip_pillar.global_launch_sequencing_and_geographic_arbitrage import (
    global_launch_sequencing_and_geographic_arbitrage)

result = await global_launch_sequencing_and_geographic_arbitrage("semaglutide")   # the drug name is the only input
```

Input is the drug's `glp_master_loe` rows, so run `primary_market_entry_horizon` first.
`config.REINDEX` ignores the approval dates and PTE of the previous run and looks everything up again.

## What one run does

1. Load the drug's `glp_master_loe` rows, approved patents only (`master_loe.load_master_data`).
2. **Shortlist** (`filters.py`): target jurisdictions (CN, IN, BR, AU, RU, US, CA, JP, MX, TW, KR), granted, BLOCKING, and key claim categories (Composition of Matter, Device, Formulation).
3. **Approval date** per marketed jurisdiction (`approval.py`) and **patent term extension (PTE)** per patent (`pte.py`), both looked up with Gemini. Lookups from an earlier run are reused from BigQuery.
4. **Expiry** per jurisdiction (`expiry.py`): filing date + 20 years, plus PTE under each country's rules (`PTE_RULES` in `constants.py`; India and Brazil allow none), capped after approval where the rules say so. The longest expiry per jurisdiction is kept.
5. **Arbitrage** (`arbitrage.py`): launch order, expiry gaps against the longest-protected jurisdiction, the protection gap (which key claim categories are present or absent), and a 1-5 score per country:

   | Score | Meaning |
   |---|---|
   | 5 | loss of exclusivity this year (immediate opportunity) |
   | 4 | 5 or more years earlier than the longest (strong arbitrage) |
   | 3 | 3-4 years earlier (meaningful) |
   | 2 | 2 years earlier (limited) |
   | 1 | no arbitrage |

   The average is the drug's **Dimension IV score**, rated from "FAIL - No viable geographic arbitrage" (1 or less) up to "Very Strong Geographic Arbitrage Opportunity" (above 4).
6. **Rationale**: a summary of the launch order and arbitrage, stored on every arbitrage row.
7. Two PDF reports, uploaded to GCS: the Global Launch Sequencing and Arbitrage report and the PTE Analysis report.
8. Write the two BigQuery tables.

If no patent passes the shortlist, the run returns a fixed "no qualifying patents" rationale.

## Folder structure

```
global_launch_sequencing_and_geographic_arbitrage/
├── global_launch_sequencing_and_geographic_arbitrage.py   the dimension function, nothing else
├── launch_helpers.py                                      the two-report builder
├── bq_utils.py                                            this dimension's BigQuery reads and writes
├── filters.py                                             shortlist: jurisdictions, granted, BLOCKING, key categories
├── approval.py                                            earliest approval date per jurisdiction
├── pte.py                                                 patent term extension per patent
├── expiry.py                                              expiry: filing + 20 years, PTE, post-approval cap
├── arbitrage.py                                           launch order, gaps, 1-5 score, Dimension IV score
├── lookups.py                                             concurrent Gemini lookups
├── constants.py                                           jurisdictions, filters, PTE rules, columns
└── generate_report_and_rationale/
    ├── generate_rationale.py                              summary of launch order and arbitrage
    └── generate_report/
        ├── generate_dim4_report.py                        Global Launch Sequencing & Arbitrage PDF
        ├── generate_pte_report.py                         PTE Analysis PDF
        └── utils.py                                       shared report helpers
```

Each folder also has an `__init__.py`.

## Files

| Path | Role |
|---|---|
| `global_launch_sequencing_and_geographic_arbitrage.py` | the pipeline function and nothing else |
| `launch_helpers.py` | the two-report builder |
| `bq_utils.py` | this dimension's BigQuery reads and writes |
| `filters.py`, `approval.py`, `pte.py`, `expiry.py`, `arbitrage.py` | the steps above |
| `constants.py` | jurisdictions, filters, PTE rules, table columns |
| `lookups.py` | shared Gemini lookup helpers |
| `generate_report_and_rationale/` | `generate_rationale.py`, and `generate_report/` (Dimension IV report, PTE report) |

## Outputs

| Output | Location |
|---|---|
| Shortlisted patents with expiries | BigQuery `BQ_LAUNCH_PATENTS_TABLE` (`shortlisted_secondary_patents_table`) |
| Arbitrage summary + rationale | BigQuery `BQ_ARBITRAGE_TABLE` (`arbitrage_summary_table`) |
| Reports | `gs://{REPORT_GCS_BUCKET}/{REPORT_GCS_PATH}/{drug}/IP/Global Launch Sequencing & Arbitrage.pdf` and `PTE Analysis.pdf` (+ `archive/`) |
| Run payload | `gs://{GCS_BUCKET}/{PAYLOAD_PATH}/{drug}/IP/Global Launch Sequencing & Arbitrage/output_payload.json` |

Each run replaces only that drug's rows.


