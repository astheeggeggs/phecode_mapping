"""Regression tests: exclusion rules must match on meaning, not on exact bytes.

Both exclusion mechanisms previously validated their input case-insensitively and
then matched it case-sensitively, so a capitalised value passed validation and
silently matched nothing -- the whole policy became a no-op with no error, no
warning, and outputs that looked entirely normal. A blank match_value was worse:
it put a NULL in excluded_phecodes, and `NOT IN` then dropped every phecode from
every output.

The parametrisation over spellings is the point of these tests. A single
lowercase case -- which is what the suite had -- cannot distinguish "matched
correctly" from "matched nothing".
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes

COHORT = [["f1", "Female"], ["f2", "Female"], ["f3", "Female"], ["f4", "Female"]]
# f1 is the GU_001 case; f2 and f3 are CV_003 cases and so carry the A01.1 code that
# the exclusion rules below target; f4 is the SS_004 case. f4's event matters: without
# a phecode in the excluded category actually present in the output, a "category rule
# dropped it" assertion passes whether or not the rule matched anything.
EVENTS = [["f1", "123.4", "ICD9CM"], ["f2", "A01.1", "ICD10CM"],
          ["f3", "A01.1", "ICD10CM"], ["f4", "A02.0", "ICD10CM"]]


def _restricted(run: Path) -> tuple[int, int, int]:
    """(case_count, excluded_control_count, control_count_after_exclusions) for GU_001."""
    return duckdb.sql(
        f"SELECT case_count, excluded_control_count, control_count_after_exclusions"
        f" FROM read_parquet('{run / 'phecode_counts.parquet'}') WHERE phecode = 'GU_001'").fetchone()


def _run(tmp_path: Path, release: Path, name: str, *, exclusions=None, exclude_phenotypes=None) -> Path:
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / name
    write_csv(cohort, ["person_id", "sex"], COHORT)
    write_csv(events, ["person_id", "code", "vocabulary"], EVENTS)
    map_phecodes(release, cohort, events, output, exclusions=exclusions,
                 exclude_phenotypes=exclude_phenotypes, min_cases=1, min_controls=0)
    return output


@pytest.mark.parametrize("spelling", ["code", "Code", "CODE", " code "])
def test_control_exclusion_type_matches_regardless_of_case(tmp_path: Path, full_release: Path, spelling: str) -> None:
    """Every spelling that passes validation must also actually exclude.

    f2 and f3 carry A01.1 and are non-cases for GU_001, so both are excluded from
    its control pool, leaving f4 as the only control. A spelling that matches
    nothing gives (1, 0, 3) instead -- indistinguishable from supplying no
    exclusions file at all.
    """
    exclusions = tmp_path / "exclusions.csv"
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
              [["GU_001", spelling, "A01.1", "ICD10CM"]])
    assert _restricted(_run(tmp_path, full_release, "run", exclusions=exclusions)) == (1, 2, 1)


@pytest.mark.parametrize("spelling", ["phecode", "Phecode", "PHECODE", " phecode "])
def test_phecode_exclusion_type_matches_regardless_of_case(tmp_path: Path, full_release: Path, spelling: str) -> None:
    exclusions = tmp_path / "exclusions.csv"
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
              [["GU_001", spelling, "CV_003", "ICD10CM"]])
    assert _restricted(_run(tmp_path, full_release, "run", exclusions=exclusions)) == (1, 2, 1)


@pytest.mark.parametrize("vocabulary", ["ICD10CM", "icd10cm", " Icd10Cm "])
def test_control_exclusion_vocabulary_matches_regardless_of_case(
    tmp_path: Path, full_release: Path, vocabulary: str
) -> None:
    exclusions = tmp_path / "exclusions.csv"
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
              [["GU_001", "code", "A01.1", vocabulary]])
    assert _restricted(_run(tmp_path, full_release, "run", exclusions=exclusions)) == (1, 2, 1)


def test_control_exclusion_value_tolerates_surrounding_whitespace(tmp_path: Path, full_release: Path) -> None:
    exclusions = tmp_path / "exclusions.csv"
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
              [[" GU_001 ", "code", " A01.1 ", "ICD10CM"]])
    assert _restricted(_run(tmp_path, full_release, "run", exclusions=exclusions)) == (1, 2, 1)


@pytest.mark.parametrize("spelling", ["Symptoms", "symptoms", "SYMPTOMS", " Symptoms "])
def test_exclude_phenotypes_category_matches_regardless_of_case(
    tmp_path: Path, full_release: Path, spelling: str
) -> None:
    """SS_004 is the release's only 'Symptoms' phecode and must be dropped entirely."""
    exclude = tmp_path / "exclude.csv"
    write_csv(exclude, ["match_type", "match_value"], [["category", spelling]])
    output = _run(tmp_path, full_release, "run", exclude_phenotypes=exclude)
    phecodes = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()}
    assert "SS_004" not in phecodes
    audit = json.loads((output / "audit.json").read_text())
    assert audit["exclude_phenotypes"]["phecodes_excluded"] == 1
    assert audit["exclude_phenotypes"]["unmatched_category_rules"] == []


