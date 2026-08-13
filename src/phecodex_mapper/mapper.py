from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from openpyxl import Workbook

from .io import checksum, connect, quote, relation_for


def _columns(con, source: str) -> set[str]:
    return {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}


def _xlsx(path: Path, headers: list[str], rows: list[tuple]) -> None:
    book = Workbook(write_only=True); sheet = book.create_sheet("eligible phecodes")
    sheet.append(headers)
    for row in rows: sheet.append(list(row))
    book.save(path)


def map_phecodes(release: Path, cohort: Path, events: Path, output: Path, case_rule: str = "any-event", exclusions: Path | None = None, min_cases: int = 200, min_controls: int = 200, max_unmapped_rate: float = 1.0) -> None:
    if case_rule not in {"any-event", "two-dates"}:
        raise ValueError("case_rule must be any-event or two-dates")
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}. Remove it or choose a new --output path.")
    output.mkdir(parents=True)
    con = connect(); cohort_src = relation_for(cohort); event_src = relation_for(events)
    cohort_columns = _columns(con, cohort_src)
    if "person_id" not in cohort_columns: raise ValueError("Cohort requires person_id")
    has_sex = "sex" in cohort_columns
    required = {"person_id", "code", "vocabulary"}
    missing = required - _columns(con, event_src)
    if missing: raise ValueError(f"Events missing columns: {sorted(missing)}")
    if case_rule == "two-dates" and "event_date" not in _columns(con, event_src): raise ValueError("two-dates requires event_date")
    con.execute(f"CREATE VIEW cohort_input AS SELECT * FROM {cohort_src}")
    invalid = con.execute("SELECT count(*) FROM cohort_input WHERE person_id IS NULL").fetchone()[0]
    duplicate = con.execute("SELECT count(*) - count(DISTINCT person_id) FROM cohort_input").fetchone()[0]
    if invalid or duplicate: raise ValueError("Cohort person_id must be non-null and unique")
    sex_expression = "upper(trim(sex))" if has_sex else "CAST(NULL AS VARCHAR)"
    con.execute(f"CREATE TABLE cohort AS SELECT person_id, {sex_expression} AS sex FROM cohort_input")
    con.execute(f"CREATE VIEW events_input AS SELECT * FROM {event_src}")
    date_expression = "try_cast(e.event_date AS DATE)" if "event_date" in _columns(con, event_src) else "CAST(NULL AS DATE)"
    con.execute(f"""
      CREATE TABLE normalized_events AS
      SELECT row_number() OVER () AS event_id, e.person_id, upper(trim(e.vocabulary)) AS vocabulary, e.code AS source_code,
        CASE WHEN upper(trim(e.vocabulary)) IN ('ICD9CM','ICD10CM')
             THEN regexp_replace(upper(trim(e.code)), '[.\\s-]', '', 'g')
             WHEN upper(trim(e.vocabulary)) = 'SNOMED' THEN regexp_replace(trim(e.code), '\\s+', '', 'g')
        ELSE NULL END AS normalized_code,
        {date_expression} AS event_date
      FROM events_input e JOIN cohort c USING (person_id)
    """)
    con.execute(f"CREATE VIEW icd_map AS SELECT * FROM read_parquet('{quote(release / 'icd_map.parquet')}')")
    snomed_exists = (release / "snomed_map.parquet").exists()
    if snomed_exists: con.execute(f"CREATE VIEW snomed_map AS SELECT * FROM read_parquet('{quote(release / 'snomed_map.parquet')}')")
    con.execute("""
      CREATE TABLE mapped_events AS
      SELECT e.event_id, e.person_id, m.phecode, e.event_date FROM normalized_events e JOIN icd_map m
       ON e.vocabulary = m.vocabulary AND e.normalized_code = m.normalized_code
    """ + (" UNION SELECT e.event_id, e.person_id, m.phecode, e.event_date FROM normalized_events e JOIN snomed_map m ON e.vocabulary = 'SNOMED' AND e.normalized_code = m.source_code" if snomed_exists else ""))
    con.execute("""
      CREATE TABLE unmapped_events AS SELECT e.* FROM normalized_events e
      WHERE normalized_code IS NULL OR NOT EXISTS (SELECT 1 FROM mapped_events m WHERE m.event_id=e.event_id)
    """)
    if case_rule == "any-event":
        con.execute("CREATE TABLE cases AS SELECT DISTINCT person_id, phecode FROM mapped_events")
    else:
        con.execute("CREATE TABLE cases AS SELECT person_id, phecode FROM mapped_events WHERE event_date IS NOT NULL GROUP BY person_id,phecode HAVING count(DISTINCT event_date) >= 2")
    con.execute("CREATE TABLE all_phecodes AS SELECT DISTINCT phecode FROM mapped_events")
    con.execute("CREATE TABLE exclusions(person_id VARCHAR, phecode VARCHAR)")
    exclusion_version = None
    if exclusions:
        ex_src = relation_for(exclusions); cols = _columns(con, ex_src)
        need = {"phecode", "exclusion_type", "exclusion_value"}
        if need - cols: raise ValueError(f"Exclusions missing columns: {sorted(need-cols)}")
        con.execute(f"CREATE VIEW exclusions_input AS SELECT * FROM {ex_src}")
        if "version" in cols: exclusion_version = con.execute("SELECT min(version) FROM exclusions_input").fetchone()[0]
        con.execute("""
          INSERT INTO exclusions
          SELECT DISTINCT m.person_id, x.phecode FROM exclusions_input x JOIN mapped_events m
            ON x.exclusion_type = 'phecode' AND x.exclusion_value = m.phecode
          UNION
          SELECT DISTINCT e.person_id, x.phecode FROM exclusions_input x JOIN normalized_events e
            ON x.exclusion_type = 'code' AND upper(trim(x.exclusion_value)) = e.normalized_code
        """)
    # Deliberately avoids `all_phecodes CROSS JOIN cohort`: at real-world scale (thousands
    # of phecodes x hundreds of thousands of people) that intermediate is hundreds of
    # millions of rows before any filtering happens. Instead, aggregate case counts and
    # (non-case) exclusion counts per phecode first, then join those small per-phecode
    # summaries onto all_phecodes -- same semantics (control_count_before_exclusions =
    # cohort size minus cases; excluded_control_count only counts exclusions on people who
    # are not already a case for that phecode), but cost scales with cases+exclusions
    # rows, not cohort_size x phecode_count.
    con.execute("""
      CREATE TABLE phecode_counts AS
      SELECT p.phecode,
        coalesce(cc.case_count, 0) AS case_count,
        (SELECT count(*) FROM cohort) - coalesce(cc.case_count, 0) AS control_count_before_exclusions,
        coalesce(ec.excluded_count, 0) AS excluded_control_count,
        (SELECT count(*) FROM cohort) - coalesce(cc.case_count, 0) - coalesce(ec.excluded_count, 0) AS control_count_after_exclusions
      FROM all_phecodes p
      LEFT JOIN (SELECT phecode, count(DISTINCT person_id) AS case_count FROM cases GROUP BY phecode) cc
        ON cc.phecode = p.phecode
      LEFT JOIN (
        SELECT ex.phecode, count(DISTINCT ex.person_id) AS excluded_count
        FROM exclusions ex
        LEFT JOIN cases ca2 ON ca2.phecode = ex.phecode AND ca2.person_id = ex.person_id
        WHERE ca2.person_id IS NULL
        GROUP BY ex.phecode
      ) ec ON ec.phecode = p.phecode
    """)
    con.execute(f"ALTER TABLE phecode_counts ADD COLUMN retained BOOLEAN; UPDATE phecode_counts SET retained = case_count >= {int(min_cases)} AND control_count_after_exclusions >= {int(min_controls)}")
    con.execute(f"COPY phecode_counts TO '{quote(output / 'phecode_counts.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY phecode_counts TO '{quote(output / 'phecode_counts.csv')}' (HEADER, DELIMITER ',')")
    con.execute(f"COPY (SELECT * FROM cases) TO '{quote(output / 'person_phecodes.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY (SELECT * FROM unmapped_events) TO '{quote(output / 'unmapped_events.csv')}' (HEADER, DELIMITER ',')")
    rows = con.execute("SELECT * FROM phecode_counts WHERE retained ORDER BY phecode").fetchall()
    headers = [r[0] for r in con.execute("DESCRIBE phecode_counts").fetchall()]
    _xlsx(output / "eligible_phecodes.xlsx", headers, rows)

    matrix_info = _write_phenotype_matrix(con, release, output, has_sex)

    total = con.execute("SELECT count(*) FROM normalized_events").fetchone()[0]; unmapped = con.execute("SELECT count(*) FROM unmapped_events").fetchone()[0]
    rate = unmapped / total if total else 0
    audit = {"created_at_utc": dt.datetime.now(dt.UTC).isoformat(), "release": str(release), "case_rule": case_rule,
             "min_cases": min_cases, "min_controls": min_controls, "exclusion_version": exclusion_version,
             "events": total, "unmapped_events": unmapped, "unmapped_rate": rate,
             "release_manifest_sha256": checksum(release / "manifest.json"),
             "phenotype_matrix": matrix_info}
    (output / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    if rate > max_unmapped_rate: raise RuntimeError(f"Unmapped rate {rate:.3%} exceeds threshold {max_unmapped_rate:.3%}")


def _write_phenotype_matrix(con, release: Path, output: Path, has_sex: bool) -> dict:
    """Write a wide person x phecode matrix: 1 = case, 0 = control, NA = not
    evaluable (sex-restricted phecode and person's sex doesn't match / is
    unknown, or the person is covered by a control exclusion for that
    phecode without being a case). Columns are restricted to retained
    phecodes (case_count >= --min-cases and control_count_after_exclusions
    >= --min-controls), matching eligible_phecodes.xlsx.
    """
    phecode_sex: dict[str, str] = {}
    info_path = release / "phecode_info.parquet"
    if info_path.exists():
        info_view = f"read_parquet('{quote(info_path)}')"
        info_columns = _columns(con, info_view)
        if {"phecode", "sex"} <= info_columns:
            for phecode, sex in con.execute(f"SELECT phecode, upper(trim(sex)) FROM {info_view}").fetchall():
                if sex in ("MALE", "FEMALE"):
                    phecode_sex[phecode] = sex

    retained_phecodes = [r[0] for r in con.execute("SELECT phecode FROM phecode_counts WHERE retained ORDER BY phecode").fetchall()]
    sex_restricted_retained = [p for p in retained_phecodes if p in phecode_sex]
    if sex_restricted_retained and not has_sex:
        # Not fatal: without a cohort sex column we cannot tell an ineligible
        # opposite-sex person apart from a genuine control, so those cells
        # are left as ordinary controls (0) rather than NA. Flag this loudly
        # in the audit trail rather than silently producing a matrix that
        # looks complete but is wrong for these columns.
        print(f"phecodex-map: warning: {len(sex_restricted_retained)} retained phecode(s) are sex-restricted "
              "but --cohort has no 'sex' column -- those columns will contain 0 for opposite-sex people "
              "instead of NA. Add a 'sex' column (values 'Male'/'Female') to --cohort to fix this.",
              file=sys.stderr)

    if not retained_phecodes:
        con.execute("CREATE TABLE phenotype_matrix AS SELECT person_id FROM cohort ORDER BY person_id")
    else:
        # Deliberately avoids `cohort CROSS JOIN retained_phecodes` (and DuckDB's PIVOT over
        # a large IN-list, which internally rescans its source once per pivot value): at
        # real-world scale (hundreds of thousands of people x thousands of retained
        # phecodes) that's the same hundreds-of-millions-of-rows blowup as the
        # phecode_counts cross join fixed above, and re-scanning per column made it worse,
        # not better (observed: single-digit minutes *per column* against a 336k-person
        # fixture, i.e. effectively unbounded). Instead, build a *sparse* per-(person,
        # phecode) table sized to cases+exclusions (not cohort_size x phecode_count), then
        # pivot that with one MAX(CASE WHEN phecode=... THEN val END) expression per column
        # -- a single GROUP BY pass over the sparse table, however many columns there are.
        # 1 = case, -1 = excluded-from-controls; MAX(1, -1) = 1 so a case always wins over
        # an exclusion for the same person+phecode, matching "a case is never excluded".
        con.execute("CREATE TABLE retained_phecodes(phecode VARCHAR, restrict_sex VARCHAR)")
        con.executemany("INSERT INTO retained_phecodes VALUES (?, ?)",
                         [(p, phecode_sex.get(p)) for p in retained_phecodes])
        con.execute("""
          CREATE TABLE sparse_values AS
          SELECT ca.person_id, ca.phecode, 1 AS value FROM cases ca JOIN retained_phecodes rp ON rp.phecode = ca.phecode
          UNION ALL
          SELECT ex.person_id, ex.phecode, -1 AS value FROM exclusions ex JOIN retained_phecodes rp ON rp.phecode = ex.phecode
        """)

        def sql_literal(value: str | None) -> str:
            return "NULL" if value is None else "'" + value.replace("'", "''") + "'"

        def quote_ident(name: str) -> str:
            return '"' + name.replace('"', '""') + '"'

        # Batch columns rather than building one query with a column per retained
        # phecode: a single aggregate/CASE expression list spanning thousands of
        # columns measurably increases DuckDB's temp-spill footprint (observed:
        # ~850MB of spill for ~2,600 columns against a 336k-person cohort). Batching
        # keeps each step's working set bounded by batch_size regardless of how many
        # phecodes are retained; the batches are then stitched back together with
        # cheap USING(person_id) joins.
        batch_size = 200
        batch_tables = []
        for batch_start in range(0, len(retained_phecodes), batch_size):
            batch = retained_phecodes[batch_start:batch_start + batch_size]
            batch_in_list = ", ".join(sql_literal(p) for p in batch)
            agg_columns = ", ".join(
                f"max(CASE WHEN phecode = {sql_literal(p)} THEN value END) AS {quote_ident(p)}" for p in batch
            )
            con.execute(f"""
              CREATE TEMP TABLE batch_agg AS
              SELECT person_id, {agg_columns} FROM sparse_values WHERE phecode IN ({batch_in_list}) GROUP BY person_id
            """)
            final_columns = ", ".join(
                f"""CASE WHEN {sql_literal(phecode_sex.get(p))} IS NOT NULL AND (c.sex IS NULL OR c.sex <> {sql_literal(phecode_sex.get(p))}) THEN NULL """
                f"""WHEN pa.{quote_ident(p)} = 1 THEN 1 """
                f"""WHEN pa.{quote_ident(p)} = -1 THEN NULL """
                f"""ELSE 0 END AS {quote_ident(p)}"""
                for p in batch
            )
            table_name = f"matrix_batch_{batch_start}"
            con.execute(f"""
              CREATE TABLE {table_name} AS
              SELECT c.person_id, {final_columns} FROM cohort c LEFT JOIN batch_agg pa ON pa.person_id = c.person_id
            """)
            con.execute("DROP TABLE batch_agg")
            batch_tables.append(table_name)

        join_clause = " JOIN ".join(batch_tables) if len(batch_tables) == 1 else \
            batch_tables[0] + "".join(f" JOIN {t} USING (person_id)" for t in batch_tables[1:])
        con.execute(f"CREATE TABLE phenotype_matrix AS SELECT * FROM {join_clause} ORDER BY person_id")
        for table_name in batch_tables:
            con.execute(f"DROP TABLE {table_name}")
    # Compressed: this is a dense matrix (cohort_size x retained_phecode_count), which at
    # real biobank scale is large -- gzip the CSV, and use zstd (better ratio than the
    # Parquet default) for the Parquet copy.
    con.execute(f"COPY phenotype_matrix TO '{quote(output / 'phenotype_matrix.parquet')}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute(f"COPY phenotype_matrix TO '{quote(output / 'phenotype_matrix.csv.gz')}' (HEADER, DELIMITER ',', COMPRESSION 'gzip')")
    return {
        "n_columns": len(retained_phecodes),
        "cohort_has_sex_column": has_sex,
        "sex_restricted_retained_phecodes": len(sex_restricted_retained),
        "sex_restricted_phecodes_treated_as_unrestricted": 0 if has_sex else len(sex_restricted_retained),
    }
