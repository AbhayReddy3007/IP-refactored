"""
phase_processing.py
───────────────────
Clinical phase processing, per-drug timelines, phase at filing.
"""

from typing import Dict, List
import pandas as pd
from .phase_utils import _fetch_approval_for_molecules, _normalise_phase, _parse_date_or_year


# ─────────────────────────────────────────────
# Phase processing (merged from phase_processor.py)
# ─────────────────────────────────────────────

async def _process_phase_input(clinical_df: pd.DataFrame) -> pd.DataFrame:
    """
    Processes phase data from clinical_efficacy BigQuery table.

    Steps:
      1. Normalise phases (3a→3, III→3)
      2. Keep earliest date per molecule per phase
      3. Drop Phase 4 (handled via Marketed)
      4. Use real Phase 1 / Phase 2 trial_start_date when present;
         only derive missing ones from Phase 3
         (Phase 2 = P3 − 3 yrs, Phase 1 = P3 − 4 yrs)
      5. Fetch approval dates for marketed drugs only
      6. Filter to GCS drugs only

    Expects columns: Molecule, Phase, Start Date
    """
    from dateutil.relativedelta import relativedelta

    if clinical_df.empty:
        print("[PHASE] No clinical data to process.")
        return pd.DataFrame()

    df = clinical_df.copy()
    mol_col   = "Molecule"
    phase_col = "Phase"
    date_col  = "Start Date"

    print(f"[PHASE] Processing {len(df)} rows from clinical_efficacy")

    # Normalise phases
    df["Normalised Phase"] = df[phase_col].apply(_normalise_phase)
    df = df.dropna(subset=["Normalised Phase"])

    # Parse dates/years
    df["_parsed_date"] = df[date_col].apply(_parse_date_or_year)

    # Earliest date per molecule per phase
    df = df.sort_values("_parsed_date", na_position="last")
    result = df.groupby([mol_col, "Normalised Phase"], sort=False).first().reset_index()
    result = result.drop(columns=["_parsed_date"])

    # Drop Phase 4 (handled separately via Marketed). Keep Phase 1 and Phase 2
    # rows if clinical_efficacy provides their own trial_start_date — those
    # are real observed values and should be preferred over derived ones.
    result = result[result["Normalised Phase"] != "Phase 4"]

    # Track which (molecule, phase) combinations already have a REAL start
    # date from clinical_efficacy. We will only synthesise Phase 1 or Phase 2
    # rows for drugs where the real value is missing.
    real_phases_by_mol: Dict[str, set] = {}
    for _, row in result.iterrows():
        m = str(row[mol_col]).strip()
        p = str(row["Normalised Phase"]).strip()
        if p in ("Phase 1", "Phase 2"):
            real_phases_by_mol.setdefault(m, set()).add(p)

    # Calculate Phase 1/2 from Phase 3 ONLY when the real value is missing
    # for that drug. Phase 2 = Phase 3 − 3 years, Phase 1 = Phase 3 − 4 years.
    phase3_rows = result[result["Normalised Phase"] == "Phase 3"].copy()
    new_rows = []
    derived_count = 0
    skipped_count = 0
    for _, row in phase3_rows.iterrows():
        p3_date = _parse_date_or_year(row[date_col])
        if pd.isna(p3_date):
            continue
        mol_name = row[mol_col]
        is_year = False
        try:
            y = int(float(str(row[date_col]).strip()))
            is_year = 1900 <= y <= 2100
        except (ValueError, TypeError):
            pass

        p2_date = p3_date - relativedelta(years=3)
        p1_date = p3_date - relativedelta(years=4)

        have_real = real_phases_by_mol.get(str(mol_name).strip(), set())

        if "Phase 2" not in have_real:
            p2_row = row.copy()
            p2_row["Normalised Phase"] = "Phase 2"
            p2_row[date_col] = p2_date.year if is_year else p2_date
            p2_row[phase_col] = "Phase 2 (calculated)"
            new_rows.append(p2_row)
            derived_count += 1
        else:
            skipped_count += 1

        if "Phase 1" not in have_real:
            p1_row = row.copy()
            p1_row["Normalised Phase"] = "Phase 1"
            p1_row[date_col] = p1_date.year if is_year else p1_date
            p1_row[phase_col] = "Phase 1 (calculated)"
            new_rows.append(p1_row)
            derived_count += 1
        else:
            skipped_count += 1

        if is_year:
            print(
                f"  [PHASE] {mol_name}: P3={p3_date.year} "
                f"→ P2={p2_date.year}{' (real exists, skipped)' if 'Phase 2' in have_real else ''} "
                f"→ P1={p1_date.year}{' (real exists, skipped)' if 'Phase 1' in have_real else ''}"
            )
        else:
            print(
                f"  [PHASE] {mol_name}: P3={p3_date.strftime('%d-%m-%Y')} "
                f"→ P2={p2_date.strftime('%d-%m-%Y')}{' (real exists, skipped)' if 'Phase 2' in have_real else ''} "
                f"→ P1={p1_date.strftime('%d-%m-%Y')}{' (real exists, skipped)' if 'Phase 1' in have_real else ''}"
            )

    if new_rows:
        result = pd.concat([result, pd.DataFrame(new_rows)], ignore_index=True)
        print(
            f"[PHASE] Added {derived_count} calculated Phase 1/2 row(s); "
            f"kept {skipped_count} real Phase 1/2 date(s) from clinical_efficacy"
        )
    elif skipped_count:
        print(
            f"[PHASE] Used {skipped_count} real Phase 1/2 date(s) from clinical_efficacy "
            f"— no derivation needed"
        )

    # Fetch approval dates for marketed drugs only
    if True:
        all_marketed = set()
        for _, row in df.iterrows():
            if _normalise_phase(str(row.get(phase_col, ""))) == "Marketed":
                mol = str(row.get(mol_col, "")).strip()
                if mol and mol.lower() not in ("nan", "none", ""):
                    all_marketed.add(mol)
        for mol in result[result["Normalised Phase"] == "Marketed"][mol_col].unique():
            all_marketed.add(mol)

        if all_marketed:
            print(f"[PHASE] Fetching approval dates for {len(all_marketed)} marketed drug(s)...")

            approval_map = await _fetch_approval_for_molecules(sorted(all_marketed))
            result["Approval Date"] = ""

            marketed_rows = []
            for mol_name, dates in approval_map.items():
                parsed_dates = []
                for geo in ("US", "EU"):
                    raw = dates.get(geo)
                    if raw:
                        parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True)
                        if pd.notna(parsed):
                            parsed_dates.append(parsed)
                if not parsed_dates:
                    continue
                earliest = min(parsed_dates)
                existing = result[(result[mol_col] == mol_name) & (result["Normalised Phase"] == "Marketed")]
                if not existing.empty:
                    result.loc[existing.index, "Approval Date"] = earliest
                else:
                    marketed_row = {col: "" for col in result.columns}
                    marketed_row[mol_col] = mol_name
                    marketed_row["Normalised Phase"] = "Marketed"
                    marketed_row[phase_col] = "Marketed"
                    marketed_row[date_col] = earliest
                    marketed_row["Approval Date"] = earliest
                    marketed_rows.append(marketed_row)

            if marketed_rows:
                result = pd.concat([result, pd.DataFrame(marketed_rows)], ignore_index=True)
        else:
            result["Approval Date"] = ""
    else:
        result["Approval Date"] = ""

    # Sort
    _phase_order = {"Preclinical": 0, "Phase 1": 1, "Phase 2": 2, "Phase 3": 3, "Pre-registration": 4, "Marketed": 5}
    result["_sort"] = result["Normalised Phase"].map(_phase_order).fillna(99)
    result = result.sort_values([mol_col, "_sort"]).drop(columns=["_sort"]).reset_index(drop=True)

    print(f"[PHASE] Result: {len(result)} rows ({result[mol_col].nunique()} drugs)")
    return result