@pytest.mark.parametrize("spelling", ["phecode", "Phecode", " PHECODE "])
def test_exclude_phenotypes_match_type_matches_regardless_of_case(
    tmp_path: Path, full_release: Path, spelling: str
) -> None:
    exclude = tmp_path / "exclude.csv"
    write_csv(exclude, ["match_type", "match_value"], [[spelling, "CV_003"]])
    output = _run(tmp_path, full_release, "run", exclude_phenotypes=exclude)
    phecodes = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()}
    assert "CV_003" not in phecodes


def test_category_rule_that_matches_nothing_is_recorded_not_silently_ignored(
    tmp_path: Path, full_release: Path, capsys
) -> None:
    """An unmatched category rule is legitimate but must be visible.

    A release need not contain every category a policy names, so this is a
    warning rather than an error -- but it is also exactly what a typo looks
    like, and it used to leave no trace anywhere in the outputs.
    """
    exclude = tmp_path / "exclude.csv"
    write_csv(exclude, ["match_type", "match_value"], [["category", "Sympt0ms"]])
    output = _run(tmp_path, full_release, "run", exclude_phenotypes=exclude)

    audit = json.loads((output / "audit.json").read_text())
    assert audit["exclude_phenotypes"]["unmatched_category_rules"] == ["Sympt0ms"]
    assert audit["exclude_phenotypes"]["phecodes_excluded"] == 0
    assert "Sympt0ms" in capsys.readouterr().err

    phecodes = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()}
    assert "SS_004" in phecodes, "nothing should have been excluded"


def test_blank_match_value_is_rejected_rather_than_emptying_the_run(tmp_path: Path, full_release: Path) -> None:
    """The NULL that made `NOT IN` drop every phecode must be refused at the door."""
    exclude = tmp_path / "exclude.csv"
    write_csv(exclude, ["match_type", "match_value"], [["phecode", "CV_003"], ["phecode", "  "]])
    with pytest.raises(ValueError, match="blank match_value"):
        _run(tmp_path, full_release, "run", exclude_phenotypes=exclude)


def test_unknown_phecode_rule_excludes_only_itself(tmp_path: Path, full_release: Path) -> None:
    """A rule naming a phecode absent from the release must not affect anything else.

    This is the shape that the `NOT IN` NULL bug generalised from: one unusable
    row in the exclusions file silently emptying every output.
    """
    exclude = tmp_path / "exclude.csv"
    write_csv(exclude, ["match_type", "match_value"], [["phecode", "NOT_IN_RELEASE"]])
    output = _run(tmp_path, full_release, "run", exclude_phenotypes=exclude)
    phecodes = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()}
    assert phecodes == {"GU_001", "CV_003", "SS_004"}, "an unmatched phecode rule disturbed the run"
