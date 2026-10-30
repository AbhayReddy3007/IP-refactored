"""
generate_rationale.py
─────────────────────
Per-drug LOE (Primary Market) metrics and the plain-English rationale that
explains them. Operates on a drug's rows of the merged LOE table (friendly
column names, as built by score_calculation.merge_forecasted_loe.run_merge()):

    extract_drug_data(df)        -> dict of controlling patents, Years to Entry, score, counts, rationale
    generate_rationale(d)        -> the rationale string for such a dict
    rationale_for_drug(df)       -> rationale for one drug's rows (US/EP only)

run_merge() stores rationale_for_drug() in glp_master_loe's "Rationale" column;
generate_report.py reuses extract_drug_data() to build the PDF from the same table.
Pure logic — no I/O.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from ... import config
from ...utils import ip_dimension_score, is_blank, year_from_value

logger = logging.getLogger(__name__)

FORECASTED_COL = "No Of Forecasted Patents"
EXPIRY_COL = "Controlling Patent Expiry Year"

# Score 1 = >13 yrs = Very High Barrier (strongest protection)
# Score 5 = <=6 yrs = Minimal Barrier (weakest protection)
SCORE_LABELS = {1: "Very High Barrier", 2: "High Barrier", 3: "Moderate Barrier",
                4: "Low Barrier", 5: "Minimal Barrier"}


# ─────────────────────────────────────────────
# Value helpers
# ─────────────────────────────────────────────

def cell(row, col, default="N/A"):
    """A row's value as a clean string, or *default* when blank."""
    val = row.get(col)
    return default if is_blank(val) else str(val).strip()


