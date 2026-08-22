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


def _load_excluded_phecodes(con, release: Path, exclude_phenotypes: Path | None) -> int:
    """Build the `excluded_phecodes` table: phecodes dropped from every output
    (phecode_counts, person_phecodes, eligible_phecodes, phenotype_matrix)
    entirely -- e.g. whole categories with poor genetic construct validity for
    a given analysis (see phecodex_mapper/data/recommended_exclusions.csv), or
    specific phecodes. Distinct from --control-exclusions, which only adjusts
    the control pool for *other* phecodes.
    """
    con.execute("CREATE TABLE excluded_phecodes(phecode VARCHAR)")
    if not exclude_phenotypes:
        return 0
    ex_src = relation_for(exclude_phenotypes)
    cols = _columns(con, ex_src)
    required = {"match_type", "match_value"}
    if required - cols:
        raise ValueError(f"--exclude-phenotypes missing columns: {sorted(required - cols)}")
    con.execute(f"CREATE VIEW exclude_phenotypes_input AS SELECT * FROM {ex_src}")
    bad_types = con.execute(
        "SELECT DISTINCT match_type FROM exclude_phenotypes_input WHERE match_type NOT IN ('category', 'phecode')"
    ).fetchall()
    if bad_types:
        raise ValueError(f"--exclude-phenotypes match_type must be 'category' or 'phecode', got: {[r[0] for r in bad_types]}")
    has_category_rule = con.execute(
        "SELECT count(*) FROM exclude_phenotypes_input WHERE match_type = 'category'"
    ).fetchone()[0] > 0
    info_path = release / "phecode_info.parquet"
    info_view = None
    if has_category_rule:
        if not info_path.exists():
            raise ValueError("--exclude-phenotypes has a 'category' rule, but this release has no phecode_info.parquet "
                              "(build-vocabulary was run without --phecodex-info). Rebuild the release with a "
                              "--phecodex-info file that has a 'category' column, or use 'phecode'-type rules instead.")
        info_view = f"read_parquet('{quote(info_path)}')"
        if "category" not in _columns(con, info_view):
            raise ValueError("--exclude-phenotypes has a 'category' rule, but this release's phecode_info has no "
                              "'category' column.")
    con.execute(f"""
      INSERT INTO excluded_phecodes
      SELECT DISTINCT match_value FROM exclude_phenotypes_input WHERE match_type = 'phecode'
    """ + (f"""
      UNION
      SELECT DISTINCT pi.phecode FROM exclude_phenotypes_input x JOIN {info_view} pi
        ON x.match_type = 'category' AND x.match_value = pi.category
    """ if info_view else ""))
    return con.execute("SELECT count(*) FROM excluded_phecodes").fetchone()[0]


