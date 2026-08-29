#!/usr/bin/env python3
"""Reconcile the attrition curve's full-cohort point against the run it came from.

At a sample size equal to the whole cohort, the curve must reproduce the run's own
`phenotype_matrix.n_columns`. If it does not, the curve is wrong at every other point
too, and this says which phecodes differ and BY HOW MUCH, attributing the gap to the
run's own columns rather than leaving it to inference.

The curve's control model is `evaluable people - cases`
(retention.controls_from_evaluable). A run also REMOVES people from the control pool:
sub-threshold carriers under `--case-rule two-dates`, and non-cases named by
`--control-exclusions`. Those removals are person-level and live only in the run, so
the curve cannot see them. The whole discrepancy is therefore
`control_count_before_exclusions - control_count_after_exclusions`, which
phecode_counts already carries -- this script reports it rather than guessing.

Prints counts and phecode identifiers only -- no person_id, no per-person rows.

Usage:
    python reconcile_attrition.py --run phecodex_run --release release --cohort cohort.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from phecodex_mapper.io import checksum, connect, relation_for
from phecodex_mapper.retention import eligible_count_sql


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--cohort", required=True, type=Path)
    parser.add_argument("--min-cases", type=int, default=200)
    parser.add_argument("--min-controls", type=int, default=200)
    args = parser.parse_args()

    # connect()/relation_for(), not duckdb.connect() and a suffix ternary: this reads a
    # run the same way the run was written, and accepts a Parquet cohort because the
    # mapper does.
    con = connect()
    audit = json.loads((args.run / "audit.json").read_text())
    con.execute(f"CREATE VIEW cohort AS SELECT * FROM {relation_for(args.cohort)}")
    con.execute(f"CREATE VIEW counts AS SELECT * FROM {relation_for(args.run / 'phecode_counts.parquet')}")
    con.execute(f"CREATE VIEW cases AS SELECT * FROM {relation_for(args.run / 'person_phecodes.parquet')}")
    con.execute(f"CREATE VIEW info AS SELECT * FROM {relation_for(args.release / 'phecode_info.parquet')}")

    n_all, n_male, n_female = con.execute("""
      SELECT count(*), count(*) FILTER (WHERE upper(trim(sex))='MALE'),
             count(*) FILTER (WHERE upper(trim(sex))='FEMALE') FROM cohort""").fetchone()

    # Everything below reconciles the run against THIS release and THIS cohort, so if
    # either is the wrong one every number is meaningless -- and the failure looks
    # exactly like a genuine model discrepancy, which is how a valid single run
    # reconciled against a sibling cohort came to be reported as an assembled-from-two-
    # runs directory. Check identity first, and refuse: the run itself records both.
    recorded_manifest = audit.get("release_manifest_sha256")
    if recorded_manifest and recorded_manifest != checksum(args.release / "manifest.json"):
        raise SystemExit(
            f"--release {args.release} is not the release this run was produced against: "
            f"its manifest.json hashes to {checksum(args.release / 'manifest.json')[:12]}, "
            f"but audit.json records {recorded_manifest[:12]}. The sex restrictions below "
            f"would come from the wrong release. Point --release at "
            f"{audit.get('release', 'the release named in audit.json')}.")
    recorded_sex = audit.get("sex") or {}
    if "n_male" in recorded_sex:
        recorded = (recorded_sex["n_male"] + recorded_sex["n_female"]
                    + recorded_sex["n_unknown_sex"], recorded_sex["n_male"], recorded_sex["n_female"])
        if recorded != (n_all, n_male, n_female):
            raise SystemExit(
                f"--cohort {args.cohort} is not the cohort this run was produced against: it has "
                f"{n_all:,} people ({n_male:,} male, {n_female:,} female) but the run's audit.json "
                f"records {recorded[0]:,} ({recorded[1]:,} male, {recorded[2]:,} female). Every "
                f"denominator below would be wrong, and the mismatch would surface as a curve/run "
                f"disagreement that has nothing to do with the curve.")

    print(f"cohort: {n_all:,}  ({n_male:,} male, {n_female:,} female, "
          f"{n_all - n_male - n_female:,} unknown)")
    print(f"run reports phenotype_matrix.n_columns = "
          f"{audit['phenotype_matrix']['n_columns']:,}\n")

    # A: what the run itself retained, from its own counts table.
    a = con.execute("SELECT count(*) FROM counts WHERE retained").fetchone()[0]

    # B: reapply the thresholds to the run's own counts. `retained` is set by exactly
    # this expression, so A != B means the run directory was edited or assembled from
    # more than one run -- it is a tamper check, not a model check.
    b = con.execute("SELECT count(*) FROM counts WHERE case_count >= ? "
                    "AND control_count_after_exclusions >= ?",
                    [args.min_cases, args.min_controls]).fetchone()[0]

    # C: what the attrition curve computes -- cases from person_phecodes, controls as
    # (evaluable for the phecode's sex) minus cases. Same denominator rule as the
    # mapper (retention.eligible_count_sql), so any B/C gap is the control removals
    # the curve cannot see, never a difference of opinion about sex.
    # Inlined rather than bound: DuckDB will not prepare parameters inside CREATE VIEW.
    evaluable_sql = eligible_count_sql("upper(trim(coalesce(i.sex, 'Both')))",
                                       n_male=str(int(n_male)), n_female=str(int(n_female)),
                                       n_all=str(int(n_all)))
    con.execute(f"""
      CREATE VIEW curve AS
      SELECT c.phecode, count(DISTINCT c.person_id) AS case_count,
             {evaluable_sql} AS evaluable
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

    # Every divergent phecode, not a display page of them. The verdict below used to be
    # computed from a LIMIT 15 result, so a run whose one anomalous phecode ranked 16th by
    # case count printed the reassuring narrative and no warning at all -- the check was a
    # function of how many OTHER phecodes happened to diverge. Classify all of them; show
    # the largest few.
    extra = con.execute("""
      SELECT v.phecode, v.case_count, v.evaluable - v.case_count AS curve_controls,
             k.control_count_after_exclusions AS run_controls,
             k.control_count_before_exclusions - k.control_count_after_exclusions AS removed,
             k.subthreshold_control_count, k.excluded_control_count,
             coalesce(i.sex, 'Both') AS restrict_sex
      FROM curve v LEFT JOIN counts k USING (phecode) LEFT JOIN info i USING (phecode)
      WHERE v.case_count >= ? AND v.evaluable - v.case_count >= ?
        AND coalesce(k.retained, false) = false
      ORDER BY v.case_count DESC
    """, [args.min_cases, args.min_controls]).fetchall()
    if extra:
        shown = extra[:15]
        suffix = f", {len(shown)} shown" if len(shown) < len(extra) else ""
        print(f"\nphecodes the CURVE keeps but the RUN dropped ({len(extra)}{suffix}):")
        print(f"  {'phecode':14s} {'sex':7s} {'cases':>8s} {'curve ctl':>10s} "
              f"{'run ctl':>10s} {'removed':>8s} {'sub':>6s} {'cxcl':>6s}")
        for phecode, cases_n, curve_ctl, run_ctl, removed, sub, cxcl, sex in shown:
            if run_ctl is None:
                print(f"  {phecode:14s} {sex:7s} {cases_n:>8,} {curve_ctl:>10,} "
                      f"{'absent':>10s} {'-':>8s} {'-':>6s} {'-':>6s}")
                continue
            print(f"  {phecode:14s} {sex:7s} {cases_n:>8,} {curve_ctl:>10,} "
                  f"{run_ctl:>10,} {removed:>+8,} {sub:>6,} {cxcl:>6,}")
        # Two distinct failures, classified over the WHOLE set above:
        #   absent   -- no phecode_counts row at all for a phecode person_phecodes has cases for
        #   mismatch -- there is a row, but the gap is not the removals it reports
        absent = [r[0] for r in extra if r[3] is None]
        mismatch = [r[0] for r in extra if r[3] is not None and r[2] - r[3] != r[4]]

        print("\n  'removed' is control_count_before_exclusions - control_count_after_exclusions,")
        print("  and it is exactly what the curve cannot see: 'sub' are sub-threshold carriers")
        print("  under --case-rule two-dates, 'cxcl' are non-cases named by --control-exclusions.")
        # 'removed' counts each person once (mapper's noncase_excluded is a UNION), while
        # sub and cxcl are counted independently, so someone who is both a sub-threshold
        # carrier AND named by a control exclusion appears in both columns. sub + cxcl is
        # therefore an upper bound on 'removed', not a decomposition of it -- presenting
        # them as components makes a correct run look like it has lost 15 people twice.
        print("  Those two columns are counted independently and can overlap (one person may be")
        print("  both), so sub + cxcl >= removed; 'removed' is the de-duplicated total.")
        print("  Where 'removed' accounts for the curve/run gap, the curve is behaving as")
        print("  documented and the run is right -- read the curve as an upper bound.")

        if mismatch:
            # The release and the cohort were already checked against audit.json above, so
            # this is not a wrong-input diagnosis. What is left is a phecode_counts row that
            # does not describe the same population person_phecodes does.
            print(f"\n  gap NOT accounted for by the removals ({len(mismatch)}): {mismatch[:15]}")
            print("  curve controls - run controls should equal 'removed' exactly. It does not")
            print("  here, and --release/--cohort already match what audit.json records, so the")
            print("  phecode_counts row and the person_phecodes rows describe different")
            print("  populations. Re-run map_phecodes rather than trusting either number.")
        if absent:
            # This cannot happen in a run map_phecodes produced: `cases` (-> person_phecodes)
            # and `all_phecodes` (-> phecode_counts) are built from the same not_excluded
            # predicate, so phecode_counts is always a superset. Seeing it means the
            # directory was assembled from more than one run.
            print(f"\n  absent from phecode_counts ({len(absent)}): {absent[:15]}")
            print("  A phecode absent from phecode_counts while person_phecodes carries its")
            print("  cases is not something map_phecodes can produce -- both come from the same")
            print("  tables in one call. Check that this run directory is from a single run.")

    missing = con.execute("""
      SELECT count(*) FROM counts k WHERE k.retained
        AND NOT EXISTS (SELECT 1 FROM curve v WHERE v.phecode = k.phecode
                        AND v.case_count >= ? AND v.evaluable - v.case_count >= ?)
    """, [args.min_cases, args.min_controls]).fetchone()[0]
    if missing:
        print(f"\nphecodes the RUN kept but the curve drops: {missing:,}")


if __name__ == "__main__":
    main()
