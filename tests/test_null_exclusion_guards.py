"""Regression tests: a blank cell in an exclusion file must raise, not evaporate.

`WHERE col NOT IN ('a','b')` is the natural way to find bad rows and it silently
misses the worst input. SQL three-valued logic makes `NULL NOT IN ('a','b')`
UNKNOWN rather than TRUE, so a blank is never selected and passes validation --
and the join that later applies the rule is UNKNOWN for the same reason, so the
rule matches nothing and disappears without an error.

The perverse consequence, measured on the fixture below before the fix:

    well-formed rule       excluded=2  controls_after=2   correct
    blank vocabulary       excluded=0  controls_after=4   rule vanished
    blank exclusion_type   excluded=0  controls_after=4   rule vanished
    typo "kode"            RAISED                         caught

A misspelling was caught loudly while an empty cell sailed through, leaving two
hypertensives in the diabetes control pool and a run that looked entirely
successful.

This is the same three-valued-logic defect baa3d56 already fixed in the exclusion
*filter* (NOT IN -> NOT EXISTS). It left NOT IN in the three *validation* guards,
so the half of the bug that catches user error survived. No test among the 121
covered a NULL in any of those columns.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary

EX_COLS = ["phecode", "exclusion_type", "exclusion_value", "vocabulary"]


@pytest.fixture
def rel(tmp_path: Path) -> Path:
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_401", "I10", "ICD10CM"], ["EM_202", "E11", "ICD10CM"]])
    info = tmp_path / "info.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_401", "Both", "Hypertension", "Cardiovascular"],
               ["EM_202", "Both", "Diabetes", "Endocrine"]])
    out = tmp_path / "rel"
    build_vocabulary(source, info, out, None)
    return out


def _run(tmp_path: Path, rel: Path, rows: list, name: str) -> Path:
    """p1/p2 are diabetes cases; p3/p4 are hypertensives to remove from EM_202 controls."""
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], [[f"p{i}", "Female"] for i in range(1, 7)])
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["p1", "E11", "ICD10CM"], ["p2", "E11", "ICD10CM"],
               ["p3", "I10", "ICD10CM"], ["p4", "I10", "ICD10CM"]])
    ex = tmp_path / f"ex_{name}.csv"
    write_csv(ex, EX_COLS, rows)
    out = tmp_path / f"run_{name}"
    map_phecodes(rel, cohort, events, out, exclusions=ex, min_cases=1, min_controls=0)
    return out


def test_a_well_formed_exclusion_still_works(tmp_path: Path, rel: Path) -> None:
    """Positive control. Without this the tests below pass by refusing everything."""
    out = _run(tmp_path, rel, [["EM_202", "code", "I10", "ICD10CM"]], "good")
    excluded, after = duckdb.sql(
        "SELECT excluded_control_count, control_count_after_exclusions FROM "
        f"read_parquet('{out / 'phecode_counts.parquet'}') WHERE phecode='EM_202'").fetchone()
    assert (excluded, after) == (2, 2)


@pytest.mark.parametrize("column,row", [
    ("vocabulary", ["EM_202", "code", "I10", ""]),
    ("exclusion_type", ["EM_202", "", "I10", "ICD10CM"]),
    ("phecode", ["", "code", "I10", "ICD10CM"]),
    ("exclusion_value", ["EM_202", "code", "", "ICD10CM"]),
])
def test_a_blank_in_any_joined_column_is_refused(tmp_path: Path, rel: Path, column: str, row: list) -> None:
    """Each of these silently voided the rule and reported a clean run."""
    with pytest.raises(ValueError, match=f"empty {column}"):
        _run(tmp_path, rel, [row], f"blank_{column}")


def test_the_message_says_a_blank_voids_the_rule(tmp_path: Path, rel: Path) -> None:
    """'invalid value' would not tell the analyst why an empty cell is dangerous."""
    with pytest.raises(ValueError, match="silently voids the whole rule"):
        _run(tmp_path, rel, [["EM_202", "code", "I10", ""]], "msg")


def test_an_unrecognised_value_is_still_caught_and_named(tmp_path: Path, rel: Path) -> None:
    """The typo path must not regress, and must stay distinct from the blank path."""
    with pytest.raises(ValueError, match="must be one of"):
        _run(tmp_path, rel, [["EM_202", "kode", "I10", "ICD10CM"]], "typo")


def test_blank_and_typo_produce_different_messages(tmp_path: Path, rel: Path) -> None:
    """'you left this empty' and 'you misspelled this' want different corrections."""
    with pytest.raises(ValueError) as blank:
        _run(tmp_path, rel, [["EM_202", "", "I10", "ICD10CM"]], "d1")
    with pytest.raises(ValueError) as typo:
        _run(tmp_path, rel, [["EM_202", "kode", "I10", "ICD10CM"]], "d2")
    assert str(blank.value) != str(typo.value)
    assert "empty" in str(blank.value) and "empty" not in str(typo.value)


def test_exclude_phenotypes_blank_match_type_is_refused(tmp_path: Path, rel: Path) -> None:
    """The same guard on the other exclusion file."""
    cohort, events = tmp_path / "c2.csv", tmp_path / "e2.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "E11", "ICD10CM"]])
    drop = tmp_path / "drop.csv"
    write_csv(drop, ["match_type", "match_value"], [["", "Endocrine"]])
    with pytest.raises(ValueError, match="empty match_type"):
        map_phecodes(rel, cohort, events, tmp_path / "run_ep", exclude_phenotypes=drop,
                     min_cases=1, min_controls=0)
