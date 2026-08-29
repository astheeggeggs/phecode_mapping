#!/usr/bin/env python3
"""Prevalence plausibility check. Run INSIDE the secure environment, on a real cohort.

This is the one class of error the test suite cannot reach. Every check in the repo is
internal consistency -- do the numbers reconcile, do two runs agree, does the guard
fire. None of them can tell you whether hypertension comes out at 25% or at 2.5%.
The de-identified fixture cannot answer it either: its codes are shuffled across people
and its dates are synthetic, so every per-person quantity computed from it is
meaningless by construction.

It emits ONLY aggregate counts and rates -- no person_id, no per-person rows. Check the
output against your own data-sharing agreement before moving it anywhere.

Usage:
    python prevalence_check.py --run phecodex_run --release release --out prevalence.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

# Phecodes whose approximate hospital-coded prevalence in a UK Biobank-like cohort is
# well enough characterised to be worth eyeballing. The bands are deliberately WIDE and
# order-of-magnitude only: they are here to catch a mapping that is broken by a factor
# of ten, not to validate epidemiology. Narrow them against your own published
# estimates before treating a near-miss as a finding.
EXPECTED = {
    "CV_401":     ("Hypertension",              0.10, 0.45),
    "EM_202":     ("Type 2 diabetes",           0.02, 0.15),
    "RE_475":     ("Asthma",                    0.03, 0.20),
    "CV_404":     ("Ischemic heart disease",    0.02, 0.20),
    "CV_400":     ("Atrial fibrillation",       0.01, 0.12),
    "MS_708":     ("Osteoarthritis",            0.03, 0.30),
    "GI_530":     ("GORD / oesophagitis",       0.02, 0.25),
    "MB_286":     ("Mood disorders",            0.01, 0.15),
}

# Sex-restricted phecodes are the strongest check available, because the expected
# answer is exactly zero rather than a band. If any of these has a single case in the
# wrong sex, the sex restriction is not being applied to real data.
SEX_CHECKS = {"GU_608": "Male", "CA_111": "Female", "PP_901": "Female"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path, help="A phecodex-map run directory.")
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--cohort", required=True, type=Path,
                         help="The same cohort file the run used.")
    parser.add_argument("--out", required=True, type=Path, help="Aggregate CSV to write.")
    parser.add_argument("--top", type=int, default=40, help="How many commonest phecodes to list.")
    args = parser.parse_args()

    con = duckdb.connect()
    con.execute(f"CREATE VIEW counts AS SELECT * FROM read_parquet('{args.run / 'phecode_counts.parquet'}')")
    con.execute(f"CREATE VIEW info AS SELECT * FROM read_parquet('{args.release / 'phecode_info.parquet'}')")
    con.execute(f"CREATE VIEW matrix AS SELECT * FROM read_parquet('{args.run / 'phenotype_matrix.parquet'}')")

    n_people = con.execute("SELECT count(*) FROM matrix").fetchone()[0]
    print(f"cohort: {n_people:,} people\n")

    # Prevalence among the EVALUABLE denominator, not the whole cohort -- a sex-restricted
    # phecode scored against everyone would look half as common as it is.
    con.execute("""
      CREATE VIEW prev AS
      SELECT c.phecode, any_value(i.phecode_string) AS label, any_value(i.sex) AS restrict_sex,
             c.case_count,
             c.case_count + c.control_count_after_exclusions AS evaluable,
             c.case_count::DOUBLE / nullif(c.case_count + c.control_count_after_exclusions, 0) AS prevalence
      FROM counts c LEFT JOIN info i USING (phecode)
      GROUP BY c.phecode, c.case_count, c.control_count_after_exclusions
    """)

    print("=== Named phecodes against expected bands ===")
    problems = []
    for phecode, (label, lo, hi) in EXPECTED.items():
        row = con.execute("SELECT case_count, evaluable, prevalence FROM prev WHERE phecode = ?",
                          [phecode]).fetchone()
        if not row or row[1] in (None, 0):
            print(f"  {phecode:10s} {label:26s} ABSENT from this run")
            problems.append(f"{phecode} absent")
            continue
        cases, evaluable, rate = row
        flag = "" if lo <= rate <= hi else "   <-- OUTSIDE BAND"
        print(f"  {phecode:10s} {label:26s} {rate:7.2%}  ({cases:,} / {evaluable:,}) "
              f"expected {lo:.0%}-{hi:.0%}{flag}")
        if not lo <= rate <= hi:
            problems.append(f"{phecode} at {rate:.2%}, expected {lo:.0%}-{hi:.0%}")

    print("\n=== Sex restriction on real data (the sharpest check: expected answer is exact) ===")
    con.execute(f"CREATE VIEW cohort AS SELECT * FROM read_csv_auto('{args.cohort}')")
    matrix_columns = {r[0] for r in con.execute("DESCRIBE matrix").fetchall()}
    for phecode, expected_sex in SEX_CHECKS.items():
        if phecode not in matrix_columns:
            print(f"  {phecode:10s} not retained in this run")
            continue
        wrong = con.execute(f"""
          SELECT count(*) FROM matrix m JOIN cohort c USING (person_id)
          WHERE m."{phecode}" IS NOT NULL AND upper(trim(c.sex)) <> upper(?)
        """, [expected_sex]).fetchone()[0]
        scored = con.execute(
            f'SELECT count(*) FROM matrix WHERE "{phecode}" IS NOT NULL').fetchone()[0]
        verdict = "OK" if wrong == 0 else f"*** {wrong:,} WRONG-SEX PEOPLE SCORED ***"
        print(f"  {phecode:10s} {expected_sex:6s}-only: {scored:,} scored, {wrong:,} of the "
              f"other sex   {verdict}")
        if wrong:
            problems.append(f"{phecode} scored {wrong} people of the wrong sex")

    print(f"\n=== {args.top} commonest phecodes (eyeball for anything absurd) ===")
    for phecode, label, rate, cases, evaluable in con.execute(
            "SELECT phecode, label, prevalence, case_count, evaluable FROM prev "
            "WHERE evaluable > 0 ORDER BY prevalence DESC LIMIT ?", [args.top]).fetchall():
        print(f"  {phecode:12s} {rate:7.2%}  {str(label)[:44]}")

    con.execute(f"""COPY (SELECT phecode, label, restrict_sex, case_count, evaluable, prevalence
                          FROM prev ORDER BY prevalence DESC)
                    TO '{args.out}' (HEADER, DELIMITER ',')""")
    print(f"\nwrote aggregate prevalences to {args.out}")

    if problems:
        print("\nOUT OF BAND:")
        for p in problems:
            print(f"  - {p}")
        print("\nA single near-miss is probably the band being too narrow for your cohort.")
        print("Several at once, or anything off by a factor of ten, is a mapping problem.")


if __name__ == "__main__":
    main()
