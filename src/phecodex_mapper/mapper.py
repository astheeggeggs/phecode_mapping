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


# DuckDB type names that carry a code or identifier without losing characters.
TEXT_TYPES = {"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR"}


def _column_types(con, source: str) -> dict[str, str]:
    return {r[0]: str(r[1]).upper() for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}


def validate_cohort_and_events(con, cohort_src: str, event_src: str) -> dict:
    """Validate the analyst's two input files and return descriptive counts.

    Lives here, and is called by BOTH map_phecodes and workflow.preflight, because
    these checks previously existed only in preflight -- so `map-phecodes`, which
    README documents as available to advanced users, accepted input that `run`
    rejects and produced silently wrong output from it.
    """
    cohort_types, event_types = _column_types(con, cohort_src), _column_types(con, event_src)
    for name, types, required in (("cohort", cohort_types, {"person_id", "sex"}),
                                  ("events", event_types, {"person_id", "code", "vocabulary"})):
        missing = required - set(types)
        if missing:
            raise ValueError(f"{name} is missing required columns: {sorted(missing)}")

    cohort_rows = con.execute(f"SELECT count(*) FROM {cohort_src}").fetchone()[0]
    duplicate_people = con.execute(f"SELECT count(*) - count(DISTINCT person_id) FROM {cohort_src}").fetchone()[0]
    null_people = con.execute(
        f"SELECT count(*) FROM {cohort_src} WHERE person_id IS NULL OR trim(CAST(person_id AS VARCHAR)) = ''").fetchone()[0]
    if duplicate_people or null_people:
        raise ValueError(f"cohort person_id must be non-null and unique "
                         f"(duplicates={duplicate_people}, null_or_blank={null_people})")
    bad_sex = con.execute(
        f"SELECT DISTINCT sex FROM {cohort_src} WHERE sex IS NOT NULL"
        f" AND upper(trim(CAST(sex AS VARCHAR))) NOT IN ('MALE', 'FEMALE')").fetchall()
    if bad_sex:
        raise ValueError(f"cohort sex must be Male, Female, or missing; found: {[r[0] for r in bad_sex[:10]]}")

    event_rows = con.execute(f"SELECT count(*) FROM {event_src}").fetchone()[0]
    missing_event_fields = con.execute(
        f"SELECT count(*) FROM {event_src} WHERE person_id IS NULL OR code IS NULL OR vocabulary IS NULL").fetchone()[0]
    if missing_event_fields:
        raise ValueError(f"events contain {missing_event_fields} rows missing person_id, code, or vocabulary")
    vocab_counts = {str(r[0]): r[1] for r in con.execute(
        f"SELECT upper(trim(CAST(vocabulary AS VARCHAR))), count(*) FROM {event_src} GROUP BY 1 ORDER BY 1").fetchall()}
    unsupported = sorted(set(vocab_counts) - {"ICD9CM", "ICD10", "ICD10CM", "SNOMED"})
    if unsupported:
        raise ValueError(f"events contain unsupported vocabularies: {unsupported}")

    # CSV is read with all_varchar=true, but Parquet keeps its native types, so a
    # Parquet events file can arrive with a numeric `code`. CAST(code AS VARCHAR) then
    # loses information in both directions: INTEGER 001 -> '1' (unmappable), and DOUBLE
    # 250.00 -> '250.0', which normalizes to '2500' -- a DIFFERENT real ICD-9 code,
    # mapped to the wrong phecode with no unmapped-event record. Neither is recoverable
    # downstream, so the only safe contract is to require text.
    code_type = event_types.get("code", "")
    if code_type and not any(code_type.startswith(t) for t in TEXT_TYPES):
        raise ValueError(
            f"events 'code' column has type {code_type}; it must be text. Numeric code columns "
            "lose leading zeros ('001' -> '1') and trailing decimal zeros ('250.00' -> '250.0', "
            "which normalizes to the different code '2500'). Re-export the column as a string.")
    cohort_is_text = any(cohort_types.get("person_id", "").startswith(t) for t in TEXT_TYPES)
    events_is_text = any(event_types.get("person_id", "").startswith(t) for t in TEXT_TYPES)
    if cohort_types.get("person_id") and event_types.get("person_id") and cohort_is_text != events_is_text:
        raise ValueError(
            f"cohort person_id is {cohort_types['person_id']} but events person_id is "
            f"{event_types['person_id']}. Mixing text and numeric identifiers across the two files "
            "makes the join depend on type coercion. Export both as the same type.")

    unknown_people = con.execute(
        f"SELECT count(*) FROM {event_src} e LEFT JOIN {cohort_src} c USING (person_id)"
        f" WHERE c.person_id IS NULL").fetchone()[0]
    # Events for people outside the cohort are legitimately dropped -- but if *every*
    # event is dropped the two files do not describe the same population, and the run
    # would otherwise complete reporting 0 events and a 0.0 unmapped rate, passing even
    # --max-unmapped-rate 0.0.
    if event_rows and unknown_people == event_rows:
        raise ValueError(
            f"none of the {event_rows} event rows match a person in the cohort; the two files "
            "appear to use different person_id formats or describe different populations")

    # try_cast yields NULL for anything that is not an ISO date, so a locale format
    # silently disables the two-dates case rule instead of failing.
    unparseable_dates = 0
    if "event_date" in event_types:
        unparseable_dates = con.execute(
            f"SELECT count(*) FROM {event_src} WHERE event_date IS NOT NULL"
            f" AND trim(CAST(event_date AS VARCHAR)) <> '' AND try_cast(event_date AS DATE) IS NULL").fetchone()[0]

    return {"cohort_rows": cohort_rows, "event_rows": event_rows,
            "event_rows_missing_required_fields": missing_event_fields,
            "vocabulary_counts": vocab_counts, "events_for_unknown_people": unknown_people,
            "event_rows_with_unparseable_date": unparseable_dates}


