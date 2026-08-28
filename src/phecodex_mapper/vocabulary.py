from __future__ import annotations

import datetime as dt
import re
import sys
import zipfile
from pathlib import Path

from openpyxl import Workbook

from . import __version__
from .io import checksum, connect, pin_workbook_timestamps, quote, relation_for, write_release_metadata


REQUIRED_MAP_COLUMNS = {"phecode", "ICD", "vocabulary_id"}


def _columns(con, source: str) -> set[str]:
    return {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}


def _write_xlsx(path: Path, sheets: dict[str, list[tuple[list[str], list[tuple]]]]) -> None:
    book = Workbook(write_only=True)
    # openpyxl stamps the current time into docProps, which alone would make every
    # rebuild a different file even with identical contents. The release is meant to
    # be checksum-comparable between federated sites, so pin it.
    book.properties.created = book.properties.modified = dt.datetime(2000, 1, 1)
    for name, (headers, rows) in sheets.items():
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for row in rows:
            sheet.append(list(row))
    book.save(path)
    pin_workbook_timestamps(path)


def _recover_unmapped_codes(con, output: Path, athena_dir: Path | None,
                            adjudication: Path | None) -> dict:
    """Add ICD codes the published map omits but the release itself can justify.

    PhecodeX's WHO ICD-10 map is roughly six times coarser than its ICD-10-CM map
    (8,560 distinct codes against 55,338), and WHO retires codes the map never
    catches up with -- I84.x haemorrhoids was reclassified to K64.x, so a cohort
    spanning 2000-2022 loses every older haemorrhoid episode silently. Neither is
    a curation decision this tool should honour by dropping the events.

    Two routes supply evidence, and only evidence already inside the release is used
    -- nothing is inferred from code structure:

      cross_vocabulary  the same code carries phecodes under another vocabulary
                        of the SAME ICD generation (ICD-10 <-> ICD-10-CM); the
                        ICD-9/ICD-10 boundary is never crossed, because the two
                        reuse code strings for unrelated diseases
      snomed_bridge     the code maps to a SNOMED concept the bridge already
                        accepted, which means every source ICD code for that
                        concept agreed on the phecode (see the bridge above)

    Where both routes fire and agree, the row is added. Where only one fires, it
    is added. Where they DISAGREE the code is skipped unless an adjudication file
    resolves it, because guessing between two sources that contradict each other
    is exactly the inference this tool refuses to make elsewhere.

    This runs AFTER the SNOMED bridge and never feeds back into it; the bridge is
    built from the published map alone, so recovery cannot bootstrap itself.
    """
    con.execute("""
        CREATE TABLE recovery_candidates AS
        SELECT DISTINCT regexp_replace(upper(concept_code), '[.\\s-]', '', 'g') AS normalized_code,
               concept_code, vocabulary_id AS vocabulary, concept_id
        FROM concept
        WHERE vocabulary_id IN ('ICD9CM', 'ICD10CM', 'ICD10')
          AND NOT EXISTS (SELECT 1 FROM icd_map m
                          WHERE m.normalized_code = regexp_replace(upper(concept_code), '[.\\s-]', '', 'g')
                            AND m.vocabulary = vocabulary_id)
    """)
    con.execute("""
        CREATE TABLE recovery_cross AS
        SELECT c.normalized_code, c.vocabulary, list_sort(list(DISTINCT m.phecode)) AS phecodes
        FROM recovery_candidates c JOIN icd_map m
          ON m.normalized_code = c.normalized_code AND m.vocabulary <> c.vocabulary
          -- ...but only within one ICD generation. ICD-9 and ICD-10 reuse the same
          -- code STRINGS for unrelated diseases, so a bare string match across the
          -- boundary is not "the same code": ICD-9-CM V09.0 is penicillin-resistant
          -- infection while WHO ICD-10 V09.0 is a pedestrian hit in a nontraffic
          -- accident, and ICD-9-CM E888.9 is an unspecified fall while ICD-10-CM
          -- E88.89 is a metabolic disorder. Without this guard the E (ICD-9 external
          -- cause vs ICD-10 endocrine) and V (ICD-9 health status vs ICD-10 transport)
          -- chapters collide wholesale. ICD10 <-> ICD10CM, the route's actual purpose,
          -- is unaffected; ICD9CM has no sibling here, so it gets no cross evidence.
          AND (CASE WHEN m.vocabulary = 'ICD9CM' THEN 9 ELSE 10 END)
            = (CASE WHEN c.vocabulary = 'ICD9CM' THEN 9 ELSE 10 END)
        GROUP BY 1, 2
    """)
    con.execute("""
        CREATE TABLE recovery_snomed AS
        SELECT c.normalized_code, c.vocabulary, list_sort(list(DISTINCT s.phecode)) AS phecodes
        FROM recovery_candidates c
        JOIN relationship r ON r.concept_id_1 = c.concept_id
          AND r.relationship_id = 'Maps to' AND r.invalid_reason IS NULL
        JOIN concept sc ON sc.concept_id = r.concept_id_2 AND sc.vocabulary_id = 'SNOMED'
        JOIN snomed_map s ON s.source_code = sc.concept_code
        GROUP BY 1, 2
    """)
    # 'A' selects the cross-vocabulary assignment, 'B' the SNOMED route -- the same
    # labels the adjudication report uses, so a reviewed file can be fed straight back.
    con.execute("CREATE TABLE recovery_adjudicated"
                "(normalized_code VARCHAR, vocabulary VARCHAR, choice VARCHAR)")
    adjudication_meta = None
    if adjudication:
        adj = relation_for(adjudication)
        adj_columns = _columns(con, adj)
        needed = {"icd_code", "adjudication_A_or_B"}
        if needed - adj_columns:
            raise ValueError(f"--recovery-adjudication missing columns: {sorted(needed - adj_columns)}")
        # A verdict may be scoped to one vocabulary. A blank or absent column means
        # "any vocabulary", which is what a file written before the column existed
        # meant. Without this the reviewer's stated scope was silently discarded and
        # one verdict resolved that code's conflict under every vocabulary at once.
        vocabulary_expr = ("nullif(upper(trim(CAST(vocabulary AS VARCHAR))), '')"
                           if "vocabulary" in adj_columns else "CAST(NULL AS VARCHAR)")
        con.execute(f"""
            INSERT INTO recovery_adjudicated
            SELECT regexp_replace(upper(icd_code), '[.\\s-]', '', 'g'), {vocabulary_expr},
                   upper(trim(CAST(adjudication_A_or_B AS VARCHAR)))
            FROM {adj} WHERE adjudication_A_or_B IS NOT NULL
              AND upper(trim(CAST(adjudication_A_or_B AS VARCHAR))) IN ('A', 'B')
        """)
        # The file is the reviewer's authority, so it must speak with one voice. Two
        # rows that can both match one code fanned the LEFT JOIN below out into two
        # resolved rows and applied BOTH verdicts -- adding the union of the two
        # contradicting routes, which is precisely the guess this feature refuses to
        # make. Dotted and undotted spellings of one code collide here too.
        ambiguous = con.execute("""
            SELECT normalized_code, list_sort(list(DISTINCT coalesce(vocabulary, '*') || '=' || choice))
            FROM recovery_adjudicated GROUP BY 1
            HAVING count(*) <> count(DISTINCT coalesce(vocabulary, '*'))
                OR (count(*) > 1 AND count(*) FILTER (WHERE vocabulary IS NULL) > 0)
            ORDER BY 1
        """).fetchall()
        if ambiguous:
            raise ValueError(
                f"--recovery-adjudication has {len(ambiguous)} code(s) matched by more than one "
                f"verdict, so no single verdict can be applied: "
                f"{[(c, v) for c, v in ambiguous[:5]]}. Give each code one row, or one row per "
                f"vocabulary; '*' above is a row with no vocabulary, which matches every one.")
        adjudication_meta = {"path": str(adjudication), "sha256": checksum(adjudication),
                             "verdicts": con.execute("SELECT count(*) FROM recovery_adjudicated").fetchone()[0]}
    con.execute("""
        CREATE TABLE recovery_resolved AS
        SELECT coalesce(x.normalized_code, s.normalized_code) AS normalized_code,
               coalesce(x.vocabulary, s.vocabulary) AS vocabulary,
               CASE
                 WHEN x.phecodes IS NOT NULL AND s.phecodes IS NOT NULL AND x.phecodes = s.phecodes
                   THEN 'both_routes_agree'
                 WHEN x.phecodes IS NOT NULL AND s.phecodes IS NOT NULL AND a.choice = 'A'
                   THEN 'adjudicated_cross_vocabulary'
                 WHEN x.phecodes IS NOT NULL AND s.phecodes IS NOT NULL AND a.choice = 'B'
                   THEN 'adjudicated_snomed_bridge'
                 WHEN x.phecodes IS NOT NULL AND s.phecodes IS NOT NULL
                   THEN 'skipped_unresolved_disagreement'
                 WHEN x.phecodes IS NOT NULL THEN 'cross_vocabulary'
                 ELSE 'snomed_bridge'
               END AS route,
               CASE
                 WHEN x.phecodes IS NOT NULL AND s.phecodes IS NOT NULL AND a.choice = 'B' THEN s.phecodes
                 WHEN x.phecodes IS NOT NULL THEN x.phecodes
                 ELSE s.phecodes
               END AS phecodes
        FROM recovery_cross x
        FULL OUTER JOIN recovery_snomed s
          ON s.normalized_code = x.normalized_code AND s.vocabulary = x.vocabulary
        LEFT JOIN recovery_adjudicated a
          ON a.normalized_code = coalesce(x.normalized_code, s.normalized_code)
         AND (a.vocabulary IS NULL OR a.vocabulary = coalesce(x.vocabulary, s.vocabulary))
    """)
    con.execute("""
        CREATE TABLE recovered_rows AS
        SELECT unnest(r.phecodes) AS phecode, c.concept_code AS source_code,
               r.vocabulary, r.normalized_code, r.route
        FROM recovery_resolved r
        JOIN recovery_candidates c
          ON c.normalized_code = r.normalized_code AND c.vocabulary = r.vocabulary
        WHERE r.route <> 'skipped_unresolved_disagreement'
    """)
    con.execute("""
        INSERT INTO icd_map
        SELECT DISTINCT phecode, source_code, vocabulary, normalized_code FROM recovered_rows
    """)
    con.execute(f"""
        COPY (SELECT normalized_code, source_code, vocabulary, phecode, route
              FROM recovered_rows ORDER BY vocabulary, normalized_code, phecode)
        TO '{quote(output / 'recovered_codes.csv')}' (HEADER, DELIMITER ',')
    """)
    # Two units, kept apart on purpose. A `codes_*` field counts distinct ICD code
    # STRINGS; an `assignments_*` field counts (code, vocabulary) pairs, which is what
    # the map actually gains -- one code recovered under both ICD10 and ICD10CM is one
    # code but two assignments. Reporting one number under a name that reads like the
    # other is how a 2,121 that should have been 2,204 went unnoticed.
    by_route = dict(con.execute(
        "SELECT route, count(DISTINCT normalized_code) FROM recovered_rows GROUP BY 1 ORDER BY 1").fetchall())
    by_route_assignments = dict(con.execute(
        "SELECT route, count(*) FROM (SELECT DISTINCT route, normalized_code, vocabulary "
        "FROM recovered_rows) GROUP BY 1 ORDER BY 1").fetchall())
    skipped_codes = con.execute(
        "SELECT count(DISTINCT normalized_code) FROM recovery_resolved"
        " WHERE route = 'skipped_unresolved_disagreement'").fetchone()[0]
    skipped = con.execute(
        "SELECT count(*) FROM recovery_resolved WHERE route = 'skipped_unresolved_disagreement'").fetchone()[0]
    unresolved = [r[0] for r in con.execute(
        "SELECT normalized_code FROM recovery_resolved WHERE route = 'skipped_unresolved_disagreement'"
        " ORDER BY normalized_code LIMIT 25").fetchall()]
    if skipped:
        print(f"phecodex-build: warning: {skipped} code(s) have conflicting recovery routes and were "
              f"NOT added; supply --recovery-adjudication to resolve them. First few: {unresolved[:10]}",
              file=sys.stderr)
    return {
        "rule": "codes absent from the published map are added only where the release itself supplies "
                "evidence -- another vocabulary's assignment, or a SNOMED concept the bridge accepted; "
                "conflicting routes are skipped unless adjudicated",
        "rows_added": con.execute("SELECT count(*) FROM recovered_rows").fetchone()[0],
        "codes_added": con.execute("SELECT count(DISTINCT normalized_code) FROM recovered_rows").fetchone()[0],
        "assignments_added": con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT normalized_code, vocabulary FROM recovered_rows)").fetchone()[0],
        "codes_added_by_route": by_route,
        "assignments_added_by_route": by_route_assignments,
        "codes_skipped_unresolved_disagreement": skipped_codes,
        "assignments_skipped_unresolved_disagreement": skipped,
        # The cross-vocabulary routes are justified by PhecodeX's own published map and
        # carry no Athena-derived content. These do not: they exist only because a
        # SNOMED concept in the Athena extract vouched for them. Recorded so a site can
        # see exactly how much of its map depends on a vocabulary it may not be
        # licensed to redistribute, and decide accordingly.
        "assignments_resting_solely_on_athena_evidence": con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT normalized_code, vocabulary FROM recovered_rows"
            " WHERE route IN ('snomed_bridge', 'adjudicated_snomed_bridge'))").fetchone()[0],
        "adjudication": adjudication_meta,
    }


