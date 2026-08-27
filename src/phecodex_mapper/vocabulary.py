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
    for path, item in zip(map_paths, sources):
        item_columns = _columns(con, item)
        code_column = "ICD" if "ICD" in item_columns else "icd" if "icd" in item_columns else None
        if code_column is None or "phecode" not in item_columns or "vocabulary_id" not in item_columns:
            raise ValueError("PhecodeX map requires columns: phecode, ICD/icd, vocabulary_id")
        # Carry the source filename so the manifest can record which file each
        # vocabulary label came from -- see the "vocabularies" block below.
        normalized_sources.append(
            f"SELECT phecode, {code_column} AS ICD, vocabulary_id, "
            f"'{Path(path).name}' AS source_file FROM {item}")
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
    # Per-vocabulary provenance, kept out of icd_map itself so the shipped map keeps
    # its existing columns and the phetk_custom_map export is unaffected.
    con.execute("""
        CREATE TABLE icd_map_sources AS
        SELECT DISTINCT upper(vocabulary_id) AS vocabulary,
               regexp_replace(upper(trim(ICD)), '[.\\s-]', '', 'g') AS normalized_code,
               source_file
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
    snomed_summary: dict | None = None
    if athena_dir:
        concept = athena_dir / "CONCEPT.csv"
        relationships = athena_dir / "CONCEPT_RELATIONSHIP.csv"
        if not concept.exists() or not relationships.exists():
            raise ValueError("Athena directory must contain CONCEPT.csv and CONCEPT_RELATIONSHIP.csv")
        con.execute(f"CREATE VIEW concept AS SELECT * FROM {relation_for(concept)}")
        con.execute(f"CREATE VIEW relationship AS SELECT * FROM {relation_for(relationships)}")
        # SNOMED -> standard Condition -> ICD source concepts -> official PhecodeX mappings.
        #
        # OMOP's ICD->SNOMED mapping is many-to-one: where no exact SNOMED equivalent
        # exists, many specific ICD codes collapse onto the nearest broader concept.
        # Reading that backwards, as this bridge must, is lossy -- 410 ICD codes map to
        # "Third trimester pregnancy" alone, including O10.x (pre-existing hypertension
        # complicating pregnancy) and O99.0x (anaemia complicating pregnancy).
        #
        # So a SNOMED concept S tells us only that the patient has *something* in the
        # set of ICD codes that collapse onto S. It therefore implies a phecode only if
        # EVERY one of those codes implies it -- the intersection, not the union. Taking
        # the union made one routine antenatal code a case for 144 phecodes, including
        # hypertension, anaemia and autoimmune disease.
        #
        # The intersection is cheap in coverage and decisive on the pathology: against a
        # full Athena extract it retains 13,376 of 13,867 SNOMED codes (96%) while the
        # worst fan-out falls from 144 phecodes to 10 -- and those remaining are genuine
        # multi-phenotype conditions such as "Herpes zoster iridocyclitis", which really
        # is both a zoster infection and an iridocyclitis. A one-to-one SNOMED/ICD
        # equivalence is unaffected, keeping every phecode it had.
        # Built in two steps, and the order matters. The universe is EVERY ICD code
        # that collapses onto the concept; the triples are the subset PhecodeX
        # happens to map. Counting the denominator on the universe rather than the
        # subset is the whole point: computing it from the already-joined triples
        # makes both sides of the HAVING enumerate the same truncated set, so the
        # equality is satisfied trivially and the concept inherits whatever slice
        # of its sources the map covers. Where that slice is biased -- and it often
        # is -- the result is the many-to-one inversion this rule exists to stop.
        # Measured on a full Athena extract, 799 of 13,397 concepts (6.0%) had a
        # truncated denominator; SNOMED 7895008 "Poisoning by drug" kept
        # MB_284.2 "Suicide and self-inflicted harm" on the strength of 108 mapped
        # sources out of 633, all of them the intentional-self-harm variants.
        con.execute("""
            CREATE TABLE snomed_source_universe AS
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
            SELECT DISTINCT s.concept_code AS source_code, ic.concept_code AS icd_code,
              ic.vocabulary_id
            FROM snomed s JOIN standard_concept sc ON s.concept_id = sc.snomed_id
              JOIN icd_source i ON sc.standard_id = i.standard_id
              JOIN concept ic ON i.icd_id = ic.concept_id
            WHERE ic.invalid_reason IS NULL
              -- Only vocabularies this release's map could cover. A source code in a
              -- vocabulary PhecodeX does not ship (ICD-O, Read, ...) can never imply a
              -- phecode, so counting it would make the rule impossible to satisfy and
              -- silently empty the bridge.
              AND ic.vocabulary_id IN (SELECT DISTINCT vocabulary FROM icd_map)
        """)
        con.execute("""
            CREATE TABLE snomed_bridge_triples AS
            SELECT DISTINCT u.source_code, u.icd_code,
              m.phecode, m.source_code AS bridge_icd_code, m.vocabulary AS bridge_vocabulary
            FROM snomed_source_universe u
              -- Athena tags WHO ICD-10 concepts as vocabulary_id='ICD10'; retain
              -- that as distinct from US clinical-modification ICD10CM.
              JOIN icd_map m ON regexp_replace(upper(u.icd_code), '[.\\s-]', '', 'g') = m.normalized_code
                AND u.vocabulary_id = m.vocabulary
        """)
        # n_source_icd_codes comes from the UNIVERSE, not the triples. This is the
        # difference between "every source code implies this phecode" and "every
        # source code we happen to map implies it" -- only the first is a safe
        # inference from a broad SNOMED concept to a specific phenotype.
        con.execute("""
            CREATE TABLE snomed_source_counts AS
            SELECT u.source_code,
                   count(DISTINCT u.icd_code) AS n_source_icd_codes,
                   count(DISTINCT t.icd_code) AS n_source_icd_codes_mapped
            FROM snomed_source_universe u
            LEFT JOIN snomed_bridge_triples t
              ON t.source_code = u.source_code AND t.icd_code = u.icd_code
            GROUP BY u.source_code
        """)
        con.execute("""
            CREATE TABLE snomed_map AS
            SELECT t.source_code, 'SNOMED' AS vocabulary, t.phecode,
                   min(t.bridge_icd_code) AS bridge_icd_code,
                   min(t.bridge_vocabulary) AS bridge_vocabulary,
                   any_value(c.n_source_icd_codes) AS source_icd_code_count,
                   -- Kept alongside so a reviewer can see the coverage behind each
                   -- retained mapping without recomputing it from the Athena extract.
                   any_value(c.n_source_icd_codes_mapped) AS source_icd_codes_mapped
            FROM snomed_bridge_triples t JOIN snomed_source_counts c USING (source_code)
            GROUP BY t.source_code, t.phecode
            HAVING count(DISTINCT t.icd_code) = any_value(c.n_source_icd_codes)
        """)
        con.execute(f"COPY snomed_map TO '{quote(output / 'snomed_map.parquet')}' (FORMAT PARQUET)")
        con.execute(f"COPY snomed_map TO '{quote(output / 'snomed_map.csv')}' (HEADER, DELIMITER ',')")
        snomed_rows = con.execute("SELECT * FROM snomed_map ORDER BY source_code, phecode").fetchall()
        dropped = con.execute("""
            SELECT count(*) FROM (SELECT DISTINCT source_code, phecode FROM snomed_bridge_triples)
        """).fetchone()[0] - len(snomed_rows)
        ambiguous = con.execute("""
            SELECT count(*) FROM (SELECT DISTINCT source_code FROM snomed_bridge_triples)
            WHERE source_code NOT IN (SELECT source_code FROM snomed_map)
        """).fetchone()[0]
        # How many concepts the map covers only partly. These are the ones the old
        # truncated denominator silently waved through, so the number belongs in the
        # manifest: it is the size of the population this rule is protecting against.
        partial = con.execute("""
            SELECT count(*) FROM snomed_source_counts
            WHERE n_source_icd_codes_mapped > 0
              AND n_source_icd_codes_mapped < n_source_icd_codes
        """).fetchone()[0]
        snomed_summary = {
            # Says what the code does. The previous wording claimed "every source ICD
            # code" while the denominator counted only mapped ones, so the manifest
            # asserted a guarantee the build did not provide.
            "rule": "phecode retained only where every source ICD code that Athena collapses "
                    "onto the concept implies it, counting sources absent from the PhecodeX map",
            "mappings_dropped_as_ambiguous": dropped,
            "snomed_codes_left_with_no_phecode": ambiguous,
            "snomed_codes_with_partial_map_coverage": partial,
        }

    icd_rows = con.execute("SELECT * FROM icd_map ORDER BY vocabulary, normalized_code, phecode").fetchall()
    _write_xlsx(output / "phecodex_reference_maps.xlsx", {
        "ICD map": (["phecode", "source_code", "vocabulary", "normalized_code"], icd_rows),
        "SNOMED bridge": (["source_code", "vocabulary", "phecode", "bridge_icd_code",
                            "bridge_vocabulary", "source_icd_code_count"], snomed_rows),
    })
    manifest = {
        "tool_version": __version__, "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "phecodex_map": ([{"path": str(path), "sha256": checksum(path)} for path in map_paths]
                         if len(map_paths) > 1 else {"path": str(map_paths[0]), "sha256": checksum(map_paths[0])}),
        "phecodex_info": None if not phecodex_info else {"path": str(phecodex_info), "sha256": checksum(phecodex_info)},
        "athena_dir": None if not athena_dir else str(athena_dir),
        "counts": {"icd_map_rows": len(icd_rows), "snomed_map_rows": len(snomed_rows)},
        "snomed_bridge": snomed_summary,
        # Which source file each vocabulary label came from. PhecodeX ships the WHO
        # ICD-10 map twice: phecodeX_unrolled_ICD_WHO.csv labels its 20,255 rows
        # ICD10, and phecodeX_unrolled_ICD_UKB.csv labels the byte-identical content
        # ICD10CM. Both are upstream choices and this tool carries the label through
        # rather than overriding it -- but that makes two releases silently
        # incompatible with the same events file, and the label alone cannot tell you
        # which you have. This can.
        # `rows` counts icd_map rows so it sums to counts.icd_map_rows; `distinct_codes`
        # counts codes, which is smaller wherever one code carries several phecodes.
        # Reporting only the second under the name `rows` made two numbers in the same
        # manifest fail to reconcile.
        "vocabularies": {
            vocabulary: {"rows": rows, "distinct_codes": codes, "source_files": sorted(files)}
            for vocabulary, rows, codes, files in con.execute("""
              SELECT m.vocabulary, count(*) AS rows, count(DISTINCT m.normalized_code) AS codes,
                     (SELECT list(DISTINCT s.source_file) FROM icd_map_sources s
                      WHERE s.vocabulary = m.vocabulary) AS files
              FROM icd_map m GROUP BY m.vocabulary ORDER BY m.vocabulary
            """).fetchall()
        },
    }
    # Checksum every file this release ships. Without these, verify_release.py can only
    # confirm that filenames exist -- the manifest's own source checksums describe the
    # build machine's input CSVs, which the receiving site does not have and cannot
    # check anything against. manifest.json itself is excluded because it is the file
    # carrying the digests; its integrity comes from the archive's .sha256 sidecar and
    # from audit.json's release_manifest_sha256.
    manifest["artifacts"] = {
        path.name: {"sha256": checksum(path), "bytes": path.stat().st_size}
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "manifest.json"
    }
    if phecodex_info and "sex" in info_columns:
        manifest["phecodex_info_sex_counts"] = {
            sex: con.execute(
                f"SELECT count(*) FROM {info_source} WHERE upper(trim(sex)) = ?", [sex.upper()]
            ).fetchone()[0]
            for sex in ("Both", "Female", "Male")
        }
    write_release_metadata(output, manifest)
