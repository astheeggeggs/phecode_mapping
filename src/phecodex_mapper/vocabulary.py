from __future__ import annotations

import datetime as dt
from pathlib import Path

from openpyxl import Workbook

from . import __version__
from .io import checksum, connect, quote, relation_for, write_release_metadata


REQUIRED_MAP_COLUMNS = {"phecode", "ICD", "vocabulary_id"}


def _columns(con, source: str) -> set[str]:
    return {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}


def _write_xlsx(path: Path, sheets: dict[str, list[tuple[list[str], list[tuple]]]]) -> None:
    book = Workbook(write_only=True)
    for name, (headers, rows) in sheets.items():
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for row in rows:
            sheet.append(list(row))
    book.save(path)


def build_vocabulary(phecodex_map: Path | list[Path], phecodex_info: Path | None, output: Path, athena_dir: Path | None = None) -> None:
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}. Remove it or choose a new --output path.")
    output.mkdir(parents=True)
    con = connect()
    map_paths = [phecodex_map] if isinstance(phecodex_map, Path) else phecodex_map
    if not map_paths:
        raise ValueError("At least one PhecodeX map is required")
    sources = [relation_for(path) for path in map_paths]
    normalized_sources = []
    for item in sources:
        item_columns = _columns(con, item)
        code_column = "ICD" if "ICD" in item_columns else "icd" if "icd" in item_columns else None
        if code_column is None or "phecode" not in item_columns or "vocabulary_id" not in item_columns:
            raise ValueError("PhecodeX map requires columns: phecode, ICD/icd, vocabulary_id")
        normalized_sources.append(f"SELECT phecode, {code_column} AS ICD, vocabulary_id FROM {item}")
    source = " UNION ALL ".join(normalized_sources)
    columns = _columns(con, f"({source})")
    missing = REQUIRED_MAP_COLUMNS - columns
    if missing:
        raise ValueError(f"PhecodeX map missing columns: {sorted(missing)}")
    con.execute(f"CREATE VIEW source_map AS SELECT * FROM ({source})")
    # Keep source ICD verbatim for traceability and a punctuation-insensitive key for joins.
    con.execute("""
        CREATE TABLE icd_map AS
        SELECT DISTINCT phecode, ICD AS source_code,
               upper(vocabulary_id) AS vocabulary,
               regexp_replace(upper(trim(ICD)), '[.\\s-]', '', 'g') AS normalized_code
        FROM source_map
        WHERE upper(vocabulary_id) IN ('ICD9CM', 'ICD10CM', 'ICD10')
    """)
    con.execute(f"COPY icd_map TO '{quote(output / 'icd_map.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY icd_map TO '{quote(output / 'icd_map.csv')}' (HEADER, DELIMITER ',')")
    if phecodex_info:
        info_source = relation_for(phecodex_info)
        info_columns = _columns(con, info_source)
        if "phecode" in info_columns:
            duplicate_info = con.execute(f"SELECT phecode FROM {info_source} GROUP BY phecode HAVING count(*) > 1 LIMIT 1").fetchone()
            if duplicate_info:
                raise ValueError(f"Phecode info contains duplicate phecode: {duplicate_info[0]}")
        if "sex" in info_columns:
            bad_sex = con.execute(f"SELECT DISTINCT sex FROM {info_source} WHERE upper(trim(sex)) NOT IN ('BOTH', 'MALE', 'FEMALE')").fetchall()
            if bad_sex:
                raise ValueError(f"Phecode info sex must be Both, Male, or Female, got: {[r[0] for r in bad_sex]}")
        con.execute(f"COPY (SELECT * FROM {info_source}) TO '{quote(output / 'phecode_info.parquet')}' (FORMAT PARQUET)")
        con.execute(f"COPY (SELECT * FROM {info_source}) TO '{quote(output / 'phecode_info.csv')}' (HEADER, DELIMITER ',')")
        if "phecode" in info_columns:
            con.execute(f"CREATE VIEW phecode_info AS SELECT * FROM {info_source}")
            sex = "coalesce(i.sex, 'Both')" if "sex" in info_columns else "'Both'"
            description = "coalesce(i.phecode_string, '')" if "phecode_string" in info_columns else "''"
            category = "coalesce(i.category, '')" if "category" in info_columns else "''"
        else:
            sex, description, category = "'Both'", "''", "''"
    else:
        sex, description, category = "'Both'", "''", "''"
    info_join = "LEFT JOIN phecode_info i USING (phecode)" if phecodex_info and "phecode" in _columns(con, relation_for(phecodex_info)) else ""
    # Adapter only: enables black-box parity tests; it is not a source of exclusions.
    con.execute(f"""
        COPY (SELECT m.phecode, m.source_code AS ICD,
          CASE WHEN m.vocabulary='ICD9CM' THEN 9 ELSE 10 END AS flag,
          {sex} AS sex, {description} AS phecode_string, {category} AS phecode_category,
          '' AS exclude_range
          FROM icd_map m {info_join})
        TO '{quote(output / 'phetk_custom_map.csv')}' (HEADER, DELIMITER ',')
    """)

    snomed_rows: list[tuple] = []
    if athena_dir:
        concept = athena_dir / "CONCEPT.csv"
        relationships = athena_dir / "CONCEPT_RELATIONSHIP.csv"
        if not concept.exists() or not relationships.exists():
            raise ValueError("Athena directory must contain CONCEPT.csv and CONCEPT_RELATIONSHIP.csv")
        con.execute(f"CREATE VIEW concept AS SELECT * FROM {relation_for(concept)}")
        con.execute(f"CREATE VIEW relationship AS SELECT * FROM {relation_for(relationships)}")
        # SNOMED -> standard Condition -> ICD source concepts -> official PhecodeX mappings.
        con.execute("""
            CREATE TABLE snomed_map AS
            WITH snomed AS (
              SELECT concept_id, concept_code FROM concept
              WHERE vocabulary_id = 'SNOMED' AND domain_id = 'Condition'
                AND invalid_reason IS NULL
            ), standard_concept AS (
              SELECT r.concept_id_1 AS snomed_id, r.concept_id_2 AS standard_id
              FROM relationship r WHERE r.relationship_id = 'Maps to' AND r.invalid_reason IS NULL
              UNION
              SELECT concept_id, concept_id FROM concept
              WHERE vocabulary_id = 'SNOMED' AND standard_concept = 'S' AND invalid_reason IS NULL
            ), icd_source AS (
              SELECT r.concept_id_1 AS icd_id, r.concept_id_2 AS standard_id
              FROM relationship r WHERE r.relationship_id = 'Maps to' AND r.invalid_reason IS NULL
            )
            SELECT DISTINCT s.concept_code AS source_code, 'SNOMED' AS vocabulary,
              m.phecode, m.source_code AS bridge_icd_code, m.vocabulary AS bridge_vocabulary
            FROM snomed s JOIN standard_concept sc ON s.concept_id = sc.snomed_id
              JOIN icd_source i ON sc.standard_id = i.standard_id
              JOIN concept ic ON i.icd_id = ic.concept_id
              -- Athena tags WHO ICD-10 concepts as vocabulary_id='ICD10'; retain
              -- that as distinct from US clinical-modification ICD10CM.
              JOIN icd_map m ON regexp_replace(upper(ic.concept_code), '[.\\s-]', '', 'g') = m.normalized_code
                AND ic.vocabulary_id = m.vocabulary
            WHERE ic.invalid_reason IS NULL
        """)
        con.execute(f"COPY snomed_map TO '{quote(output / 'snomed_map.parquet')}' (FORMAT PARQUET)")
        con.execute(f"COPY snomed_map TO '{quote(output / 'snomed_map.csv')}' (HEADER, DELIMITER ',')")
        snomed_rows = con.execute("SELECT * FROM snomed_map ORDER BY source_code, phecode").fetchall()

    icd_rows = con.execute("SELECT * FROM icd_map ORDER BY vocabulary, normalized_code, phecode").fetchall()
    _write_xlsx(output / "phecodex_reference_maps.xlsx", {
        "ICD map": (["phecode", "source_code", "vocabulary", "normalized_code"], icd_rows),
        "SNOMED bridge": (["source_code", "vocabulary", "phecode", "bridge_icd_code", "bridge_vocabulary"], snomed_rows),
    })
    manifest = {
        "tool_version": __version__, "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "phecodex_map": ([{"path": str(path), "sha256": checksum(path)} for path in map_paths]
                         if len(map_paths) > 1 else {"path": str(map_paths[0]), "sha256": checksum(map_paths[0])}),
        "phecodex_info": None if not phecodex_info else {"path": str(phecodex_info), "sha256": checksum(phecodex_info)},
        "athena_dir": None if not athena_dir else str(athena_dir),
        "counts": {"icd_map_rows": len(icd_rows), "snomed_map_rows": len(snomed_rows)},
    }
    if phecodex_info and "sex" in info_columns:
        manifest["phecodex_info_sex_counts"] = {
            sex: con.execute(
                f"SELECT count(*) FROM {info_source} WHERE upper(trim(sex)) = ?", [sex.upper()]
            ).fetchone()[0]
            for sex in ("Both", "Female", "Male")
        }
    write_release_metadata(output, manifest)