def build_vocabulary(phecodex_map: Path | list[Path], phecodex_info: Path | None, output: Path,
                     athena_dir: Path | None = None, recover_unmapped: bool = False,
                     recovery_adjudication: Path | None = None, icd_only: bool = False) -> None:
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
    # icd_map is written after the recovery step below, so recovered rows are included.
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
        # --phecodex-info is documented as optional ("omit to default sex=Both and blank
        # text"), but omitting it used to ship no phecode_info.parquet at all -- and
        # verify_release.py requires that file, so a site that followed the docs produced
        # a release which failed the analyst's very first documented step. Honour the
        # documented default by materialising it instead of leaving a hole.
        # Phecode only. Do NOT synthesise sex/phecode_string/category columns: a `sex`
        # column full of 'Both' would make a release that carries no sex knowledge report
        # release_has_sex_metadata=true, hiding the very condition that flag exists to
        # expose -- every sex-specific phecode silently scored against the whole cohort.
        # Likewise a blank `category` column would let --exclude-phenotypes filter by a
        # category that is not really there. The file states what is known and no more.
        con.execute("""
            CREATE TABLE phecode_info_default AS
            SELECT DISTINCT phecode FROM icd_map ORDER BY phecode
        """)
        default_info = "SELECT * FROM phecode_info_default"
        con.execute(f"COPY ({default_info}) TO '{quote(output / 'phecode_info.parquet')}' (FORMAT PARQUET)")
        con.execute(f"COPY ({default_info}) TO '{quote(output / 'phecode_info.csv')}' (HEADER, DELIMITER ',')")
    info_join = "LEFT JOIN phecode_info i USING (phecode)" if phecodex_info and "phecode" in _columns(con, relation_for(phecodex_info)) else ""
    # phetk_custom_map.csv is exported after recovery, below, for the same reason.

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
        # Ordered so two builds from identical inputs are byte-identical. Federated
        # sites must be able to compare release checksums and conclude they hold the
        # same map; an unordered COPY makes every rebuild a different file.
        snomed_ordered = "SELECT * FROM snomed_map ORDER BY source_code, phecode"
        # --icd-only still BUILDS the bridge, because recovery needs it as evidence, but
        # ships none of it. The distinction matters: the bridge is how some recovered ICD
        # rows were justified, while snomed_map.* is Athena-derived content that the
        # analyst distribution must not redistribute.
        if not icd_only:
            con.execute(f"COPY ({snomed_ordered}) TO '{quote(output / 'snomed_map.parquet')}' (FORMAT PARQUET)")
            con.execute(f"COPY ({snomed_ordered}) TO '{quote(output / 'snomed_map.csv')}' (HEADER, DELIMITER ',')")
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

    recovery_summary: dict | None = None
    # Reviewing conflicts and then not running recovery cannot be what anyone meant,
    # and silently ignoring the file loses the reviewer's work with no signal at all.
    if recovery_adjudication and not recover_unmapped:
        raise ValueError("--recovery-adjudication has no effect without --recover-unmapped: "
                         "the verdicts resolve conflicts between the two recovery routes, and "
                         "without recovery there are no conflicts to resolve.")
    if recover_unmapped:
        # Both routes need the Athena views (`concept`, `relationship`) and the SNOMED
        # bridge, all of which only exist when --athena-dir was supplied.
        if not athena_dir:
            raise ValueError("--recover-unmapped requires --athena-dir: both recovery routes need "
                             "the Athena vocabulary to enumerate codes and resolve SNOMED mappings.")
        recovery_summary = _recover_unmapped_codes(con, output, athena_dir, recovery_adjudication)

    # Adapter only: enables black-box parity tests; it is not a source of exclusions.
    con.execute(f"""
        COPY (SELECT m.phecode, m.source_code AS ICD,
          CASE WHEN m.vocabulary='ICD9CM' THEN 9 ELSE 10 END AS flag,
          {sex} AS sex, {description} AS phecode_string, {category} AS phecode_category,
          '' AS exclude_range
          FROM icd_map m {info_join}
          ORDER BY m.vocabulary, m.normalized_code, m.phecode, m.source_code)
        TO '{quote(output / 'phetk_custom_map.csv')}' (HEADER, DELIMITER ',')
    """)

    # Written here, not earlier, so recovered rows are in every shipped artefact.
    icd_ordered = "SELECT * FROM icd_map ORDER BY vocabulary, normalized_code, phecode, source_code"
    con.execute(f"COPY ({icd_ordered}) TO '{quote(output / 'icd_map.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY ({icd_ordered}) TO '{quote(output / 'icd_map.csv')}' (HEADER, DELIMITER ',')")

    icd_rows = con.execute("SELECT * FROM icd_map ORDER BY vocabulary, normalized_code, phecode").fetchall()
    sheets = {"ICD map": (["phecode", "source_code", "vocabulary", "normalized_code"], icd_rows)}
    if not icd_only:
        sheets["SNOMED bridge"] = (["source_code", "vocabulary", "phecode", "bridge_icd_code",
                                    "bridge_vocabulary", "source_icd_code_count"], snomed_rows)
    _write_xlsx(output / "phecodex_reference_maps.xlsx", sheets)
    manifest = {
        "tool_version": __version__, "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "phecodex_map": ([{"path": str(path), "sha256": checksum(path)} for path in map_paths]
                         if len(map_paths) > 1 else {"path": str(map_paths[0]), "sha256": checksum(map_paths[0])}),
        "phecodex_info": None if not phecodex_info else {"path": str(phecodex_info), "sha256": checksum(phecodex_info)},
        "athena_dir": None if not athena_dir else str(athena_dir),
        "counts": {"icd_map_rows": len(icd_rows),
                   "snomed_map_rows": 0 if icd_only else len(snomed_rows)},
        # True means the SNOMED bridge was built and used as recovery evidence but its
        # tables were deliberately not shipped. A site reading this knows the absence of
        # snomed_map.* is a decision, not a failed build.
        "icd_only": icd_only,
        "snomed_bridge_rows_built_but_withheld": len(snomed_rows) if icd_only else 0,
        "snomed_bridge": snomed_summary,
        # Present only when --recover-unmapped was used. Its absence means the map is
        # exactly what the published PhecodeX files contain.
        "recovery": recovery_summary,
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
        # coalesce because --recover-unmapped can introduce a vocabulary whose rows are
        # ALL recovered, so no published source file backs it; the subquery is then NULL
        # rather than empty. An empty source_files list is the honest answer there, and
        # the recovery block below accounts for where those rows came from.
        "vocabularies": {
            vocabulary: {"rows": rows, "distinct_codes": codes, "source_files": sorted(files or [])}
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