def _reject_missing(con, view: str, column: str, label: str) -> None:
    """Refuse a NULL or blank in a column that is joined on.

    Same three-valued-logic trap as _reject_unusable_values, but for free-text
    columns with no fixed vocabulary to check against: `x.phecode = m.phecode` is
    UNKNOWN when either side is NULL, so the rule silently matches nothing.
    """
    missing = con.execute(
        f"SELECT count(*) FROM {view} WHERE {column} IS NULL OR trim({column}) = ''").fetchone()[0]
    if missing:
        raise ValueError(
            f"{label} has {missing} row(s) with an empty {column}. A blank here silently voids the "
            "whole rule rather than failing, so it is refused.")


def _reject_unusable_values(con, view: str, column: str, allowed: tuple[str, ...], label: str) -> None:
    """Reject rows whose `column` is missing or outside `allowed`.

    `WHERE col NOT IN (...)` is the obvious way to find bad rows and it silently
    misses the worst input. SQL three-valued logic makes `NULL NOT IN ('a','b')`
    UNKNOWN rather than TRUE, so a blank cell is never selected and passes
    validation -- and the join that later applies the rule is UNKNOWN for the same
    reason, so the rule matches nothing and evaporates without an error. A typo
    raises; an empty cell is ignored. This is the same three-valued-logic defect
    already fixed in the exclusion *filter*; these *validation* guards kept it.

    Blank and unrecognised are reported separately because they call for different
    corrections -- "you left this empty" versus "you misspelled this".
    """
    missing = con.execute(
        f"SELECT count(*) FROM {view} WHERE {column} IS NULL OR trim({column}) = ''").fetchone()[0]
    if missing:
        raise ValueError(
            f"{label} has {missing} row(s) with an empty {column}. A blank here silently voids the "
            f"whole rule rather than failing, so it is refused. Expected one of: {', '.join(allowed)}.")
    unrecognised = [r[0] for r in con.execute(
        f"SELECT DISTINCT {column} FROM {view} WHERE {column} NOT IN {allowed}").fetchall()]
    if unrecognised:
        raise ValueError(
            f"{label} {column} must be one of {', '.join(allowed)}, got: {sorted(unrecognised)}")


