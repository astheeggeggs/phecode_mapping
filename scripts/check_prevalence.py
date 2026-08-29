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
# Identifiers verified against phecode_info, not recalled. Two of these were wrong on
# the first pass -- CV_400 is rheumatic heart disease, not atrial fibrillation, and
# GI_530 is disease of anus and rectum, not GORD -- and both duly flagged as "out of
# band" on a run that was in fact correct. A check that cries wolf about itself is
# worse than no check, so if you add a row here, confirm the string first:
#   SELECT phecode, phecode_string FROM read_parquet('<release>/phecode_info.parquet')
#
# Bands in the last column are wide and order-of-magnitude: they exist to catch a
# mapping broken by a factor of ten, not to validate epidemiology. The observed column
# is what a full UK Biobank run (502,617 people, hospital-coded) actually produced.
EXPECTED = {
    #  phecode        label                              lo    hi     observed in UKB
    "CV_401":     ("Hypertension",                     0.10, 0.45),   # 19.4%
    "EM_202":     ("Diabetes mellitus (all types)",    0.02, 0.15),   #  5.2%
    "RE_475":     ("Asthma",                           0.03, 0.20),   #  6.5%
    "CV_404":     ("Ischemic heart disease",           0.02, 0.20),   #  6.7%
    "CV_416":     ("Cardiac arrhythmia",               0.01, 0.15),   #  5.2%
    "CV_416.21":  ("Atrial fibrillation",              0.005, 0.10),
    "MS_708":     ("Osteoarthritis",                   0.03, 0.30),   #  9.4%
    "GI_511":     ("GORD / GERD",                      0.01, 0.25),   #  5.6%
    "MB_286":     ("Mood [affective] disorders",       0.01, 0.15),   #  3.1%
    "EM_239":     ("Hyperlipidemia",                   0.02, 0.30),   #  8.9%
    "SO_371":     ("Cataract",                         0.01, 0.20),   #  5.1%
}

# Sex-restricted phecodes are the strongest check available, because the expected
# answer is exactly zero rather than a band. If any of these has a single case in the
# wrong sex, the sex restriction is not being applied to real data.
# All three verified present and genuinely restricted in the 1.1 release. CA_111 was in
# an earlier draft and is not a phecode at all, so it silently skipped -- a sex check
# that never runs is exactly the vacuous guard this project keeps finding.
SEX_CHECKS = {"GU_608": "Male", "GU_602": "Male", "PP_901": "Female", "CA_144": "Female"}


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
