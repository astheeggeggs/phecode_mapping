#!/usr/bin/env python3
"""Confirm a de-identified fixture leaks no real identifier. Run INSIDE secure compute.

Prints COUNTS ONLY -- never an identifier, never a code, never a row. The output is
safe to read aloud, paste into a ticket, or send to someone outside the environment;
the inputs are not.

It answers three questions:

  1. Does any real eid appear as a person_id in the outputs?   Must be 0.
  2. Does any real eid appear ANYWHERE in the outputs?         Must be 0.
     (a stray crosswalk column, a comment, an id pasted into another field)
  3. Did the block shuffle actually move codes between people?
     Reported as the share of output people whose exact code set matches some real
     person's. A fake ID over a preserved code combination is not de-identification,
     because a rare diagnosis pattern is itself identifying.

Usage:
    python check_deidentification.py \\
        --input ukb_phenotype_file.tab.gz \\
        --cohort cohort_deid.csv.gz \\
        --events events_deid.csv.gz
"""
from __future__ import annotations

import argparse
from pathlib import Path

from phecodex_mapper.io import connect, quote, relation_for


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path,
                         help="The RAW UKB extract the fixture was built from.")
    parser.add_argument("--cohort", required=True, type=Path, help="cohort_deid.csv.gz")
    parser.add_argument("--events", required=True, type=Path, help="events_deid.csv.gz")
    parser.add_argument("--id-column", default=None,
                         help="Override the raw extract's id column (default: eid or f.eid).")
    args = parser.parse_args()

    # connect() already sets preserve_insertion_order and pins TimeZone=UTC; this used
    # to hand-copy the first of those and drop the second.
    con = connect()

    # Kept bespoke rather than relation_for(): the raw UKB extract needs
    # sample_size=-1 so the dialect/column sniff reads the whole file, and a wide .tab
    # extract is exactly where a truncated sniff goes wrong.
    raw = f"read_csv_auto('{quote(args.input)}', all_varchar=true, sample_size=-1)"
    columns = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {raw}").fetchall()]
    id_column = args.id_column or ("eid" if "eid" in columns else "f.eid")
    if id_column not in columns:
        raise SystemExit(f"could not find an id column; saw {columns[:4]}... "
                         f"pass --id-column explicitly")

    con.execute(f'CREATE TABLE real_ids AS SELECT DISTINCT trim(CAST("{id_column}" AS VARCHAR)) '
                f"AS eid FROM {raw} WHERE \"{id_column}\" IS NOT NULL")
    con.execute(f"CREATE TABLE cohort AS SELECT * FROM {relation_for(args.cohort)}")
    con.execute(f"CREATE TABLE events AS SELECT * FROM {relation_for(args.events)}")

    n_real = con.execute("SELECT count(*) FROM real_ids").fetchone()[0]
    n_cohort = con.execute("SELECT count(*) FROM cohort").fetchone()[0]
    n_events = con.execute("SELECT count(*) FROM events").fetchone()[0]
    print(f"real people in the source extract : {n_real:,}")
    print(f"people in the de-identified cohort: {n_cohort:,}")
    print(f"events in the de-identified file  : {n_events:,}\n")

    print("1. real eid appearing as an output person_id")
    failures = 0
    for label, table in (("cohort", "cohort"), ("events", "events")):
        hits = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT trim(person_id) AS p FROM {table}) t "
            f"JOIN real_ids r ON r.eid = t.p").fetchone()[0]
        verdict = "OK" if hits == 0 else f"*** {hits:,} REAL IDS PRESENT ***"
        print(f"   {label:8s} {verdict}")
        failures += hits

    print("\n2. real eid appearing anywhere in any column")
    for label, table in (("cohort", "cohort"), ("events", "events")):
        cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
        union = " UNION ".join(f'SELECT DISTINCT trim(CAST("{c}" AS VARCHAR)) AS v FROM {table}'
                               for c in cols)
        hits = con.execute(
            f"SELECT count(*) FROM ({union}) t JOIN real_ids r ON r.eid = t.v").fetchone()[0]
        verdict = "OK" if hits == 0 else f"*** {hits:,} DISTINCT REAL IDS PRESENT ***"
        print(f"   {label:8s} {verdict}  ({len(cols)} columns scanned)")
        failures += hits

    print("\n3. code combinations broken up by the block shuffle")
    # Compare the SET of codes each output person holds against the sets real people
    # held. Some overlap is expected by chance, especially for people with one or two
    # very common codes; near-total overlap means only the label changed.
    id_col_sql = f'"{id_column}"'
    code_columns = [c for c in columns
                    if c != id_column and any(f in c for f in ("41270", "41271", "41202",
                                                               "41203", "41204", "41205",
                                                               "40006", "40013"))]
    if not code_columns:
        print("   (no recognised diagnosis columns in the source; skipped)")
    else:
        joined = " || ' ' || ".join(f'coalesce(CAST("{c}" AS VARCHAR), \'\')' for c in code_columns)
        con.execute(f"""
          CREATE TABLE real_sets AS
          SELECT list_sort(list(DISTINCT code)) AS codes FROM (
            SELECT {id_col_sql} AS eid, unnest(string_split_regex(trim({joined}), '\\s+')) AS code
            FROM {raw}) WHERE code <> '' GROUP BY eid
        """)
        con.execute("""
          CREATE TABLE out_sets AS
          SELECT list_sort(list(DISTINCT replace(code, '.', ''))) AS codes
          FROM events GROUP BY person_id
        """)
        con.execute("CREATE TABLE real_norm AS SELECT list_sort(list_transform(codes, x -> replace(upper(x), '.', ''))) AS codes FROM real_sets")
        total = con.execute("SELECT count(*) FROM out_sets").fetchone()[0]
        intact = con.execute("""
          SELECT count(*) FROM out_sets o
          WHERE EXISTS (SELECT 1 FROM real_norm r WHERE r.codes = o.codes)
        """).fetchone()[0]
        share = intact / total if total else 0
        verdict = "OK" if share < 0.20 else "*** COMBINATIONS LARGELY INTACT ***"
        print(f"   {intact:,} of {total:,} output people ({share:.1%}) hold a code set that "
              f"some real person held   {verdict}")
        print("   (a few percent is chance -- people with one common code collide often;")
        print("    near 100% means only the identifier was replaced)")
        if share >= 0.20:
            failures += 1

    print("\n" + ("PASS -- no identifier leaked and combinations were broken up"
                  if not failures else
                  "FAIL -- do NOT move these files out of secure compute"))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