def _load_excluded_phecodes(con, release: Path, exclude_phenotypes: Path | None) -> dict:
    """Build the `excluded_phecodes` table: phecodes dropped from every output
    (phecode_counts, person_phecodes, eligible_phecodes, phenotype_matrix)
    entirely -- e.g. whole categories with poor genetic construct validity for
    a given analysis (see phecodex_mapper/data/recommended_exclusions.csv), or
    specific phecodes. Distinct from --control-exclusions, which only adjusts
    the control pool for *other* phecodes.
    """
    con.execute("CREATE TABLE excluded_phecodes(phecode VARCHAR)")
    if not exclude_phenotypes:
        return {"phecodes_excluded": 0, "unmatched_category_rules": []}
    ex_src = relation_for(exclude_phenotypes)
    cols = _columns(con, ex_src)
    required = {"match_type", "match_value"}
    if required - cols:
        raise ValueError(f"--exclude-phenotypes missing columns: {sorted(required - cols)}")
    # Canonicalize on load, for the same reason as --control-exclusions: a value that
    # passes a case-insensitive check and is then matched case-sensitively silently
    # excludes nothing. Category comparison is case- and whitespace-insensitive below.
    con.execute(f"""
      CREATE VIEW exclude_phenotypes_input AS
      SELECT lower(trim(CAST(match_type AS VARCHAR))) AS match_type,
             trim(CAST(match_value AS VARCHAR)) AS match_value
      FROM {ex_src}
    """)
    _reject_unusable_values(con, "exclude_phenotypes_input", "match_type",
                            ("category", "phecode"), "--exclude-phenotypes")
    # A NULL or blank match_value would put a NULL into excluded_phecodes, and SQL's
    # three-valued logic then makes `phecode NOT IN (...)` UNKNOWN for every row --
    # silently dropping every phecode from every output. Refuse it at the door; the
    # filter itself is also made NULL-safe in _build_cases_and_counts.
    blank_values = con.execute(
        "SELECT count(*) FROM exclude_phenotypes_input WHERE match_value IS NULL OR match_value = ''"
    ).fetchone()[0]
    if blank_values:
        raise ValueError(f"--exclude-phenotypes has {blank_values} row(s) with a blank match_value; "
                         "remove them or supply the category/phecode each rule should match")
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
        ON x.match_type = 'category'
        AND upper(x.match_value) = upper(trim(CAST(pi.category AS VARCHAR)))
    """ if info_view else ""))
    # A category rule matching nothing is legitimate (a release need not contain every
    # category the policy names) but it is also what a typo or a casing mismatch looks
    # like, and previously it was invisible -- the run proceeded as though the exclusion
    # had applied. Surface it rather than failing on it.
    unmatched = []
    if info_view:
        unmatched = sorted(r[0] for r in con.execute(f"""
          SELECT DISTINCT x.match_value FROM exclude_phenotypes_input x
          WHERE x.match_type = 'category' AND NOT EXISTS (
            SELECT 1 FROM {info_view} pi
            WHERE upper(x.match_value) = upper(trim(CAST(pi.category AS VARCHAR))))
        """).fetchall())
        if unmatched:
            print(f"phecodex-map: warning: --exclude-phenotypes category rule(s) matched no phecode in this "
                  f"release: {unmatched}. Check them against the release's phecode_info 'category' values.",
                  file=sys.stderr)
    # The same check for 'phecode' rules, which had none. A rule naming a phecode the
    # release does not contain -- a typo, or the right identifier in the wrong case --
    # was inserted into excluded_phecodes regardless, excluding nothing while inflating
    # phecodes_excluded. The audit then reported phenotypes as dropped that were never
    # dropped, which is worse than silence: it reads as confirmation the rule worked.
    known = "SELECT phecode FROM icd_map UNION SELECT phecode FROM excluded_phecodes WHERE FALSE"
    if info_view:
        known += f" UNION SELECT phecode FROM {info_view}"
    unmatched_phecodes = sorted(r[0] for r in con.execute(f"""
      SELECT DISTINCT x.match_value FROM exclude_phenotypes_input x
      WHERE x.match_type = 'phecode' AND x.match_value NOT IN ({known})
    """).fetchall())
    if unmatched_phecodes:
        print(f"phecodex-map: warning: --exclude-phenotypes phecode rule(s) name no phecode in this release: "
              f"{unmatched_phecodes}. Matching is case-sensitive; check them against the release's phecodes.",
              file=sys.stderr)
    return {
        # Counts phecodes the release actually has, not rules supplied. Previously a
        # rule naming nothing still incremented this.
        "phecodes_excluded": con.execute(
            f"SELECT count(*) FROM excluded_phecodes WHERE phecode IN ({known})").fetchone()[0],
        "unmatched_category_rules": unmatched,
        "unmatched_phecode_rules": unmatched_phecodes,
    }


def _load_phecode_sex(con, release: Path) -> dict:
    """Build `phecode_sex` (only genuinely restricted phecodes) and `cohort_sex_counts`.

    A person is *evaluable* for a phecode when the phecode carries no sex
    restriction, or their cohort sex matches it; unknown sex is never evaluable
    for a restricted phecode. This is the single definition of evaluability --
    cases, counts, retention and the phenotype matrix all derive from it, so
    eligible_phecodes.xlsx can no longer claim controls the matrix does not have.
    """
    con.execute("CREATE TABLE phecode_sex(phecode VARCHAR, restrict_sex VARCHAR)")
    info_path = release / "phecode_info.parquet"
    # Whether the RELEASE carries sex metadata at all. A release built without
    # --phecodex-info (or from the upstream phecodeX_info.csv, which has no `sex`
    # column) leaves phecode_sex empty, EVALUABLE degenerates to TRUE for everyone,
    # and every sex-specific phecode is silently scored against the whole cohort --
    # exactly the defect the sex fix was written to remove. Report it so the caller
    # can say so out loud rather than discovering it in the effect estimates.
    release_has_sex = False
    if info_path.exists():
        info_view = f"read_parquet('{quote(info_path)}')"
        if {"phecode", "sex"} <= _columns(con, info_view):
            release_has_sex = True
            con.execute(f"""
              INSERT INTO phecode_sex
              SELECT phecode, upper(trim(sex)) FROM {info_view}
              WHERE upper(trim(sex)) IN ('MALE', 'FEMALE')
            """)
    con.execute("""
      CREATE TABLE cohort_sex_counts AS
      SELECT count(*) AS n_all,
             count(*) FILTER (WHERE sex = 'MALE') AS n_male,
             count(*) FILTER (WHERE sex = 'FEMALE') AS n_female
      FROM cohort
    """)
    n_all, n_male, n_female = con.execute(
        "SELECT n_all, n_male, n_female FROM cohort_sex_counts").fetchone()
    return {
        "release_has_sex_metadata": release_has_sex,
        "n_restricted_phecodes": con.execute("SELECT count(*) FROM phecode_sex").fetchone()[0],
        "n_male": n_male, "n_female": n_female,
        # Neither MALE nor FEMALE: blank, NULL, or an unrecognised encoding. These
        # people are correctly non-evaluable for every sex-restricted phecode, but
        # without this number a case count deflated by the unknown-sex fraction is
        # indistinguishable from a genuinely rare phenotype.
        "n_unknown_sex": n_all - n_male - n_female,
    }


# A person counts toward a phecode only if the phecode is unrestricted or their sex matches.
EVALUABLE = "(ps.restrict_sex IS NULL OR c.sex = ps.restrict_sex)"


def _apply_control_exclusions(con, target_table: str, mapped_table: str) -> None:
    """Populate a control-exclusion table from the normalized `exclusions_input`.

    Values in exclusions_input are already canonical (see map_phecodes), so the only
    normalization here is ICD punctuation stripping, which must match the
    normalized_events expression.
    """
    con.execute(f"""
      INSERT INTO {target_table}
      SELECT DISTINCT m.person_id, x.phecode FROM exclusions_input x JOIN {mapped_table} m
        ON x.exclusion_type = 'phecode' AND x.exclusion_value = m.phecode
      UNION
      SELECT DISTINCT e.person_id, x.phecode FROM exclusions_input x JOIN normalized_events e
        ON x.exclusion_type = 'code'
        AND x.vocabulary = e.vocabulary
        AND regexp_replace(upper(x.exclusion_value), '[.\\s-]', '', 'g') = e.normalized_code
    """)


def _build_cases_and_counts(con, *, mapped_table: str, exclusions_table: str,
                            case_rule: str, min_cases: int, min_controls: int) -> None:
    """Create the cases, all_phecodes and phecode_counts tables.

    Denominators are the *evaluable* cohort for each phecode, not the whole
    cohort: a Female-only phecode is scored against females only, so its control
    count and its `retained` verdict describe the same people the matrix scores.

    Deliberately avoids `all_phecodes CROSS JOIN cohort`: at real-world scale
    (thousands of phecodes x hundreds of thousands of people) that intermediate is
    hundreds of millions of rows before any filtering happens. Instead, aggregate
    case counts and (non-case) exclusion counts per phecode first, then join those
    small per-phecode summaries onto all_phecodes, with the evaluable cohort size
    supplied by three whole-cohort tallies -- cost scales with cases+exclusions
    rows, not cohort_size x phecode_count.
    """
    cases, all_phecodes = "cases", "all_phecodes"
    counts, eligible = "phecode_counts", "phecode_eligible"
    # NOT EXISTS rather than NOT IN: with NOT IN, a single NULL in excluded_phecodes
    # makes the predicate UNKNOWN for every row and silently empties every output.
    not_excluded = "NOT EXISTS (SELECT 1 FROM excluded_phecodes x WHERE x.phecode = m.phecode)"
    joins = (f"JOIN cohort c ON c.person_id = m.person_id "
             f"LEFT JOIN phecode_sex ps ON ps.phecode = m.phecode")
    if case_rule == "any-event":
        con.execute(f"""
          CREATE TABLE {cases} AS
          SELECT DISTINCT m.person_id, m.phecode FROM {mapped_table} m {joins}
          WHERE {not_excluded} AND {EVALUABLE}
        """)
    else:
        con.execute(f"""
          CREATE TABLE {cases} AS
          SELECT m.person_id, m.phecode FROM {mapped_table} m {joins}
          WHERE m.event_date IS NOT NULL AND {not_excluded} AND {EVALUABLE}
          GROUP BY m.person_id, m.phecode HAVING count(DISTINCT m.event_date) >= 2
        """)
    # A person carrying the phecode but not meeting the case rule is ambiguous
    # evidence, not evidence of absence: they must not be scored as a clean control.
    # This matches PheTK, whose control set excludes everyone appearing in
    # phecode_counts for the phecode with no count threshold applied
    # (PheWAS._case_control_prep: `exclude_range = [phecode] + ...`), so a person
    # below min_phecode_count is neither case nor control.
    #
    # Under --case-rule any-event one event already makes a case, so this table is
    # always empty there and the default behaviour is unchanged; it bites only
    # under two-dates.
    con.execute(f"""
      CREATE TABLE subthreshold AS
      SELECT DISTINCT m.person_id, m.phecode FROM {mapped_table} m {joins}
      WHERE {not_excluded} AND {EVALUABLE}
        AND NOT EXISTS (SELECT 1 FROM {cases} ca
                        WHERE ca.person_id = m.person_id AND ca.phecode = m.phecode)
    """)
    # Everyone removed from a phecode's control pool, for whatever reason: the
    # sub-threshold carriers above, plus evaluable non-cases named by
    # --control-exclusions. The matrix scores these NA; the counts report the two
    # reasons separately so the control denominator stays explainable.
    con.execute(f"""
      CREATE TABLE noncase_excluded AS
      SELECT person_id, phecode FROM subthreshold
      UNION
      SELECT m.person_id, m.phecode FROM {exclusions_table} m {joins}
      LEFT JOIN {cases} ca ON ca.phecode = m.phecode AND ca.person_id = m.person_id
      WHERE ca.person_id IS NULL AND {EVALUABLE}
    """)
    con.execute(f"CREATE TABLE {all_phecodes} AS SELECT DISTINCT m.phecode AS phecode FROM {mapped_table} m WHERE {not_excluded}")
    con.execute(f"""
      CREATE TABLE {eligible} AS
      SELECT p.phecode,
             CASE ps.restrict_sex WHEN 'MALE' THEN s.n_male WHEN 'FEMALE' THEN s.n_female
                  ELSE s.n_all END AS eligible_count
      FROM {all_phecodes} p LEFT JOIN phecode_sex ps USING (phecode) CROSS JOIN cohort_sex_counts s
    """)
    con.execute(f"""
      CREATE TABLE {counts} AS
      SELECT p.phecode,
        coalesce(cc.case_count, 0) AS case_count,
        el.eligible_count - coalesce(cc.case_count, 0) AS control_count_before_exclusions,
        coalesce(ec.excluded_count, 0) AS excluded_control_count,
        coalesce(sc.subthreshold_count, 0) AS subthreshold_control_count,
        el.eligible_count - coalesce(cc.case_count, 0) - coalesce(ne.removed_count, 0) AS control_count_after_exclusions
      FROM {all_phecodes} p
      JOIN {eligible} el ON el.phecode = p.phecode
      LEFT JOIN (SELECT phecode, count(DISTINCT person_id) AS case_count FROM {cases} GROUP BY phecode) cc
        ON cc.phecode = p.phecode
      LEFT JOIN (SELECT phecode, count(DISTINCT person_id) AS removed_count FROM noncase_excluded GROUP BY phecode) ne
        ON ne.phecode = p.phecode
      LEFT JOIN (SELECT phecode, count(DISTINCT person_id) AS subthreshold_count FROM subthreshold GROUP BY phecode) sc
        ON sc.phecode = p.phecode
      LEFT JOIN (
        SELECT m.phecode, count(DISTINCT m.person_id) AS excluded_count
        FROM {exclusions_table} m {joins}
        LEFT JOIN {cases} ca ON ca.phecode = m.phecode AND ca.person_id = m.person_id
        WHERE ca.person_id IS NULL AND {EVALUABLE}
        GROUP BY m.phecode
      ) ec ON ec.phecode = p.phecode
    """)
    con.execute(f"ALTER TABLE {counts} ADD COLUMN retained BOOLEAN; UPDATE {counts} SET retained = case_count >= {int(min_cases)} AND control_count_after_exclusions >= {int(min_controls)}")


def map_phecodes(release: Path, cohort: Path, events: Path, output: Path, case_rule: str = "any-event", exclusions: Path | None = None, min_cases: int = 200, min_controls: int = 200, max_unmapped_rate: float = 1.0, exclude_phenotypes: Path | None = None) -> None:
    """Map a cohort's events to phecodes by exact match against the release's map.

    Mapping is exact only. The published PhecodeX map is already unrolled to leaf
    level wherever a phecode is assigned, so a code's absence from it is a curation
    decision rather than a gap to be filled: the unmapped branches are dominated by
    trauma sequelae, iatrogenic complications and status codes, whose mapped
    ancestors are disease phenotypes. Inferring them from a parent would assign, for
    example, 'retained intraocular foreign body' to 'Disorders of globe'. Where the
    published map genuinely lags the ICD release, the defensible remedy is a curated,
    versioned supplement to the map -- explicit and auditable -- not run-time
    inference that no external reviewer can check.
    """
    if case_rule not in {"any-event", "two-dates"}:
        raise ValueError("case_rule must be any-event or two-dates")
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}. Remove it or choose a new --output path.")
    output.mkdir(parents=True)
    con = connect(); cohort_src = relation_for(cohort); event_src = relation_for(events)
    # Applied here, not only in workflow.preflight, so the `map-phecodes` subcommand
    # gets the same guarantees as the documented `run` workflow.
    validate_cohort_and_events(con, cohort_src, event_src)
    # validate_cohort_and_events guarantees person_id/sex on the cohort and
    # person_id/code/vocabulary on the events, so the duplicate column checks that
    # used to sit here (with their own divergent wording) are gone. The `sex` column
    # is therefore always present; whether it holds any USABLE values is a separate
    # question, answered after _load_phecode_sex below.
    if case_rule == "two-dates" and "event_date" not in _columns(con, event_src): raise ValueError("two-dates requires event_date")
    if case_rule == "two-dates":
        # try_cast returns NULL for a non-ISO date rather than failing, and the
        # two-dates rule then never sees a second date -- silently demoting genuine
        # repeat-coded cases to clean controls, with unmapped_rate still 0.0.
        unparseable = con.execute(
            f"SELECT count(*) FROM {event_src} WHERE event_date IS NOT NULL"
            f" AND trim(CAST(event_date AS VARCHAR)) <> '' AND try_cast(event_date AS DATE) IS NULL"
        ).fetchone()[0]
        if unparseable:
            example = con.execute(
                f"SELECT DISTINCT CAST(event_date AS VARCHAR) FROM {event_src} WHERE event_date IS NOT NULL"
                f" AND trim(CAST(event_date AS VARCHAR)) <> '' AND try_cast(event_date AS DATE) IS NULL LIMIT 3"
            ).fetchall()
            raise ValueError(
                f"--case-rule two-dates requires parseable dates, but {unparseable} event row(s) have an "
                f"event_date that is not an ISO date, e.g. {[r[0] for r in example]}. These would silently "
                "be treated as undated and their carriers counted as controls. Convert event_date to YYYY-MM-DD.")
    con.execute(f"CREATE VIEW cohort_input AS SELECT * FROM {cohort_src}")
    invalid = con.execute("SELECT count(*) FROM cohort_input WHERE person_id IS NULL").fetchone()[0]
    duplicate = con.execute("SELECT count(*) - count(DISTINCT person_id) FROM cohort_input").fetchone()[0]
    if invalid or duplicate: raise ValueError("Cohort person_id must be non-null and unique")
    # The column is guaranteed present by validate_cohort_and_events; blank/NULL/
    # unrecognised values normalise to something outside ('MALE','FEMALE') and are
    # counted as n_unknown_sex, which is what makes them non-evaluable rather than
    # silently evaluable.
    sex_expression = "upper(trim(sex))"
    # Cast person_id to VARCHAR on both sides so the join does not depend on DuckDB's
    # type coercion, which resolved differently in different joins of the same run.
    con.execute(f"CREATE TABLE cohort AS SELECT CAST(person_id AS VARCHAR) AS person_id, {sex_expression} AS sex FROM cohort_input")
    con.execute(f"CREATE VIEW events_input AS SELECT * FROM {event_src}")
    date_expression = "try_cast(e.event_date AS DATE)" if "event_date" in _columns(con, event_src) else "CAST(NULL AS DATE)"
    con.execute(f"""
      CREATE TABLE normalized_events AS
      SELECT row_number() OVER () AS event_id, c.person_id,
        upper(trim(CAST(e.vocabulary AS VARCHAR))) AS vocabulary,
        CAST(e.code AS VARCHAR) AS source_code,
        CASE WHEN upper(trim(CAST(e.vocabulary AS VARCHAR))) IN ('ICD9CM','ICD10CM','ICD10')
             THEN regexp_replace(upper(trim(CAST(e.code AS VARCHAR))), '[.\\s-]', '', 'g')
             WHEN upper(trim(CAST(e.vocabulary AS VARCHAR))) = 'SNOMED' THEN regexp_replace(trim(CAST(e.code AS VARCHAR)), '\\s+', '', 'g')
        ELSE NULL END AS normalized_code,
        {date_expression} AS event_date
      FROM events_input e JOIN cohort c ON c.person_id = CAST(e.person_id AS VARCHAR)
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
    exclusion_summary = _load_excluded_phecodes(con, release, exclude_phenotypes)
    sex_summary = _load_phecode_sex(con, release)
    # has_sex is DERIVED, never asserted. It was previously a hardwired True, which
    # made both audit fields below constants and the warning branch unreachable --
    # so the two signals designed to flag a broken sex configuration could not fire.
    has_sex = (sex_summary["n_male"] + sex_summary["n_female"]) > 0
    if sex_summary["n_restricted_phecodes"] and not has_sex:
        # Every sex-restricted phecode would score 0 cases and 0 controls, drop out
        # of the matrix entirely, and leave no trace in stdout or the audit. Refuse
        # rather than emit a matrix that is silently missing 322 phenotypes.
        raise ValueError(
            f"cohort has no usable sex values ({sex_summary['n_unknown_sex']} of "
            f"{sex_summary['n_unknown_sex']} rows are blank, NULL or unrecognised), but the release "
            f"restricts {sex_summary['n_restricted_phecodes']} phecode(s) by sex. Every one of them "
            "would be scored as 0 cases / 0 controls and silently dropped from the phenotype matrix. "
            "Expected 'Male'/'Female' (case-insensitive). Fix the cohort sex column, or use a release "
            "without sex metadata if you genuinely intend every phecode to be unrestricted.")
    if not sex_summary["release_has_sex_metadata"]:
        print("phecodex-map: warning: the release's phecode_info has no 'sex' column, so NO phecode is "
              "sex-restricted and every sex-specific phenotype will be scored against the whole cohort. "
              "Rebuild the release with --phecodex-info <file with a sex column> to restore sex "
              "restrictions. Recorded as release_has_sex_metadata=false in audit.json.", file=sys.stderr)
    con.execute("CREATE TABLE exclusions(person_id VARCHAR, phecode VARCHAR)")
    con.execute("CREATE TABLE exclusions_input(phecode VARCHAR, exclusion_type VARCHAR, exclusion_value VARCHAR, vocabulary VARCHAR)")
    exclusion_version = None
    if exclusions:
        ex_src = relation_for(exclusions); cols = _columns(con, ex_src)
        need = {"phecode", "exclusion_type", "exclusion_value", "vocabulary"}
        if need - cols: raise ValueError(f"Exclusions missing columns: {sorted(need-cols)}")
        con.execute("DROP TABLE exclusions_input")
        # Normalize once, here, rather than at each consumption site. The validation
        # below is case-insensitive, so anything the consumers compare case-sensitively
        # would pass validation and then silently match nothing -- which is exactly how
        # 'Code' used to void an entire exclusion policy without an error. Storing the
        # canonical form means the four downstream joins cannot drift from the checks.
        version_column = ", version" if "version" in cols else ""
        con.execute(f"""
          CREATE TABLE exclusions_input AS
          SELECT trim(CAST(phecode AS VARCHAR)) AS phecode,
                 lower(trim(CAST(exclusion_type AS VARCHAR))) AS exclusion_type,
                 trim(CAST(exclusion_value AS VARCHAR)) AS exclusion_value,
                 upper(trim(CAST(vocabulary AS VARCHAR))) AS vocabulary
                 {version_column}
          FROM {ex_src}
        """)
        _reject_unusable_values(con, "exclusions_input", "exclusion_type",
                                ("phecode", "code"), "--control-exclusions")
        _reject_unusable_values(con, "exclusions_input", "vocabulary",
                                ("ICD9CM", "ICD10CM", "ICD10", "SNOMED"), "--control-exclusions")
        # phecode and exclusion_value are equally load-bearing: a blank in either
        # makes the join UNKNOWN and voids the rule just as quietly.
        _reject_missing(con, "exclusions_input", "phecode", "--control-exclusions")
        _reject_missing(con, "exclusions_input", "exclusion_value", "--control-exclusions")
        if "version" in cols: exclusion_version = con.execute("SELECT min(version) FROM exclusions_input").fetchone()[0]
        _apply_control_exclusions(con, "exclusions", "mapped_events")
    _build_cases_and_counts(con, mapped_table="mapped_events", exclusions_table="exclusions",
                            case_rule=case_rule, min_cases=min_cases, min_controls=min_controls)
    con.execute(f"COPY phecode_counts TO '{quote(output / 'phecode_counts.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY phecode_counts TO '{quote(output / 'phecode_counts.csv')}' (HEADER, DELIMITER ',')")
    con.execute(f"COPY (SELECT * FROM cases) TO '{quote(output / 'person_phecodes.parquet')}' (FORMAT PARQUET)")
    con.execute(f"COPY (SELECT * FROM unmapped_events) TO '{quote(output / 'unmapped_events.csv')}' (HEADER, DELIMITER ',')")
    rows = con.execute("SELECT * FROM phecode_counts WHERE retained ORDER BY phecode").fetchall()
    headers = [r[0] for r in con.execute("DESCRIBE phecode_counts").fetchall()]
    _xlsx(output / "eligible_phecodes.xlsx", headers, rows)

    matrix_info = _write_phenotype_matrix(con, release, output, has_sex)

    total = con.execute("SELECT count(*) FROM normalized_events").fetchone()[0]
    unmapped = con.execute("SELECT count(*) FROM unmapped_events").fetchone()[0]
    rate = unmapped / total if total else 0
    audit = {"created_at_utc": dt.datetime.now(dt.UTC).isoformat(), "release": str(release), "case_rule": case_rule,
             "min_cases": min_cases, "min_controls": min_controls, "exclusion_version": exclusion_version,
             "exclude_phenotypes": None if not exclude_phenotypes else {"file": str(exclude_phenotypes), **exclusion_summary},
             "events": total, "unmapped_events": unmapped, "unmapped_rate": rate,
             "mapping_policy": "exact-match-against-published-map",
             # Every field here is derived. release_has_sex_metadata=false means NO
             # phecode was sex-restricted; n_unknown_sex is the count of people who
             # are non-evaluable for every restricted phecode, without which a
             # deflated case count is indistinguishable from a rare phenotype.
             "sex": sex_summary,
             "release_manifest_sha256": checksum(release / "manifest.json"),
             "phenotype_matrix": matrix_info}
    (output / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    if rate > max_unmapped_rate:
        raise RuntimeError(f"Unmapped rate {rate:.3%} exceeds threshold {max_unmapped_rate:.3%}")


def _write_phenotype_matrix(con, release: Path, output: Path, has_sex: bool) -> dict:
    """Write a wide person x phecode matrix: 1 = case, 0 = control, NA = not
    evaluable (sex-restricted phecode and person's sex doesn't match / is
    unknown, or the person is covered by a control exclusion for that
    phecode without being a case). Columns are restricted to retained
    phecodes (case_count >= --min-cases and control_count_after_exclusions
    >= --min-controls), matching eligible_phecodes.xlsx.
    """
    # Read from the phecode_sex table rather than re-deriving from the release, so the
    # matrix cannot disagree with the counts about which phecodes are sex-restricted.
    phecode_sex: dict[str, str] = dict(con.execute("SELECT phecode, restrict_sex FROM phecode_sex").fetchall())

    # noncase_excluded, not the raw --control-exclusions table: it also carries the
    # sub-threshold carriers, who must be NA rather than 0 (see _build_cases_and_counts).
    counts_table, cases_table, exclusions_table = "phecode_counts", "cases", "noncase_excluded"
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
        con.execute(f"CREATE TABLE phenotype_matrix AS SELECT person_id FROM cohort ORDER BY person_id")
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
        con.execute(f"CREATE TABLE retained_phecodes(phecode VARCHAR, restrict_sex VARCHAR)")
        con.executemany(f"INSERT INTO retained_phecodes VALUES (?, ?)",
                         [(p, phecode_sex.get(p)) for p in retained_phecodes])
        con.execute(f"""
          CREATE TABLE sparse_values AS
          SELECT ca.person_id, ca.phecode, 1 AS value FROM {cases_table} ca JOIN retained_phecodes rp ON rp.phecode = ca.phecode
          UNION ALL
          SELECT ex.person_id, ex.phecode, -1 AS value FROM {exclusions_table} ex JOIN retained_phecodes rp ON rp.phecode = ex.phecode
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
    output_stem = "phenotype_matrix"
    con.execute(f"COPY phenotype_matrix TO '{quote(output / (output_stem + '.parquet'))}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute(f"COPY phenotype_matrix TO '{quote(output / (output_stem + '.csv.gz'))}' (HEADER, DELIMITER ',', COMPRESSION 'gzip')")
    return {
        "n_columns": len(retained_phecodes),
        # Derived from cohort_sex_counts by the caller, not hardwired. When this is
        # false the run carries sex-restricted phecodes it cannot evaluate, and the
        # field below is non-zero rather than a constant 0.
        "cohort_has_usable_sex": has_sex,
        "sex_restricted_retained_phecodes": len(sex_restricted_retained),
        "sex_restricted_phecodes_treated_as_unrestricted": 0 if has_sex else len(sex_restricted_retained),
    }
