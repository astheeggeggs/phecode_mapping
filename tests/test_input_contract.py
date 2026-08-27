"""Regression tests: inputs that cannot be read losslessly must be refused.

CSV is read with all_varchar=true but Parquet keeps its native types, so the same
logical file could arrive with a numeric `code` or `person_id` column. Nothing
checked, and the coercions that followed were silent and unrecoverable:

  INTEGER 1     -> '1'      an ICD-9 '001' that no longer maps
  DOUBLE  250.0 -> '250.0'  normalizes to '2500', a DIFFERENT real ICD-9 code
  non-ISO date  -> NULL     two-dates never sees a second date; cases become controls
  id mismatch   -> no join  every event dropped, audit reports 0 events / 0.0 rate

The Parquet fixtures here are the point: a CSV round-trip yields VARCHAR either
way, which is why the existing CSV/Parquet parity test could not see any of this.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.workflow import preflight, run_workflow


def _events_parquet(path: Path, rows: list[tuple], code_sql: str) -> None:
    """Write an events Parquet whose `code` column has a chosen native type."""
    values = ", ".join(f"({p!r}, {c}, {v!r})".replace("'", "'") for p, c, v in rows)
    duckdb.sql(f"COPY (SELECT CAST(col0 AS VARCHAR) AS person_id, CAST(col1 AS {code_sql}) AS code,"
               f" CAST(col2 AS VARCHAR) AS vocabulary FROM (VALUES {values}) t(col0, col1, col2))"
               f" TO '{path}' (FORMAT PARQUET)")


def _cohort(tmp_path: Path) -> Path:
    cohort = tmp_path / "cohort.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    return cohort


@pytest.mark.parametrize("code_sql,label", [("INTEGER", "leading zeros"), ("DOUBLE", "trailing decimal zeros")])
def test_numeric_code_column_is_rejected(tmp_path: Path, full_release: Path, code_sql: str, label: str) -> None:
    events = tmp_path / "events.parquet"
    _events_parquet(events, [("p1", "123", "ICD9CM")], code_sql)
    with pytest.raises(ValueError, match="must be text"):
        preflight(full_release, _cohort(tmp_path), events)


def test_text_code_column_in_parquet_is_accepted(tmp_path: Path, full_release: Path) -> None:
    """The contract must reject the lossy typing, not Parquet events as such."""
    events = tmp_path / "events.parquet"
    _events_parquet(events, [("p1", "123.4", "ICD9CM")], "VARCHAR")
    report = preflight(full_release, _cohort(tmp_path), events)
    assert report["event_rows"] == 1


def test_mixed_person_id_types_across_files_are_rejected(tmp_path: Path, full_release: Path) -> None:
    """A text cohort and an integer events file must not be silently coerced."""
    events = tmp_path / "events.parquet"
    duckdb.sql(f"COPY (SELECT CAST(1 AS BIGINT) AS person_id, '123.4' AS code, 'ICD9CM' AS vocabulary)"
               f" TO '{events}' (FORMAT PARQUET)")
    with pytest.raises(ValueError, match="person_id"):
        preflight(full_release, _cohort(tmp_path), events)


def test_events_matching_no_cohort_person_are_rejected(tmp_path: Path, full_release: Path) -> None:
    """Every event dropped means the files describe different populations.

    Previously this completed with audit events: 0 and unmapped_rate: 0.0, passing
    even --max-unmapped-rate 0.0.
    """
    cohort, events = _cohort(tmp_path), tmp_path / "events.csv"
    write_csv(events, ["person_id", "code", "vocabulary"], [["nobody", "123.4", "ICD9CM"]])
    with pytest.raises(ValueError, match="none of the 1 event rows match"):
        preflight(full_release, cohort, events)


def test_some_unknown_people_are_reported_but_allowed(tmp_path: Path, full_release: Path) -> None:
    """Phenotyping a cohort subset is legitimate; only a total mismatch is fatal."""
    cohort, events = _cohort(tmp_path), tmp_path / "events.csv"
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["p1", "123.4", "ICD9CM"], ["nobody", "123.4", "ICD9CM"]])
    report = preflight(full_release, cohort, events)
    assert report["events_for_unknown_people"] == 1


def test_unparseable_dates_are_fatal_under_two_dates(tmp_path: Path, full_release: Path) -> None:
    """Non-ISO dates silently demote repeat-coded cases to controls."""
    cohort, events = _cohort(tmp_path), tmp_path / "events.csv"
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"],
              [["p1", "123.4", "ICD9CM", "31/01/2020"], ["p1", "123.4", "ICD9CM", "01/02/2020"]])
    with pytest.raises(ValueError, match="not an ISO date"):
        map_phecodes(full_release, cohort, events, tmp_path / "run", case_rule="two-dates",
                     min_cases=1, min_controls=0)


def test_unparseable_dates_are_reported_by_preflight(tmp_path: Path, full_release: Path) -> None:
    """Under any-event the dates are unused, so surface the count rather than failing."""
    cohort, events = _cohort(tmp_path), tmp_path / "events.csv"
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"],
              [["p1", "123.4", "ICD9CM", "31/01/2020"], ["p2", "A01.1", "ICD10CM", "2020-02-01"]])
    report = preflight(full_release, cohort, events)
    assert report["event_rows_with_unparseable_date"] == 1

    output = tmp_path / "run"
    run_workflow(release=full_release, cohort=cohort, events=events, output=output,
                 min_cases=1, min_controls=0)
    assert (output / "audit.json").exists()


@pytest.mark.parametrize("bad_input", ["sex", "code_type", "unknown_people"])
def test_map_phecodes_enforces_the_same_contract_as_run(
    tmp_path: Path, full_release: Path, bad_input: str
) -> None:
    """`map-phecodes` must not accept input that `run` rejects.

    README documents map-phecodes as available to advanced users, and it used to
    perform none of preflight's checks -- so the same file that `run` refuses
    produced a completed run with silently wrong output. These assert against
    map_phecodes directly; asserting via preflight would not detect a regression
    where the shared validator is simply not called here.
    """
    cohort, events = tmp_path / "cohort.csv", tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    expected = "sex must be"

    if bad_input == "sex":
        write_csv(cohort, ["person_id", "sex"], [["p1", "F"], ["p2", "M"]])
    elif bad_input == "code_type":
        events = tmp_path / "events.parquet"
        _events_parquet(events, [("p1", "123", "ICD9CM")], "INTEGER")
        expected = "must be text"
    else:
        write_csv(events, ["person_id", "code", "vocabulary"], [["nobody", "123.4", "ICD9CM"]])
        expected = "none of the 1 event rows match"

    with pytest.raises(ValueError, match=expected):
        map_phecodes(full_release, cohort, events, tmp_path / "run", min_cases=1, min_controls=0)


def test_person_id_string_variants_do_not_collide(tmp_path: Path, full_release: Path) -> None:
    """'1', '01' and '001' are three people and must stay three people.

    A sanity check on the matching itself, not the regression test for the
    coercion bug -- both files here are CSV, so both were already VARCHAR and this
    passed before the fix too. The collision needed a text cohort against an
    integer Parquet events file, which is now refused outright; that case is
    covered by test_mixed_person_id_types_across_files_are_rejected.
    """
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [["1", "Female"], ["01", "Female"], ["001", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["1", "123.4", "ICD9CM"]])
    map_phecodes(full_release, cohort, events, output, min_cases=1, min_controls=0)

    cases = duckdb.sql(
        f"SELECT person_id FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchall()
    assert cases == [("1",)], "person_id variants collided through type coercion"
    rows = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{output / 'phenotype_matrix.parquet'}')").fetchone()[0]
    assert rows == 3
