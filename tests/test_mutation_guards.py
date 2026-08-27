"""Tests that exist to kill specific mutations, not to describe behaviour.

An audit of baa3d56 mutation-tested the 121-test suite and found several changes
that reintroduce fixed defects while every test still passes. Re-measured against
the 133-test suite at 9eca4c3:

    NOT EXISTS -> NOT IN in the exclusion predicate     133 passed   SURVIVES
    drop EVALUABLE from the any-event case query          5 failed   killed
    drop EVALUABLE from the subthreshold query            4 failed   killed
    add a parent-prefix fallback to the mapping join    133 passed   SURVIVES

The two EVALUABLE mutations were killed by tests/test_sex_metadata.py, added
after the audit ran. This module covers the two that survive.

Each test below is written to fail under its named mutation, and both were
confirmed to do so before being accepted. They assert invariants that no other
test pins, which is why the mutations lived.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary


# --------------------------------------------------------------------------
# M1: the exclusion predicate must be NULL-safe.
#
# `phecode NOT IN (SELECT phecode FROM excluded_phecodes)` is UNKNOWN for EVERY
# row as soon as that subquery yields a single NULL, so the WHERE selects
# nothing and every phecode silently vanishes from every output. NOT EXISTS is
# unaffected. A NULL gets in via a release whose phecode_info carries a blank
# phecode row in a category that --exclude-phenotypes names -- malformed, but
# nothing rejects it, and the failure is total and silent.
# --------------------------------------------------------------------------

@pytest.fixture
def release_with_blank_phecode_row(tmp_path: Path) -> Path:
    """A release whose phecode_info has a blank phecode in an excluded category."""
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_401", "I10", "ICD10CM"], ["SS_004", "R10", "ICD10CM"]])
    info = tmp_path / "info.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_401", "Both", "Hypertension", "Cardiovascular"],
               ["SS_004", "Both", "Abdominal pain", "Symptoms"]])
    release = tmp_path / "rel"
    build_vocabulary(source, info, release, None)

    # Append the malformed row directly: build_vocabulary is not the thing under
    # test here, and a real release can be assembled by other means.
    info_parquet = release / "phecode_info.parquet"
    duckdb.sql(f"""COPY (
        SELECT * FROM read_parquet('{info_parquet}')
        UNION ALL
        SELECT NULL AS phecode, 'Both' AS sex, '' AS phecode_string, 'Symptoms' AS category
    ) TO '{info_parquet}' (FORMAT PARQUET)""")
    return release


def test_a_null_in_excluded_phecodes_does_not_erase_every_phecode(
        tmp_path: Path, release_with_blank_phecode_row: Path) -> None:
    """MUTATION GUARD: fails if the predicate reverts to NOT IN.

    Excluding the Symptoms category picks up the blank phecode row, putting a
    NULL into excluded_phecodes. Under NOT IN, CV_401 disappears too and the run
    produces an empty output that looks merely uninformative.
    """
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    drop = tmp_path / "drop.csv"
    write_csv(drop, ["match_type", "match_value"], [["category", "Symptoms"]])

    out = tmp_path / "run"
    map_phecodes(release_with_blank_phecode_row, cohort, events, out,
                 exclude_phenotypes=drop, min_cases=1, min_controls=0)

    phecodes = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{out / 'phecode_counts.parquet'}')").fetchall()}
    assert "CV_401" in phecodes, \
        "a NULL in excluded_phecodes erased every phecode -- the predicate is not NULL-safe"
    assert "SS_004" not in phecodes, "the Symptoms rule should still have excluded SS_004"

    cases = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchone()[0]
    assert cases == 1, "p1 is a hypertension case and must survive the unrelated exclusion"


# --------------------------------------------------------------------------
# M2: mapping is exact-match only.
#
# The hierarchy fallback was removed deliberately in baa3d56, measured as
# rescuing 0.10% of cases while ~40% of what it rescued was trauma or iatrogenic
# codes inheriting disease phenotypes. Nothing in the suite pinned that decision,
# so a prefix join could be reintroduced with every test still green.
# --------------------------------------------------------------------------

@pytest.fixture
def parent_only_release(tmp_path: Path) -> Path:
    """A map containing the parent E11 but NOT the child E11.42."""
    source = tmp_path / "official_p.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["EM_202", "E11", "ICD10CM"]])
    info = tmp_path / "info_p.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["EM_202", "Both", "Diabetes", "Endocrine"]])
    release = tmp_path / "rel_p"
    build_vocabulary(source, info, release, None)
    return release


def test_a_child_code_whose_parent_is_mapped_stays_unmapped(
        tmp_path: Path, parent_only_release: Path) -> None:
    """MUTATION GUARD: fails if any parent/prefix inference is reintroduced.

    E11.42 (diabetic polyneuropathy) is absent from the map; E11 is present.
    Exact-only mapping must leave E11.42 unmapped rather than inferring EM_202.
    """
    cohort, events = tmp_path / "c_p.csv", tmp_path / "e_p.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "E11.42", "ICD10CM"]])

    out = tmp_path / "run_p"
    map_phecodes(parent_only_release, cohort, events, out, min_cases=1, min_controls=0,
                 max_unmapped_rate=1.0)

    cases = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchone()[0]
    assert cases == 0, "E11.42 was mapped to its parent's phecode -- hierarchy inference is back"

    unmapped = duckdb.sql(
        f"SELECT count(*) FROM read_csv_auto('{out / 'unmapped_events.csv'}')").fetchone()[0]
    assert unmapped == 1, "the child code should be reported as unmapped, not silently absorbed"


def test_the_exact_parent_code_still_maps(tmp_path: Path, parent_only_release: Path) -> None:
    """Positive control: exact-only must not be mistaken for matching nothing."""
    cohort, events = tmp_path / "c_e.csv", tmp_path / "e_e.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "E11", "ICD10CM"]])

    out = tmp_path / "run_e"
    map_phecodes(parent_only_release, cohort, events, out, min_cases=1, min_controls=0)
    cases = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchall()}
    assert cases == {"EM_202"}


def test_punctuation_still_normalises(tmp_path: Path, parent_only_release: Path) -> None:
    """Exact-only is about hierarchy, not spelling.

    E11 and 'e11 ' are the same code. If a guard against prefix inference were
    implemented by comparing raw strings, this would break -- and the PheTK
    comparison showed normalisation is what makes us robust where PheTK's exact
    string matching collapses from 2,620 phecodes to 328.
    """
    cohort, events = tmp_path / "c_n.csv", tmp_path / "e_n.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", " e11 ", "ICD10CM"]])

    out = tmp_path / "run_n"
    map_phecodes(parent_only_release, cohort, events, out, min_cases=1, min_controls=0)
    cases = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchone()[0]
    assert cases == 1
