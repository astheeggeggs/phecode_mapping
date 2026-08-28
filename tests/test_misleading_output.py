"""Regression tests: reported numbers and notes must describe the run that happened.

Two audit findings, both of the same kind -- the computation was right and the
thing said about it was not. That is worse than a silent failure, because a
number or an annotation is what a reviewer trusts when they are not re-deriving
the result themselves.

Finding 05  validate-phecodex stamped `local_sex_denominator_not_available` on
            EVERY sex-restricted row. That described the pre-S1 behaviour; once
            denominators became sex-aware it was simply false, and it told a
            reviewer to discount exactly the rows that had become trustworthy.

Finding 06  audit.json counted `--exclude-phenotypes` phecode rules that name
            nothing in the release toward `phecodes_excluded`, reporting
            phenotypes as dropped that were never dropped. A typo, or the right
            identifier in the wrong case, read as confirmation the rule worked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.validation import validate_phecodex_counts
from phecodex_mapper.vocabulary import build_vocabulary


def _release(tmp_path: Path, name: str, *, with_sex: bool) -> Path:
    source = tmp_path / f"official_{name}.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GU_001", "A01.1", "ICD10CM"], ["CV_003", "I10", "ICD10CM"]])
    info = None
    if with_sex:
        info = tmp_path / f"info_{name}.csv"
        write_csv(info, ["phecode", "sex", "phecode_string", "category"],
                  [["GU_001", "Female", "Endometriosis", "Genitourinary"],
                   ["CV_003", "Both", "Hypertension", "Cardiovascular"]])
    release = tmp_path / f"rel_{name}"
    build_vocabulary(source, info, release, None)
    return release


# ---------------------------------------------------------------------------
# Finding 06 -- phecodes_excluded
# ---------------------------------------------------------------------------

def test_a_phecode_rule_naming_nothing_is_not_counted_as_excluded(tmp_path: Path) -> None:
    """The count must reflect phecodes dropped, not rules supplied."""
    release = _release(tmp_path, "e", with_sex=True)
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"]])
    drop = tmp_path / "drop.csv"
    write_csv(drop, ["match_type", "match_value"],
              [["phecode", "CV_003"],          # real
               ["phecode", "NOT_IN_RELEASE"],  # typo
               ["phecode", "gu_001"]])         # right identifier, wrong case

    out = tmp_path / "run"
    map_phecodes(release, cohort, events, out, exclude_phenotypes=drop,
                 min_cases=1, min_controls=0)
    summary = json.loads((out / "audit.json").read_text())["exclude_phenotypes"]

    assert summary["phecodes_excluded"] == 1, "rules matching nothing were counted as exclusions"
    assert summary["unmatched_phecode_rules"] == ["NOT_IN_RELEASE", "gu_001"]


def test_an_unmatched_phecode_rule_warns_on_stderr(tmp_path: Path, capsys) -> None:
    """Symmetric with the existing category-rule warning; a typo should be visible."""
    release = _release(tmp_path, "w", with_sex=True)
    cohort, events = tmp_path / "cw.csv", tmp_path / "ew.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"]])
    drop = tmp_path / "dropw.csv"
    write_csv(drop, ["match_type", "match_value"], [["phecode", "TYPO_999"]])

    map_phecodes(release, cohort, events, tmp_path / "run_w", exclude_phenotypes=drop,
                 min_cases=1, min_controls=0)
    err = capsys.readouterr().err
    assert "name no phecode in this release" in err
    assert "TYPO_999" in err


def test_a_matching_phecode_rule_is_still_counted(tmp_path: Path) -> None:
    """Positive control: the count must not simply become zero."""
    release = _release(tmp_path, "p", with_sex=True)
    cohort, events = tmp_path / "cp.csv", tmp_path / "ep.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"]])
    drop = tmp_path / "dropp.csv"
    write_csv(drop, ["match_type", "match_value"], [["phecode", "CV_003"], ["phecode", "GU_001"]])

    out = tmp_path / "run_p"
    map_phecodes(release, cohort, events, out, exclude_phenotypes=drop, min_cases=1, min_controls=0)
    summary = json.loads((out / "audit.json").read_text())["exclude_phenotypes"]
    assert summary["phecodes_excluded"] == 2
    assert summary["unmatched_phecode_rules"] == []


# ---------------------------------------------------------------------------
# Finding 05 -- the validate-phecodex sex note
# ---------------------------------------------------------------------------

def _external(path: Path) -> Path:
    write_csv(path, ["phecode", "description", "sex", "ancestry", "case_count",
                     "control_count", "sample_count", "source", "source_version"],
              [["GU_001", "Endometriosis", "Female", "ALL", "10", "90", "100", "ext", "1"],
               ["CV_003", "Hypertension", "Both", "ALL", "20", "180", "200", "ext", "1"]])
    return path


def _validate(tmp_path: Path, release: Path, name: str) -> list[dict]:
    cohort, events = tmp_path / f"cv_{name}.csv", tmp_path / f"ev_{name}.csv"
    write_csv(cohort, ["person_id", "sex"],
              [["f1", "Female"], ["f2", "Female"], ["m1", "Male"], ["m2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["f1", "A01.1", "ICD10CM"], ["m1", "I10", "ICD10CM"]])
    run = tmp_path / f"run_v_{name}"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=0)
    out = tmp_path / f"val_{name}"
    validate_phecodex_counts(run, release, _external(tmp_path / f"ext_{name}.csv"), out)
    import csv
    with open(out / "phecodex_comparison.csv") as fh:
        return list(csv.DictReader(fh))


def test_a_sex_aware_run_is_not_annotated_as_lacking_a_sex_denominator(tmp_path: Path) -> None:
    """The note described pre-S1 behaviour and became false when S1 landed."""
    rows = _validate(tmp_path, _release(tmp_path, "v1", with_sex=True), "v1")
    female = [r for r in rows if r["phecode"] == "GU_001"]
    assert female, "the Female-restricted phecode should appear in the comparison"
    for row in female:
        assert "not_sex_restricted" not in row["notes"], \
            "a sex-aware denominator was annotated as unavailable"
        assert "local_sex_denominator_not_available" not in row["notes"]


def test_a_run_whose_release_lacks_sex_metadata_IS_annotated(tmp_path: Path) -> None:
    """The note must still fire when it is true -- otherwise this is a deletion.

    Without sex metadata no phecode is restricted, so the local denominator
    really is not sex-restricted and a reviewer comparing against a sex-stratified
    external result needs to know.
    """
    import duckdb
    # A phecode_info WITHOUT a sex column: phecode_info.parquet exists (the validator
    # needs it) but the mapper sees no sex metadata and restricts nothing.
    source = tmp_path / "official_v2.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GU_001", "A01.1", "ICD10CM"], ["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "info_v2.csv"
    write_csv(info, ["phecode", "phecode_string", "category"],
              [["GU_001", "Endometriosis", "Genitourinary"],
               ["CV_003", "Hypertension", "Cardiovascular"]])
    release = tmp_path / "rel_v2"
    build_vocabulary(source, info, release, None)

    cohort, events = tmp_path / "cv_v2.csv", tmp_path / "ev_v2.csv"
    write_csv(cohort, ["person_id", "sex"],
              [["f1", "Female"], ["f2", "Female"], ["m1", "Male"], ["m2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["f1", "A01.1", "ICD10CM"], ["m1", "I10", "ICD10CM"]])
    run = tmp_path / "run_v_v2"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=0)
    assert json.loads((run / "audit.json").read_text())["sex"]["release_has_sex_metadata"] is False

    # Add the column back purely so the validator can compare strata. This does not
    # touch manifest.json, so the run's provenance check still passes.
    info_parquet = release / "phecode_info.parquet"
    duckdb.sql(f"""COPY (SELECT phecode, 'Female' AS sex, phecode_string, category
                         FROM read_parquet('{info_parquet}')) TO '{info_parquet}' (FORMAT PARQUET)""")
    out = tmp_path / "val_v2"
    validate_phecodex_counts(run, release, _external(tmp_path / "ext_v2.csv"), out)
    import csv
    with open(out / "phecodex_comparison.csv") as fh:
        rows = list(csv.DictReader(fh))
    annotated = [r for r in rows if "local_denominator_not_sex_restricted" in r["notes"]]
    assert annotated, "a genuinely sex-blind denominator was not annotated"


# ---------------------------------------------------------------------------
# An empty matrix is a successful run that produced nothing usable
# ---------------------------------------------------------------------------

def test_an_empty_matrix_says_why_it_is_empty(tmp_path: Path, capsys) -> None:
    """The documented example run does exactly this: 2 people against min_cases=200.

    It exits 0, writes a matrix with only a person_id column, and reports
    "matrix_columns: 0" with no reason. That is indistinguishable from a broken
    install to someone running the tool for the first time, and the bundled
    examples/ files guarantee every new analyst meets it.
    """
    release = _release(tmp_path, "empty", with_sex=True)
    cohort, events = tmp_path / "ce.csv", tmp_path / "ee.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"]])
    out = tmp_path / "empty_run"
    map_phecodes(release, cohort, events, out, min_cases=200, min_controls=200)

    err = capsys.readouterr().err
    assert "no phecode met the retention thresholds" in err
    assert "--min-cases" in err, "the warning must name the knob that caused it"
    reason = json.loads((out / "audit.json").read_text())["phenotype_matrix"]["no_columns_retained_because"]
    assert reason["phecodes_with_at_least_one_case"] == 1
    assert reason["largest_case_count"] == 1


def test_a_run_that_retains_columns_does_not_warn(tmp_path: Path, capsys) -> None:
    """Negative control: a warning printed unconditionally teaches analysts to ignore it."""
    release = _release(tmp_path, "full", with_sex=True)
    cohort, events = tmp_path / "cf.csv", tmp_path / "ef.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"], ["p3", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["p1", "A01.1", "ICD10CM"], ["p2", "A01.1", "ICD10CM"]])
    out = tmp_path / "full_run"
    map_phecodes(release, cohort, events, out, min_cases=1, min_controls=1)

    assert "no phecode met the retention thresholds" not in capsys.readouterr().err
    audit = json.loads((out / "audit.json").read_text())
    assert audit["phenotype_matrix"]["n_columns"] >= 1
    assert audit["phenotype_matrix"]["no_columns_retained_because"] is None
