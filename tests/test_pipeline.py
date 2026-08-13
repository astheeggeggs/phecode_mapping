from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import pytest

from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.normalize import normalize_code
from conftest import write_csv


def test_normalization_is_presentation_only() -> None:
    assert normalize_code(" a01.1 ", "ICD10CM") == "A011"
    assert normalize_code("123.4", "ICD9CM") == "1234"
    assert normalize_code("123.4", "LOCAL") == "123.4"


def test_counts_before_and_after_exclusion_and_thresholds(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id"], [[str(i)] for i in range(1, 202)])
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"], [[str(i), "123.4", "ICD9CM", "2020-01-01"] for i in range(1, 201)])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "version"], [["AA_1", "code", "A011", "v1"]])
    # Person 201 is the only non-case and has an exclusion code for AA_1.
    with events.open("a", newline="") as stream: csv.writer(stream).writerow(["201", "A01.1", "ICD10CM", "2020-01-01"])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, exclusions=exclusions)
    row = duckdb.sql(f"SELECT * FROM read_parquet('{output / 'phecode_counts.parquet'}') WHERE phecode='AA_1'").fetchone()
    assert row[1:] == (200, 1, 1, 0, False)
    audit = json.loads((output / "audit.json").read_text())
    assert audit["exclusion_version"] == "v1"


def test_two_dates_and_case_precedence(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id"], [["p1"], ["p2"], ["p3"]])
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"], [
        ["p1", "12345", "ICD9CM", "2020-01-01"], ["p1", "123.45", "ICD9CM", "2020-01-02"],
        ["p2", "123.45", "ICD9CM", "2020-01-01"], ["p3", "A01.1", "ICD10CM", "2020-01-01"],
    ])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value"], [["AA_1.1", "phecode", "AA_1.1"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, case_rule="two-dates", exclusions=exclusions, min_cases=1, min_controls=1)
    rows = duckdb.sql(f"SELECT phecode, case_count, control_count_before_exclusions, excluded_control_count, control_count_after_exclusions FROM read_parquet('{output / 'phecode_counts.parquet'}') ORDER BY phecode").fetchall()
    assert rows[0] == ("AA_1", 1, 2, 0, 2)
    assert rows[1] == ("AA_1.1", 1, 2, 1, 1)  # p1 is protected; p2 is an excluded control


def test_invalid_input_and_unmapped_rate(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id"], [["p1"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "not-a-code", "ICD10CM"]])
    with pytest.raises(RuntimeError, match="Unmapped rate"):
        map_phecodes(release, cohort, events, tmp_path / "run", max_unmapped_rate=0)
