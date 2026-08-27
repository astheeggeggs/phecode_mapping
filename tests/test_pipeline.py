from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import pytest

from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.normalize import normalize_code
from phecodex_mapper.vocabulary import build_vocabulary
from conftest import write_csv


def test_normalization_is_presentation_only() -> None:
    assert normalize_code(" a01.1 ", "ICD10CM") == "A011"
    assert normalize_code("123.4", "ICD9CM") == "1234"
    assert normalize_code("123.4", "LOCAL") == "123.4"


def test_counts_before_and_after_exclusion_and_thresholds(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], [[str(i), "Female"] for i in range(1, 202)])
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"], [[str(i), "123.4", "ICD9CM", "2020-01-01"] for i in range(1, 201)])
    # Exclusion codes may use presentation punctuation just like event codes.
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary", "version"], [["AA_1", "code", "A01.1", "ICD10CM", "v1"]])
    # Person 201 is the only non-case and has an exclusion code for AA_1.
    with events.open("a", newline="") as stream: csv.writer(stream).writerow(["201", "A01.1", "ICD10CM", "2020-01-01"])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, exclusions=exclusions)
    row = duckdb.sql(f"SELECT * FROM read_parquet('{output / 'phecode_counts.parquet'}') WHERE phecode='AA_1'").fetchone()
    # case, control_before, excluded, subthreshold, control_after, retained.
    # subthreshold is 0 under the default any-event rule: one event already makes a case.
    assert row[1:] == (200, 1, 1, 0, 0, False)
    audit = json.loads((output / "audit.json").read_text())
    assert audit["exclusion_version"] == "v1"


def test_two_dates_and_case_precedence(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"], ["p3", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"], [
        ["p1", "12345", "ICD9CM", "2020-01-01"], ["p1", "123.45", "ICD9CM", "2020-01-02"],
        ["p2", "123.45", "ICD9CM", "2020-01-01"], ["p3", "A01.1", "ICD10CM", "2020-01-01"],
    ])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"], [["AA_1.1", "phecode", "AA_1.1", "ICD9CM"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, case_rule="two-dates", exclusions=exclusions, min_cases=1, min_controls=1)
    rows = duckdb.sql(f"SELECT phecode, case_count, control_count_before_exclusions, excluded_control_count, subthreshold_control_count, control_count_after_exclusions FROM read_parquet('{output / 'phecode_counts.parquet'}') ORDER BY phecode").fetchall()
    # p2 carries the code on a single date, so under two-dates they are neither a case
    # nor a clean control -- non-evaluable, matching PheTK's control definition.
    assert rows[0] == ("AA_1", 1, 2, 0, 1, 1)
    # For AA_1.1 p2 is both sub-threshold and named by the exclusion rule; they are
    # removed once, so the control count is 1 either way. p1 is a case and protected.
    assert rows[1] == ("AA_1.1", 1, 2, 1, 1, 1)


def test_invalid_input_and_unmapped_rate(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "not-a-code", "ICD10CM"]])
    with pytest.raises(RuntimeError, match="Unmapped rate"):
        map_phecodes(release, cohort, events, tmp_path / "run", max_unmapped_rate=0)


