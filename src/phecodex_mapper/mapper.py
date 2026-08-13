from __future__ import annotations

import datetime as dt
import json
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
    if "person_id" not in _columns(con, cohort_src): raise ValueError("Cohort requires person_id")
    required = {"person_id", "code", "vocabulary"}
    missing = required - _columns(con, event_src)
    if missing: raise ValueError(f"Events missing columns: {sorted(missing)}")
    if case_rule == "two-dates" and "event_date" not in _columns(con, event_src): raise ValueError("two-dates requires event_date")
    con.execute(f"CREATE VIEW cohort_input AS SELECT * FROM {cohort_src}")
    invalid = con.execute("SELECT count(*) FROM cohort_input WHERE person_id IS NULL").fetchone()[0]
    duplicate = con.execute("SELECT count(*) - count(DISTINCT person_id) FROM cohort_input").fetchone()[0]
    if invalid or duplicate: raise ValueError("Cohort person_id must be non-null and unique")
    con.execute("CREATE TABLE cohort AS SELECT person_id FROM cohort_input")
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
    total = con.execute("SELECT count(*) FROM normalized_events").fetchone()[0]; unmapped = con.execute("SELECT count(*) FROM unmapped_events").fetchone()[0]
    rate = unmapped / total if total else 0
    audit = {"created_at_utc": dt.datetime.now(dt.UTC).isoformat(), "release": str(release), "case_rule": case_rule,
             "min_cases": min_cases, "min_controls": min_controls, "exclusion_version": exclusion_version,
             "events": total, "unmapped_events": unmapped, "unmapped_rate": rate,
             "release_manifest_sha256": checksum(release / "manifest.json")}
    (output / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    if rate > max_unmapped_rate: raise RuntimeError(f"Unmapped rate {rate:.3%} exceeds threshold {max_unmapped_rate:.3%}")