def to_number(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def clean_year(val_str):
    """'2034.0' -> '2034'; non-numeric values pass through."""
    if val_str == "N/A":
        return val_str
    n = to_number(val_str)
    if n is not None and n == int(n):
        return str(int(n))
    return val_str


def fmt_number(val) -> str:
    if val is None:
        return "N/A"
    return str(int(val)) if val == int(val) else str(round(val, 1))


def loe_score(avg: Optional[float]) -> Optional[int]:
    """The 1-5 score for an average years-to-entry, or None when there is no average."""
    return None if avg is None else ip_dimension_score(avg)


def extract_year(date_str) -> Optional[int]:
    """4-digit year from YYYY-MM-DD, DD-MM-YYYY, YYYY, ... (None when blank)."""
    year = year_from_value(date_str)
    return None if year is None else int(year)


# ─────────────────────────────────────────────
# Forecasted-patent helpers
# ─────────────────────────────────────────────

def _find_forecasted_col(df: pd.DataFrame) -> Optional[str]:
    for c in df.columns:
        if c.lower().replace(" ", "").replace("_", "") in (
            "noofforecastedpatents", "numberofforecastedpatents",
            "forecastedpatents", "noofforecasted",
        ):
            return c
    return None


def normalize_forecasted_col(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee an integer FORECASTED_COL (0 where blank / missing)."""
    found = _find_forecasted_col(df)
    if found is None:
        logger.warning("[RATIONALE] No forecasted-patents column in %s — assuming 0", list(df.columns))
        df[FORECASTED_COL] = 0
    else:
        df[FORECASTED_COL] = pd.to_numeric(df[found], errors="coerce").fillna(0).astype(int)
    return df


def is_forecasted(row_or_filing_date, filing_date_str=None) -> bool:
    """A row is forecasted if Type says so, or its filing date is missing / year-only."""
    if isinstance(row_or_filing_date, dict):
        if cell(row_or_filing_date, "Type", "").lower() == "forecasted":
            return True
        filing_date_str = cell(row_or_filing_date, "Filing Date", "N/A")

    if not filing_date_str or filing_date_str == "N/A":
        return True
    s = str(filing_date_str).strip()
    n = to_number(s)
    if n is not None and n == int(n):
        s = str(int(n))
    return bool(re.match(r"^\d{4}$", s))


def forecasted_label(pn: str) -> str:
    """'US+Forecasted+Device' -> 'Forecasted patent for Device in US'."""
    parts = [p.strip() for p in pn.split("+")]
    if len(parts) >= 3 and parts[1].lower() == "forecasted":
        return f"Forecasted patent for {' '.join(parts[2:])} in {parts[0].upper()}"
    return pn


def _is_forecasted_type(df: pd.DataFrame) -> pd.Series:
    return df["Type"].astype(str).str.strip().str.lower() == "forecasted"


def _forecasted_count(subset: pd.DataFrame) -> int:
    if "Type" not in subset.columns:
        return 0
    fc_rows = subset[_is_forecasted_type(subset)]
    if fc_rows.empty or FORECASTED_COL not in fc_rows.columns:
        return 0
    return int(fc_rows[FORECASTED_COL].apply(to_number).dropna().sum())


def _existing_count(subset: pd.DataFrame) -> int:
    if "Type" not in subset.columns:
        return len(subset)
    return int((~_is_forecasted_type(subset)).sum())


# ─────────────────────────────────────────────
# Controlling patent + Years to Entry
# ─────────────────────────────────────────────

def find_controlling(blocking_df: pd.DataFrame):
    """The controlling patent among a jurisdiction's BLOCKING rows: the one with
    the latest expiry (a missing expiry is estimated as filing year + 20), else
    the latest filing date."""
    if blocking_df.empty:
        return None
    tmp = blocking_df.copy()
    tmp["_fs"] = tmp["Filing Date"].astype(str)

    if EXPIRY_COL in tmp.columns:
        tmp[EXPIRY_COL] = tmp[EXPIRY_COL].astype(object)
        for idx, row in tmp.iterrows():
            if is_blank(row.get(EXPIRY_COL)) or to_number(row.get(EXPIRY_COL)) is None:
                filing_year = extract_year(row.get("Filing Date"))
                if filing_year:
                    tmp.at[idx, EXPIRY_COL] = filing_year + config.PATENT_TERM_YEARS

        tmp["_expiry_num"] = tmp[EXPIRY_COL].apply(to_number)
        with_expiry = tmp[tmp["_expiry_num"].notna()]
        if not with_expiry.empty:
            return with_expiry.sort_values(["_expiry_num", "_fs"], ascending=[False, False]).iloc[0]

    return tmp.sort_values("_fs", ascending=False).iloc[0]


def _derive_years_to_entry(ctrl, blocking_df: pd.DataFrame, current_year: int) -> Optional[float]:
    """Years to Entry of the controlling patent; derived from its expiry (or
    filing year + 20) when blank, else from the forecasted patents."""
    if ctrl is not None:
        yte = to_number(cell(ctrl, "Years to Entry"))
        if yte is not None:
            return yte
        expiry = to_number(cell(ctrl, EXPIRY_COL))
        if expiry is not None:
            return expiry - current_year
        filing_year = extract_year(cell(ctrl, "Filing Date"))
        if filing_year:
            return (filing_year + config.PATENT_TERM_YEARS) - current_year

    if blocking_df is not None and not blocking_df.empty and "Type" in blocking_df.columns:
        fc = blocking_df[_is_forecasted_type(blocking_df)]
        if not fc.empty:
            for _, row in fc.iterrows():
                filing_year = extract_year(cell(row, "Filing Date"))
                if filing_year:
                    return (filing_year + config.PATENT_TERM_YEARS) - current_year
            fc_count = fc[FORECASTED_COL].apply(to_number).dropna().sum() if FORECASTED_COL in fc.columns else 0
            if fc_count > 0:
                return config.PATENT_TERM_YEARS   # forecasted but undated: assume filed this year
    return None


def _derive_est_approval(ctrl, current_year: int) -> str:
    raw = clean_year(cell(ctrl, "Est. Approval Year")) if ctrl is not None else "N/A"
    if raw != "N/A":
        return raw
    phase = cell(ctrl, "Phase").strip().lower() if ctrl is not None else ""
    if phase == "phase 3":
        return str(current_year + 3)
    if phase == "phase 2":
        return str(current_year + 5)
    if phase in ("pre-registration", "preregistration"):
        return str(current_year + 1)
    if phase == "marketed":
        return str(current_year)
    return "N/A"


# ─────────────────────────────────────────────
# Rationale
# ─────────────────────────────────────────────

def generate_rationale(d: Dict) -> str:
    """Plain-English rationale for the Average Years to Entry and LOE Score.
    Always non-empty, even when Years to Entry / score are N/A (e.g. every
    controlling patent is forecasted)."""
    si = to_number(d["loe_score"])
    band = SCORE_LABELS.get(int(si), "N/A") if si else "N/A"

    parts = []
    if si:
        parts.append(
            f"LOE Score {d['loe_score']}/5 ({band}): "
            f"avg YTE = ({d['us_yte']} + {d['ep_yte']}) / 2 = {d['avg_yte_us_ep']} yrs"
        )
    else:
        parts.append(
            "LOE Score not yet calculable (Years to Entry unavailable — "
            "controlling patents are forecasted and not yet filed)"
        )

    ctrl_parts = []
    for jur, key in (("US", "us"), ("EP", "ep")):
        if d[f"{key}_ctrl_pn_display"] == "N/A":
            continue
        text = f"{jur} controlling patent: {d[f'{key}_ctrl_pn_display']}"
        if d[f"{key}_ctrl_expiry"] != "N/A":
            text += f" (filed {d[f'{key}_ctrl_filed']}, expiry {d[f'{key}_ctrl_expiry']})"
        elif d[f"{key}_ctrl_filed"] != "N/A":
            text += f" (filed {d[f'{key}_ctrl_filed']})"
        if d[f"{key}_yte"] != "N/A":
            text += f", YTE {d[f'{key}_yte']}"
        ctrl_parts.append(text)
    if ctrl_parts:
        parts.append("; ".join(ctrl_parts))

    fc_note = ""
    if d["n_us_forecasted"] > 0 or d["n_ep_forecasted"] > 0:
        fc_note = (f" (including {d['n_us_forecasted']} US "
                   f"+ {d['n_ep_forecasted']} EP forecasted patents not yet filed)")
    parts.append(
        f"Portfolio: {d['total_us']} US ({d['n_us_blocking']} blocking) "
        f"and {d['total_ep']} EP ({d['n_ep_blocking']} blocking) patents{fc_note}"
    )
    return ". ".join(parts) + "."


# ─────────────────────────────────────────────
# Per-drug data
# ─────────────────────────────────────────────

def extract_drug_data(df: pd.DataFrame) -> Dict:
    """Everything the rationale and the report need for ONE drug's US/EP rows."""
    df = normalize_forecasted_col(df.copy())
    jurisdiction = df["Jurisdiction"].astype(str).str.upper()
    us, ep = df[jurisdiction == "US"], df[jurisdiction == "EP"]
    us_b, ep_b = us[us["Tag"] == "BLOCKING"], ep[ep["Tag"] == "BLOCKING"]
    us_n, ep_n = us[us["Tag"] == "NON-BLOCKING"], ep[ep["Tag"] == "NON-BLOCKING"]
    logger.debug("[RATIONALE] rows=%d | US %d (B %d / NB %d) | EP %d (B %d / NB %d)",
                 len(df), len(us), len(us_b), len(us_n), len(ep), len(ep_b), len(ep_n))

    us_ctrl, ep_ctrl = find_controlling(us_b), find_controlling(ep_b)
    current_year = datetime.now(timezone.utc).year

    us_yte = _derive_years_to_entry(us_ctrl, us_b, current_year)
    ep_yte = _derive_years_to_entry(ep_ctrl, ep_b, current_year)
    vals = [v for v in (us_yte, ep_yte) if v is not None]
    avg = round(sum(vals) / len(vals), 1) if vals else None

    def ctrl(c, col, default="N/A"):
        return cell(c, col) if c is not None else default

    n_us_existing, n_ep_existing = _existing_count(us), _existing_count(ep)
    n_us_fc, n_ep_fc = _forecasted_count(us), _forecasted_count(ep)
    n_us_b_existing, n_ep_b_existing = _existing_count(us_b), _existing_count(ep_b)
    n_us_b_fc, n_ep_b_fc = _forecasted_count(us_b), _forecasted_count(ep_b)

    us_ctrl_pn, ep_ctrl_pn = ctrl(us_ctrl, "Patent Number"), ctrl(ep_ctrl, "Patent Number")
    us_ctrl_row = us_ctrl.to_dict() if us_ctrl is not None else {}
    ep_ctrl_row = ep_ctrl.to_dict() if ep_ctrl is not None else {}

    result = {
        "drug_name": cell(df.iloc[0], "Drug Name"),
        "rationale": "",
        "total_us": n_us_existing + n_us_fc, "total_ep": n_ep_existing + n_ep_fc,
        "n_us_blocking": n_us_b_existing + n_us_b_fc, "n_ep_blocking": n_ep_b_existing + n_ep_b_fc,
        "n_us_non": _existing_count(us_n) + _forecasted_count(us_n),
        "n_ep_non": _existing_count(ep_n) + _forecasted_count(ep_n),
        "n_us_existing": n_us_existing, "n_ep_existing": n_ep_existing,
        "n_us_forecasted": n_us_fc, "n_ep_forecasted": n_ep_fc,
        "n_us_b_existing": n_us_b_existing, "n_ep_b_existing": n_ep_b_existing,
        "n_us_b_forecasted": n_us_b_fc, "n_ep_b_forecasted": n_ep_b_fc,
        "us_yte": fmt_number(us_yte), "ep_yte": fmt_number(ep_yte),
        "avg_yte_us_ep": fmt_number(avg), "loe_score": fmt_number(loe_score(avg)),
        "us_ctrl_pn":            us_ctrl_pn,
        "us_ctrl_pn_display":    forecasted_label(us_ctrl_pn),
        "us_ctrl_cat":           ctrl(us_ctrl, "Step 1 Claim Category"),
        "us_ctrl_filed":         clean_year(ctrl(us_ctrl, "Filing Date")),
        "us_ctrl_grant":         ctrl(us_ctrl, "Grant Date"),
        "us_ctrl_pte":           ctrl(us_ctrl, "PTE (months)"),
        "us_ctrl_ped":           ctrl(us_ctrl, "Pediatric Exclusivity", "No"),
        "us_ctrl_expiry":        clean_year(ctrl(us_ctrl, EXPIRY_COL)),
        "us_excl_yr":            clean_year(ctrl(us_ctrl, "Exclusivity Year")),
        "us_ctrl_yte":           fmt_number(to_number(ctrl(us_ctrl, "Years to Entry"))),
        "us_approval":           ctrl(us_ctrl, "Approval Date"),
        "us_est_approval":       _derive_est_approval(us_ctrl, current_year),
        "us_phase":              ctrl(us_ctrl, "Phase"),
        "us_ctrl_is_forecasted": is_forecasted(us_ctrl_row) if us_ctrl is not None else False,
        "ep_ctrl_pn":            ep_ctrl_pn,
        "ep_ctrl_pn_display":    forecasted_label(ep_ctrl_pn),
        "ep_ctrl_cat":           ctrl(ep_ctrl, "Step 1 Claim Category"),
        "ep_ctrl_filed":         clean_year(ctrl(ep_ctrl, "Filing Date")),
        "ep_ctrl_grant":         ctrl(ep_ctrl, "Grant Date"),
        "ep_ctrl_pte":           ctrl(ep_ctrl, "PTE (months)"),
        "ep_ctrl_expiry":        clean_year(ctrl(ep_ctrl, EXPIRY_COL)),
        "ep_excl_yr":            clean_year(ctrl(ep_ctrl, "Exclusivity Year")),
        "ep_ctrl_yte":           fmt_number(to_number(ctrl(ep_ctrl, "Years to Entry"))),
        "ep_approval":           ctrl(ep_ctrl, "Approval Date"),
        "ep_est_approval":       _derive_est_approval(ep_ctrl, current_year),
        "ep_phase":              ctrl(ep_ctrl, "Phase"),
        "ep_ctrl_is_forecasted": is_forecasted(ep_ctrl_row) if ep_ctrl is not None else False,
        "us_blocking_list": us_b.to_dict("records"), "ep_blocking_list": ep_b.to_dict("records"),
        "us_non_list": us_n.to_dict("records"), "ep_non_list": ep_n.to_dict("records"),
    }
    result["rationale"] = generate_rationale(result)
    return result


def us_ep_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Only the US / EP rows — the jurisdictions the LOE score is based on."""
    return df[df["Jurisdiction"].astype(str).str.upper().isin(["US", "EP"])]


def rationale_for_drug(df: pd.DataFrame) -> Optional[str]:
    """The rationale for one drug's rows of the merged table, or None when it
    has no US / EP rows."""
    rows = us_ep_rows(df)
    if rows.empty:
        return None
    return extract_drug_data(rows)["rationale"]