def test_phenotype_matrix_sex_restriction_and_exclusions(tmp_path: Path) -> None:
    # Build a dedicated release with phecode_info marking AA_1 as Female-only,
    # so we can exercise the sex-restriction NA logic.
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["AA_1", "123.4", "ICD9CM"],
        ["BB_2", "A01.1", "ICD10CM"],
    ])
    info = tmp_path / "info.csv"
    write_csv(info, ["phecode", "sex"], [["AA_1", "Female"], ["BB_2", "Both"]])
    release = tmp_path / "release"
    build_vocabulary(source, info, release)

    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    # p1: female case for AA_1. p2: male -- ineligible for AA_1, should be NA not 0.
    # p3: female non-case, but is a case for BB_2, which excludes people from AA_1's
    #     control pool -- NA, not 0. p4: female non-case, no exclusion -- ordinary control (0).
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"], ["p3", "Female"], ["p4", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"], ["p3", "A01.1", "ICD10CM"]])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"], [["AA_1", "phecode", "BB_2", "ICD9CM"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, exclusions=exclusions, min_cases=1, min_controls=1)

    rows = dict(duckdb.sql(f"SELECT person_id, \"AA_1\" FROM read_parquet('{output / 'phenotype_matrix.parquet'}') ORDER BY person_id").fetchall())
    assert rows["p1"] == 1  # case
    assert rows["p2"] is None  # male, sex-restricted phecode -> NA
    assert rows["p3"] is None  # excluded control -> NA
    assert rows["p4"] == 0  # ordinary control

    audit = json.loads((output / "audit.json").read_text())
    # Renamed from cohort_has_sex_column, which was hardwired True and so asserted
    # nothing; this value is now derived from cohort_sex_counts.
    assert audit["phenotype_matrix"]["cohort_has_usable_sex"] is True
    assert audit["phenotype_matrix"]["sex_restricted_retained_phecodes"] == 1
    assert audit["sex"]["release_has_sex_metadata"] is True
    assert audit["sex"]["n_unknown_sex"] == 0
    assert audit["sex"]["n_male"] + audit["sex"]["n_female"] == 4


def test_exclude_phenotypes_drops_whole_phecodes_from_every_output(tmp_path: Path) -> None:
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["AA_1", "123.4", "ICD9CM"],
        ["BB_2", "A01.1", "ICD10CM"],
    ])
    info = tmp_path / "info.csv"
    write_csv(info, ["phecode", "category"], [["AA_1", "Symptoms"], ["BB_2", "Cardiovascular"]])
    release = tmp_path / "release"
    build_vocabulary(source, info, release)

    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclude = tmp_path / "exclude.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"], ["p2", "A01.1", "ICD10CM"]])
    write_csv(exclude, ["match_type", "match_value"], [["category", "Symptoms"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, min_cases=1, min_controls=1, exclude_phenotypes=exclude)

    phecodes = duckdb.sql(f"SELECT DISTINCT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()
    assert phecodes == [("BB_2",)]  # AA_1 (Symptoms) dropped entirely, not just excluded from controls
    cases = duckdb.sql(f"SELECT phecode FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchall()
    assert cases == [("BB_2",)]
    matrix_cols = {r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{output / 'phenotype_matrix.parquet'}')").fetchall()}
    assert matrix_cols == {"person_id", "BB_2"}
    audit = json.loads((output / "audit.json").read_text())
    assert audit["exclude_phenotypes"]["phecodes_excluded"] == 1


def test_exclude_phenotypes_by_phecode_and_category_error_without_info(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclude = tmp_path / "exclude.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    write_csv(exclude, ["match_type", "match_value"], [["phecode", "AA_1"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, min_cases=1, min_controls=1, exclude_phenotypes=exclude)
    phecodes = duckdb.sql(f"SELECT DISTINCT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()
    assert phecodes == []  # AA_1 was the only phecode with events; excluded by phecode-type rule

    exclude_by_category = tmp_path / "exclude_category.csv"
    write_csv(exclude_by_category, ["match_type", "match_value"], [["category", "Symptoms"]])
    with pytest.raises(ValueError, match="no phecode_info.parquet"):
        map_phecodes(release, cohort, events, tmp_path / "run2", exclude_phenotypes=exclude_by_category)


def test_cohort_requires_sex_column(tmp_path: Path) -> None:
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["AA_1", "123.4", "ICD9CM"]])
    info = tmp_path / "info.csv"
    write_csv(info, ["phecode", "sex"], [["AA_1", "Female"]])
    release = tmp_path / "release"
    build_vocabulary(source, info, release)

    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id"], [["p1"], ["p2"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    output = tmp_path / "run"
    with pytest.raises(ValueError, match=r"cohort is missing required columns: \['sex'\]"):
        map_phecodes(release, cohort, events, output, min_cases=1, min_controls=1)
