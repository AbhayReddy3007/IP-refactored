"""
IP_refactored
─────────────
Top-level package.

Layout:
    config.py                     — every configuration variable / env var (single source of truth)
    gcp_utils.py                  — every function that talks to Google Cloud / Gemini directly
    chunking/                     — patent fetching + chunking + AlloyDB storage sub-package
    blocking_analysis/            — 5-step patent blocking analysis sub-package
    primary_market_entry_horizon/ — derived score calculation sub-package (+ its supporting
                                     modules: phase_fetcher, approval_date_fetcher, excel_exporter)
    dimension_1.py                 — Cloud Run Jobs entry point tying everything together;
                                     not imported here since it's meant to be run as a script:
                                       python -m IP_refactored.dimension_1 --drug <name>

agent.py has been removed entirely — dimension_1.py is its replacement,
without the Google ADK dependency.
"""

from . import config
from . import gcp_utils
from . import chunking
from . import blocking_analysis
from . import primary_market_entry_horizon

__all__ = ["config", "gcp_utils", "chunking", "blocking_analysis", "primary_market_entry_horizon"]
