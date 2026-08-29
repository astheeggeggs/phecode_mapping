#!/usr/bin/env python3
"""Reconcile the attrition curve's full-cohort point against the run it came from.

At a sample size equal to the whole cohort, the curve must reproduce the run's own
`phenotype_matrix.n_columns`. If it does not, the curve is wrong at every other point
too, and this says which phecodes differ and why rather than leaving it to inference.

Prints counts and phecode identifiers only -- no person_id, no per-person rows.

Usage:
    python reconcile_attrition.py --run phecodex_run --release release --cohort cohort.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--cohort", required=True, type=Path)
    parser.add_argument("--min-cases", type=int, default=200)
    parser.add_argument("--min-controls", type=int, default=200)
    args = parser.parse_args()

    con = duckdb.connect()
    audit = json.loads((args.run / "audit.json").read_text())
    reader = "read_parquet" if args.cohort.suffix.lower() == ".parquet" else "read_csv_auto"
    con.execute(f"CREATE VIEW cohort AS SELECT * FROM {reader}('{args.cohort}')")
    con.execute(f"CREATE VIEW counts AS SELECT * FROM read_parquet('{args.run / 'phecode_counts.parquet'}')")
    con.execute(f"CREATE VIEW cases AS SELECT * FROM read_parquet('{args.run / 'person_phecodes.parquet'}')")
    con.execute(f"CREATE VIEW info AS SELECT * FROM read_parquet('{args.release / 'phecode_info.parquet'}')")

    n_all, n_male, n_female = con.execute("""
      SELECT count(*), count(*) FILTER (WHERE upper(trim(sex))='MALE'),
             count(*) FILTER (WHERE upper(trim(sex))='FEMALE') FROM cohort""").fetchone()
    print(f"cohort: {n_all:,}  ({n_male:,} male, {n_female:,} female, "
          f"{n_all - n_male - n_female:,} unknown)")
    print(f"run reports phenotype_matrix.n_columns = "
          f"{audit['phenotype_matrix']['n_columns']:,}\n")

    # A: what the run itself retained, from its own counts table.
    a = con.execute("SELECT count(*) FROM counts WHERE retained").fetchone()[0]

    # B: reapply the thresholds to the run's own counts. Should equal A exactly.
    b = con.execute("SELECT count(*) FROM counts WHERE case_count >= ? "
                    "AND control_count_after_exclusions >= ?",
                    [args.min_cases, args.min_controls]).fetchone()[0]

    # C: what the attrition curve computes -- cases from person_phecodes, controls as
    # (evaluable for the phecode's sex) minus cases. This is the curve's whole model.
    # Inlined rather than bound: DuckDB will not prepare parameters inside CREATE VIEW.
    con.execute(f"""
      CREATE VIEW curve AS
      SELECT c.phecode, count(DISTINCT c.person_id) AS case_count,
             CASE upper(trim(coalesce(i.sex, 'Both')))
               WHEN 'MALE' THEN {int(n_male)} WHEN 'FEMALE' THEN {int(n_female)}
               ELSE {int(n_all)} END AS evaluable
      FROM cases c LEFT JOIN info i USING (phecode) GROUP BY c.phecode, i.sex
    """)
    c = con.execute("SELECT count(*) FROM curve WHERE case_count >= ? "
                    "AND evaluable - case_count >= ?",
                    [args.min_cases, args.min_controls]).fetchone()[0]

    print(f"A  run's own `retained` flag                     {a:,}")
    print(f"B  thresholds reapplied to the run's counts      {b:,}")
    print(f"C  the attrition curve's model                   {c:,}")
    if a == b == c:
        print("\nall three agree -- the curve is faithful at full cohort size")
        return

    print(f"\nA vs B differ by {b - a:+,}" if a != b else "\nA and B agree")
    print(f"B vs C differ by {c - b:+,}" if b != c else "B and C agree")

    extra = con.execute("""
      SELECT v.phecode, v.case_count, v.evaluable, v.evaluable - v.case_count AS curve_controls,
             k.case_count AS run_cases, k.control_count_after_exclusions AS run_controls,
             coalesce(i.sex, 'Both') AS restrict_sex
      FROM curve v LEFT JOIN counts k USING (phecode) LEFT JOIN info i USING (phecode)
      WHERE v.case_count >= ? AND v.evaluable - v.case_count >= ?
        AND coalesce(k.retained, false) = false
      ORDER BY v.case_count DESC LIMIT 15
    """, [args.min_cases, args.min_controls]).fetchall()
    if extra:
        print(f"\nphecodes the CURVE keeps but the RUN dropped ({len(extra)} shown):")
        print(f"  {'phecode':14s} {'sex':7s} {'cases':>8s} {'curve ctl':>10s} "
              f"{'run cases':>10s} {'run ctl':>10s}")
        for row in extra:
            run_c = "absent" if row[4] is None else f"{row[4]:,}"
            run_ct = "absent" if row[5] is None else f"{row[5]:,}"
            print(f"  {row[0]:14s} {row[6]:7s} {row[1]:>8,} {row[3]:>10,} "
                  f"{run_c:>10s} {run_ct:>10s}")
        print("\n  'absent' under run cases means the phecode is not in phecode_counts at all,")
        print("  i.e. the run excluded it outright (--exclude-phenotypes) while person_phecodes")
        print("  still carries its cases. That is the likeliest cause of an inflated curve.")

    missing = con.execute("""
      SELECT count(*) FROM counts k WHERE k.retained
        AND NOT EXISTS (SELECT 1 FROM curve v WHERE v.phecode = k.phecode
                        AND v.case_count >= ? AND v.evaluable - v.case_count >= ?)
    """, [args.min_cases, args.min_controls]).fetchone()[0]
    if missing:
        print(f"\nphecodes the RUN kept but the curve drops: {missing:,}")


if __name__ == "__main__":
    main()
