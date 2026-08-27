from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes


def test_code_exclusion_is_vocabulary_specific(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"
    events = tmp_path / "events.csv"
    exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [
        ["p1", "123.4", "ICD9CM"],
        ["p2", "123.4", "ICD10CM"],
    ])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"], [
        ["AA_1", "code", "123.4", "ICD9CM"],
    ])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, exclusions=exclusions, min_cases=0, min_controls=0)
    row = duckdb.sql(
        f"SELECT case_count, excluded_control_count, control_count_after_exclusions "
        f"FROM read_parquet('{output / 'phecode_counts.parquet'}') WHERE phecode='AA_1'"
    ).fetchone()
    assert row == (1, 0, 1)


def test_exclusions_require_vocabulary(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value"], [["AA_1", "code", "123.4"]])
    with pytest.raises(ValueError, match="vocabulary"):
        map_phecodes(release, cohort, events, tmp_path / "run", exclusions=exclusions)


def test_exclusions_reject_unknown_vocabulary(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"], [["AA_1", "code", "123.4", "LOCAL"]])
    with pytest.raises(ValueError, match="ICD9CM"):
        map_phecodes(release, cohort, events, tmp_path / "run", exclusions=exclusions)


def test_exclusions_reject_unknown_type(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"; exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"], [["AA_1", "other", "123.4", "ICD9CM"]])
    with pytest.raises(ValueError, match="exclusion_type"):
        map_phecodes(release, cohort, events, tmp_path / "run", exclusions=exclusions)


def test_empty_events_produce_person_only_matrix(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output)
    assert duckdb.sql(f"SELECT * FROM read_parquet('{output / 'phenotype_matrix.parquet'}')").fetchall() == [("p1",), ("p2",)]
    assert json.loads((output / "audit.json").read_text())["unmapped_rate"] == 0


def test_unmapped_rate_boundary_and_empty_input(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "unknown", "ICD10CM"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, max_unmapped_rate=1.0)
    assert json.loads((output / "audit.json").read_text())["unmapped_rate"] == 1.0
    with pytest.raises(RuntimeError, match="Unmapped rate"):
        map_phecodes(release, cohort, events, tmp_path / "run2", max_unmapped_rate=0.999)


@pytest.mark.parametrize("rows", [
    [["p1"], ["p1"]],
    [[""]],
])
def test_invalid_cohort_ids_are_rejected(tmp_path: Path, release: Path, rows: list[list[str]]) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [[row[0], "Female"] for row in rows])
    write_csv(events, ["person_id", "code", "vocabulary"], [])
    with pytest.raises(ValueError, match="non-null and unique"):
        map_phecodes(release, cohort, events, tmp_path / "run")


def test_csv_and_parquet_inputs_have_same_results(tmp_path: Path, release: Path) -> None:
    cohort_csv = tmp_path / "cohort.csv"; events_csv = tmp_path / "events.csv"
    write_csv(cohort_csv, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events_csv, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    cohort_pq = tmp_path / "cohort.parquet"; events_pq = tmp_path / "events.parquet"
    # all_varchar=true matches how the mapper reads CSV. Without it DuckDB infers
    # code '123.4' as DOUBLE, and the Parquet fixture then carries the numeric-code
    # corruption that test_input_contract.py exists to reject -- so this parity test
    # would be comparing a lossy round-trip against a lossless one.
    duckdb.sql(f"COPY (SELECT * FROM read_csv_auto('{cohort_csv}', all_varchar=true)) TO '{cohort_pq}' (FORMAT PARQUET)")
    duckdb.sql(f"COPY (SELECT * FROM read_csv_auto('{events_csv}', all_varchar=true)) TO '{events_pq}' (FORMAT PARQUET)")
    csv_output = tmp_path / "csv_run"; pq_output = tmp_path / "pq_run"
    map_phecodes(release, cohort_csv, events_csv, csv_output, min_cases=0, min_controls=0)
    map_phecodes(release, cohort_pq, events_pq, pq_output, min_cases=0, min_controls=0)
    for name in ("phecode_counts.parquet", "person_phecodes.parquet", "phenotype_matrix.parquet"):
        assert duckdb.sql(f"SELECT * FROM read_parquet('{csv_output / name}')").fetchall() == duckdb.sql(f"SELECT * FROM read_parquet('{pq_output / name}')").fetchall()


def test_cli_returns_actionable_error(tmp_path: Path, release: Path) -> None:
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code"], [["p1", "123.4"]])
    result = subprocess.run([
        sys.executable, "-m", "phecodex_mapper.cli", "map-phecodes",
        "--release", str(release), "--cohort", str(cohort), "--events", str(events),
        "--output", str(tmp_path / "run"),
    ], capture_output=True, text=True)
    assert result.returncode == 1
    assert "events is missing required columns: ['vocabulary']" in result.stderr