def map_phecodes(release: Path, cohort: Path, events: Path, output: Path, case_rule: str = "any-event", exclusions: Path | None = None, min_cases: int = 200, min_controls: int = 200, max_unmapped_rate: float = 1.0, exclude_phenotypes: Path | None = None, hierarchy_aware: bool = False) -> None:
    if case_rule not in {"any-event", "two-dates"}:
        raise ValueError("case_rule must be any-event or two-dates")
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}. Remove it or choose a new --output path.")
    output.mkdir(parents=True)
    con = connect(); cohort_src = relation_for(cohort); event_src = relation_for(events)
    cohort_columns = _columns(con, cohort_src)
    if "person_id" not in cohort_columns: raise ValueError("Cohort requires person_id")
    if "sex" not in cohort_columns:
        raise ValueError("Cohort requires sex column")
    has_sex = True
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
      SELECT row_number() OVER () AS event_id, e.person_id,
        upper(trim(CAST(e.vocabulary AS VARCHAR))) AS vocabulary,
        CAST(e.code AS VARCHAR) AS source_code,
        CASE WHEN upper(trim(CAST(e.vocabulary AS VARCHAR))) IN ('ICD9CM','ICD10CM','ICD10')
             THEN regexp_replace(upper(trim(CAST(e.code AS VARCHAR))), '[.\\s-]', '', 'g')
             WHEN upper(trim(CAST(e.vocabulary AS VARCHAR))) = 'SNOMED' THEN regexp_replace(trim(CAST(e.code AS VARCHAR)), '\\s+', '', 'g')
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
    excluded_phecode_count = _load_excluded_phecodes(con, release, exclude_phenotypes)
    not_excluded = "phecode NOT IN (SELECT phecode FROM excluded_phecodes)"
    if case_rule == "any-event":
        con.execute(f"CREATE TABLE cases AS SELECT DISTINCT person_id, phecode FROM mapped_events WHERE {not_excluded}")
    else:
        con.execute(f"CREATE TABLE cases AS SELECT person_id, phecode FROM mapped_events WHERE event_date IS NOT NULL AND {not_excluded} GROUP BY person_id,phecode HAVING count(DISTINCT event_date) >= 2")
    con.execute(f"CREATE TABLE all_phecodes AS SELECT DISTINCT phecode FROM mapped_events WHERE {not_excluded}")
    con.execute("CREATE TABLE exclusions(person_id VARCHAR, phecode VARCHAR)")
    con.execute("CREATE TABLE exclusions_input(phecode VARCHAR, exclusion_type VARCHAR, exclusion_value VARCHAR, vocabulary VARCHAR)")
    exclusion_version = None
    if exclusions:
        ex_src = relation_for(exclusions); cols = _columns(con, ex_src)
        need = {"phecode", "exclusion_type", "exclusion_value", "vocabulary"}
        if need - cols: raise ValueError(f"Exclusions missing columns: {sorted(need-cols)}")
        con.execute("DROP TABLE exclusions_input")
        con.execute(f"CREATE TABLE exclusions_input AS SELECT * FROM {ex_src}")
        bad_types = con.execute(
            "SELECT DISTINCT exclusion_type FROM exclusions_input "
            "WHERE lower(trim(exclusion_type)) NOT IN ('phecode', 'code')"
        ).fetchall()
        if bad_types:
            raise ValueError(f"Exclusions exclusion_type must be phecode or code, got: {[r[0] for r in bad_types]}")
        bad_vocabularies = con.execute(
            "SELECT DISTINCT vocabulary FROM exclusions_input "
            "WHERE upper(trim(vocabulary)) NOT IN ('ICD9CM', 'ICD10CM', 'ICD10', 'SNOMED')"
        ).fetchall()
        if bad_vocabularies:
            raise ValueError(f"Exclusions vocabulary must be ICD9CM, ICD10CM, ICD10, or SNOMED, got: {[r[0] for r in bad_vocabularies]}")
        if "version" in cols: exclusion_version = con.execute("SELECT min(version) FROM exclusions_input").fetchone()[0]
        con.execute("""
          INSERT INTO exclusions
          SELECT DISTINCT m.person_id, x.phecode FROM exclusions_input x JOIN mapped_events m
            ON x.exclusion_type = 'phecode' AND x.exclusion_value = m.phecode
          UNION
          SELECT DISTINCT e.person_id, x.phecode FROM exclusions_input x JOIN normalized_events e
            ON x.exclusion_type = 'code'
            AND upper(trim(CAST(x.vocabulary AS VARCHAR))) = e.vocabulary
            AND regexp_replace(upper(trim(CAST(x.exclusion_value AS VARCHAR))), '[.\\s-]', '', 'g') = e.normalized_code
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

    hierarchy_info = None
    if hierarchy_aware:
        hierarchy_info = _write_hierarchy_variant(con, release, output, has_sex, case_rule, min_cases, min_controls)

    total = con.execute("SELECT count(*) FROM normalized_events").fetchone()[0]; unmapped = con.execute("SELECT count(*) FROM unmapped_events").fetchone()[0]
    rate = unmapped / total if total else 0
    audit = {"created_at_utc": dt.datetime.now(dt.UTC).isoformat(), "release": str(release), "case_rule": case_rule,
             "min_cases": min_cases, "min_controls": min_controls, "exclusion_version": exclusion_version,
             "exclude_phenotypes": None if not exclude_phenotypes else {"file": str(exclude_phenotypes), "phecodes_excluded": excluded_phecode_count},
             "events": total, "unmapped_events": unmapped, "unmapped_rate": rate,
             "release_manifest_sha256": checksum(release / "manifest.json"),
             "phenotype_matrix": matrix_info, "hierarchy_aware": hierarchy_info}
    (output / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    if rate > max_unmapped_rate: raise RuntimeError(f"Unmapped rate {rate:.3%} exceeds threshold {max_unmapped_rate:.3%}")


def _write_hierarchy_variant(con, release: Path, output: Path, has_sex: bool, case_rule: str, min_cases: int, min_controls: int) -> dict:
    hierarchy_path = release / "icd_hierarchy.parquet"
    if not hierarchy_path.exists():
        raise ValueError("--hierarchy-aware requires a release containing icd_hierarchy.parquet")
    con.execute(f"CREATE VIEW icd_hierarchy AS SELECT * FROM read_parquet('{quote(hierarchy_path)}')")
    con.execute("""
      CREATE TABLE mapped_events_hierarchy AS
      WITH exact AS (
        SELECT e.event_id, e.person_id, m.phecode, e.event_date, 'exact' AS mapping_type,
               e.source_code, e.normalized_code, e.vocabulary AS source_vocabulary,
               e.normalized_code AS matched_icd_code, CAST(NULL AS VARCHAR) AS hierarchy_source_version
        FROM normalized_events e JOIN icd_map m
          ON e.vocabulary = m.vocabulary AND e.normalized_code = m.normalized_code
      ), fallback_candidates AS (
        SELECT e.event_id, e.person_id, m.phecode, e.event_date, 'parent_fallback' AS mapping_type,
               e.source_code, e.normalized_code, e.vocabulary AS source_vocabulary,
               h.parent_code AS matched_icd_code, h.source_version AS hierarchy_source_version,
               max(length(h.parent_code)) OVER (PARTITION BY e.event_id) AS max_parent_length
        FROM normalized_events e JOIN icd_hierarchy h ON e.vocabulary = h.vocabulary AND e.normalized_code = h.child_code
        JOIN icd_map m ON m.vocabulary = h.vocabulary AND m.normalized_code = h.parent_code
        WHERE e.vocabulary IN ('ICD9CM', 'ICD10', 'ICD10CM')
          AND NOT EXISTS (SELECT 1 FROM icd_map exact_map WHERE exact_map.vocabulary=e.vocabulary AND exact_map.normalized_code=e.normalized_code)
      )
      SELECT event_id, person_id, phecode, event_date, mapping_type, source_code, normalized_code,
             source_vocabulary, matched_icd_code, hierarchy_source_version FROM exact
      UNION ALL
      SELECT event_id, person_id, phecode, event_date, mapping_type, source_code, normalized_code,
             source_vocabulary, matched_icd_code, hierarchy_source_version FROM fallback_candidates
      WHERE length(matched_icd_code)=max_parent_length
    """)
    con.execute("CREATE TABLE unmapped_events_hierarchy AS SELECT e.* FROM normalized_events e WHERE normalized_code IS NULL OR NOT EXISTS (SELECT 1 FROM mapped_events_hierarchy m WHERE m.event_id=e.event_id)")
    con.execute("CREATE TABLE exclusions_hierarchy(person_id VARCHAR, phecode VARCHAR)")
    if _columns(con, "exclusions_input"):
        con.execute("""
          INSERT INTO exclusions_hierarchy
          SELECT DISTINCT m.person_id, x.phecode FROM exclusions_input x JOIN mapped_events_hierarchy m
            ON x.exclusion_type = 'phecode' AND x.exclusion_value = m.phecode
          UNION
          SELECT DISTINCT e.person_id, x.phecode FROM exclusions_input x JOIN normalized_events e
            ON x.exclusion_type = 'code'
            AND upper(trim(CAST(x.vocabulary AS VARCHAR))) = e.vocabulary
            AND regexp_replace(upper(trim(CAST(x.exclusion_value AS VARCHAR))), '[.\\s-]', '', 'g') = e.normalized_code
        """)
    not_excluded = "phecode NOT IN (SELECT phecode FROM excluded_phecodes)"
    if case_rule == "any-event":
        con.execute(f"CREATE TABLE cases_hierarchy AS SELECT DISTINCT person_id, phecode FROM mapped_events_hierarchy WHERE {not_excluded}")
    else:
        con.execute(f"CREATE TABLE cases_hierarchy AS SELECT person_id, phecode FROM mapped_events_hierarchy WHERE event_date IS NOT NULL AND {not_excluded} GROUP BY person_id,phecode HAVING count(DISTINCT event_date) >= 2")
    con.execute("""CREATE TABLE phecode_counts_hierarchy AS
      SELECT p.phecode, coalesce(cc.case_count,0) AS case_count,
        (SELECT count(*) FROM cohort)-coalesce(cc.case_count,0) AS control_count_before_exclusions,
        coalesce(ec.excluded_count,0) AS excluded_control_count,
        (SELECT count(*) FROM cohort)-coalesce(cc.case_count,0)-coalesce(ec.excluded_count,0) AS control_count_after_exclusions
      FROM (SELECT DISTINCT phecode FROM mapped_events_hierarchy WHERE phecode NOT IN (SELECT phecode FROM excluded_phecodes)) p
      LEFT JOIN (SELECT phecode,count(DISTINCT person_id) case_count FROM cases_hierarchy GROUP BY phecode) cc USING(phecode)
      LEFT JOIN (SELECT ex.phecode,count(DISTINCT ex.person_id) excluded_count FROM exclusions_hierarchy ex LEFT JOIN cases_hierarchy ca ON ca.phecode=ex.phecode AND ca.person_id=ex.person_id WHERE ca.person_id IS NULL GROUP BY ex.phecode) ec USING(phecode)
    """)
    con.execute(f"ALTER TABLE phecode_counts_hierarchy ADD COLUMN retained BOOLEAN; UPDATE phecode_counts_hierarchy SET retained=case_count >= {int(min_cases)} AND control_count_after_exclusions >= {int(min_controls)}")
    con.execute(f"COPY phecode_counts_hierarchy TO '{quote(output / 'phecode_counts_hierarchy.parquet')}' (FORMAT PARQUET)")
    hierarchy_rows = con.execute("SELECT * FROM phecode_counts_hierarchy WHERE retained ORDER BY phecode").fetchall()
    hierarchy_headers = [r[0] for r in con.execute("DESCRIBE phecode_counts_hierarchy").fetchall()]
    _xlsx(output / "eligible_phecodes_hierarchy.xlsx", hierarchy_headers, hierarchy_rows)
    con.execute(f"COPY (SELECT * FROM cases_hierarchy) TO '{quote(output / 'person_phecodes_hierarchy.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY unmapped_events_hierarchy TO '{quote(output / 'unmapped_events_hierarchy.csv')}' (HEADER, DELIMITER ',')")
    con.execute(f"""COPY (
      SELECT source_code, normalized_code, source_vocabulary AS vocabulary,
             matched_icd_code AS parent_code, phecode, mapping_type,
             hierarchy_source_version,
             length(normalized_code) - length(matched_icd_code) AS parent_depth,
             count(DISTINCT event_id) AS event_count
      FROM mapped_events_hierarchy WHERE mapping_type='parent_fallback'
      GROUP BY source_code, normalized_code, source_vocabulary, matched_icd_code,
               phecode, mapping_type, hierarchy_source_version
      ORDER BY event_count DESC, vocabulary, parent_code, phecode
    ) TO '{quote(output / 'hierarchy_fallbacks.csv')}' (HEADER, DELIMITER ',')""")
    matrix = _write_phenotype_matrix(con, release, output, has_sex, "_hierarchy", "exclusions_hierarchy")
    fallback = con.execute("SELECT count(DISTINCT event_id), count(DISTINCT normalized_code) FROM mapped_events_hierarchy WHERE mapping_type='parent_fallback'").fetchone()
    unmapped = con.execute("SELECT count(*) FROM unmapped_events_hierarchy").fetchone()[0]
    by_vocab = {r[0]: r[1] for r in con.execute("SELECT source_vocabulary,count(DISTINCT event_id) FROM mapped_events_hierarchy WHERE mapping_type='parent_fallback' GROUP BY source_vocabulary").fetchall()}
    depths = {str(r[0]): r[1] for r in con.execute("SELECT length(normalized_code)-length(matched_icd_code), count(DISTINCT event_id) FROM mapped_events_hierarchy WHERE mapping_type='parent_fallback' GROUP BY 1 ORDER BY 1").fetchall()}
    return {"fallback_events": fallback[0], "fallback_codes": fallback[1], "fallback_events_by_vocabulary": by_vocab, "fallback_events_by_parent_depth": depths, "unmapped_events": unmapped, "phenotype_matrix": matrix}


def _write_phenotype_matrix(con, release: Path, output: Path, has_sex: bool, suffix: str = "", exclusions_table: str = "exclusions") -> dict:
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

    counts_table = "phecode_counts" + suffix
    cases_table = "cases" + suffix
    retained_phecodes = [r[0] for r in con.execute(f"SELECT phecode FROM {counts_table} WHERE retained ORDER BY phecode").fetchall()]
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
        con.execute(f"CREATE TABLE phenotype_matrix{suffix} AS SELECT person_id FROM cohort ORDER BY person_id")
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
        con.execute(f"CREATE TABLE retained_phecodes{suffix}(phecode VARCHAR, restrict_sex VARCHAR)")
        con.executemany(f"INSERT INTO retained_phecodes{suffix} VALUES (?, ?)",
                         [(p, phecode_sex.get(p)) for p in retained_phecodes])
        con.execute(f"""
          CREATE TABLE sparse_values{suffix} AS
          SELECT ca.person_id, ca.phecode, 1 AS value FROM {cases_table} ca JOIN retained_phecodes{suffix} rp ON rp.phecode = ca.phecode
          UNION ALL
          SELECT ex.person_id, ex.phecode, -1 AS value FROM {exclusions_table} ex JOIN retained_phecodes{suffix} rp ON rp.phecode = ex.phecode
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
              SELECT person_id, {agg_columns} FROM sparse_values{suffix} WHERE phecode IN ({batch_in_list}) GROUP BY person_id
            """)
            final_columns = ", ".join(
                f"""CASE WHEN {sql_literal(phecode_sex.get(p))} IS NOT NULL AND (c.sex IS NULL OR c.sex <> {sql_literal(phecode_sex.get(p))}) THEN NULL """
                f"""WHEN pa.{quote_ident(p)} = 1 THEN 1 """
                f"""WHEN pa.{quote_ident(p)} = -1 THEN NULL """
                f"""ELSE 0 END AS {quote_ident(p)}"""
                for p in batch
            )
            table_name = f"matrix_batch{suffix}_{batch_start}"
            con.execute(f"""
              CREATE TABLE {table_name} AS
              SELECT c.person_id, {final_columns} FROM cohort c LEFT JOIN batch_agg pa ON pa.person_id = c.person_id
            """)
            con.execute("DROP TABLE batch_agg")
            batch_tables.append(table_name)

        join_clause = " JOIN ".join(batch_tables) if len(batch_tables) == 1 else \
            batch_tables[0] + "".join(f" JOIN {t} USING (person_id)" for t in batch_tables[1:])
        con.execute(f"CREATE TABLE phenotype_matrix{suffix} AS SELECT * FROM {join_clause} ORDER BY person_id")
        for table_name in batch_tables:
            con.execute(f"DROP TABLE {table_name}")
    # Compressed: this is a dense matrix (cohort_size x retained_phecode_count), which at
    # real biobank scale is large -- gzip the CSV, and use zstd (better ratio than the
    # Parquet default) for the Parquet copy.
    output_stem = "phenotype_matrix" + suffix
    con.execute(f"COPY phenotype_matrix{suffix} TO '{quote(output / (output_stem + '.parquet'))}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute(f"COPY phenotype_matrix{suffix} TO '{quote(output / (output_stem + '.csv.gz'))}' (HEADER, DELIMITER ',', COMPRESSION 'gzip')")
    return {
        "n_columns": len(retained_phecodes),
        "cohort_has_sex_column": has_sex,
        "sex_restricted_retained_phecodes": len(sex_restricted_retained),
        "sex_restricted_phecodes_treated_as_unrestricted": 0 if has_sex else len(sex_restricted_retained),
    }