def _build_phase_timelines(phase_df: pd.DataFrame) -> Dict[str, List]:
    """Builds per-drug timelines from the processed phase DataFrame."""
    timelines: Dict[str, List] = {}

    if phase_df.empty:
        return timelines

    mol_col  = "Molecule"
    date_col = "Start Date"
    approval_col = "Approval Date" if "Approval Date" in phase_df.columns else None

    _VALID_PHASES = {"Phase 1", "Phase 2", "Phase 3", "After Submission", "Marketed"}

    for _, row in phase_df.iterrows():
        mol   = str(row.get(mol_col, "")).strip()
        phase = str(row.get("Normalised Phase", "")).strip()
        raw_date = row.get(date_col)

        if not mol or mol.lower() in ("nan", "none", ""):
            continue
        if phase not in _VALID_PHASES:
            continue

        if phase == "Marketed" and approval_col:
            approval_raw = row.get(approval_col)
            parsed = pd.to_datetime(approval_raw, errors="coerce", dayfirst=True)
            if pd.notna(parsed):
                raw_date = parsed

        parsed = _parse_date_or_year(raw_date)
        if pd.isna(parsed):
            continue

        key = mol.lower()
        if key not in timelines:
            timelines[key] = []
        timelines[key].append((parsed, phase))

    # Sort each timeline
    for key in timelines:
        timelines[key].sort(key=lambda x: x[0])

    print(f"[PHASE] Built timelines for {len(timelines)} drug(s)")
    return timelines


def _resolve_phase_at_filing(
    timelines: Dict[str, List],
    drug_name: str,
    filing_date: str,
) -> str:
    """
    Given a drug's phase timeline and a patent filing date, returns the
    phase the drug was in at the time of filing.
    """
    key = drug_name.strip().lower()
    timeline = timelines.get(key, [])

    if not timeline:
        return "—"

    try:
        filing_dt = pd.to_datetime(filing_date, errors="coerce")
    except Exception:
        return "—"

    if pd.isna(filing_dt):
        return "—"

    result_phase = None
    for milestone_date, phase_label in timeline:
        if milestone_date <= filing_dt:
            result_phase = phase_label
        else:
            break

    if result_phase is None:
        return "Pre-Phase 1"

    return result_phase
