"""
display.py
──────────
Console display helpers.
"""

import pandas as pd


# ─────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────

def print_step1(df: pd.DataFrame) -> None:
    if df.empty:
        print("No data to display.")
        return

    col_widths = {}
    for col in df.columns:
        max_content = df[col].astype(str).apply(
            lambda x: max((len(line) for line in x.split("\n")), default=0)
        ).max()
        col_widths[col] = max(max_content, len(col)) + 2

    sep    = "─" * sum(col_widths.values())
    header = "".join(f"{col:<{col_widths[col]}}" for col in df.columns)

    print(f"\n{sep}")
    print(header)
    print(sep)

    for _, row in df.iterrows():
        lines_per_col = {col: str(row[col]).split("\n") for col in df.columns}
        max_lines     = max(len(v) for v in lines_per_col.values())
        for line_idx in range(max_lines):
            line_out = ""
            for col in df.columns:
                lines    = lines_per_col[col]
                cell     = lines[line_idx] if line_idx < len(lines) else ""
                line_out += f"{cell:<{col_widths[col]}}"
            print(line_out)
        print()

    print(sep)
    print(f"  {len(df)} drug(s)\n")


def print_forecast_s3(df: pd.DataFrame) -> None:
    """Prints the unified forecast_s3 table."""
    if df.empty:
        print("No data to display.")
        return

    for drug_name in df["Drug Name"].unique():
        drug_df = df[df["Drug Name"] == drug_name]
        print(f"\n{'═'*80}")
        print(f"  {drug_name}")
        print(f"{'═'*80}")

        for jur in ["US", "EP"]:
            jur_df = drug_df[drug_df["Jurisdiction"] == jur]
            if jur_df.empty:
                continue
            print(f"\n  [{jur}]")
            display_cols = [c for c in jur_df.columns if c not in ("Drug Name", "Jurisdiction")]
            col_widths = {
                col: max(jur_df[col].astype(str).map(len).max(), len(col)) + 2
                for col in display_cols
            }
            sep    = "  " + "─" * sum(col_widths.values())
            header = "  " + "".join(f"{col:<{col_widths[col]}}" for col in display_cols)
            print(sep)
            print(header)
            print(sep)
            for _, row in jur_df.iterrows():
                print("  " + "".join(f"{str(row[col]):<{col_widths[col]}}" for col in display_cols))
            print(sep)


def print_step3(df: pd.DataFrame) -> None:
    """Prints Step 3 filing pattern results."""
    if df.empty:
        print("No Step 3 data to display.")
        return

    for drug_name in df["Drug Name"].unique():
        drug_df = df[df["Drug Name"] == drug_name]
        print(f"\n{'═'*70}")
        print(f"  {drug_name} — Filing Timing vs. Development Milestones")
        print(f"{'═'*70}")
        if drug_df.empty:
            print("  No data.")
            continue
        print(f"  {'Patent Type':<25} {'Typical Filing Stage':<20} {'Reason'}")
        print(f"  {'─'*25} {'─'*20} {'─'*40}")
        for _, row in drug_df.iterrows():
            print(
                f"  {row['Patent Type']:<25} {row['Typical Filing Stage']:<20} "
                f"{row.get('Reason', '')}"
            )
