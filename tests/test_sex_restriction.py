"""Regression tests: sex restriction must reach the counts, not just the matrix.

Sex was previously consulted only when writing phenotype_matrix, so
phecode_counts / person_phecodes / eligible_phecodes.xlsx counted opposite-sex
people as cases and as controls, and `retained` was decided on those inflated
numbers. The suite could not see it: the shared fixture has no phecode_info, so
sex restriction was unreachable, and no test compared the counts against the
matrix that was actually delivered.

The invariant these pin is that the counts describe the same people the matrix
scores: matrix 1s == case_count, matrix 0s == control_count_after_exclusions.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes

# 3 women, 4 men, and one person of unknown sex.
COHORT = [["f1", "Female"], ["f2", "Female"], ["f3", "Female"],
          ["m1", "Male"], ["m2", "Male"], ["m3", "Male"], ["m4", "Male"], ["u1", ""]]
# m1 and u1 also carry the Female-only code -- miscoding and missing sex are both
# ordinary in real health data, and each was previously enough to manufacture a
# "case" of a female-specific phenotype. f1 is the only true GU_001 case.
EVENTS = [["f1", "123.4", "ICD9CM"], ["m1", "123.4", "ICD9CM"], ["u1", "123.4", "ICD9CM"],
          ["f2", "A01.1", "ICD10CM"], ["m2", "A01.1", "ICD10CM"]]
# Removes every A01.1 carrier (f2, m2) from GU_001's control pool. Only f2 is
# evaluable for GU_001, so only f2 may be counted as an excluded control.
EXCLUSIONS = [["GU_001", "code", "A01.1", "ICD10CM"]]


def _column_tally(matrix: Path, phecode: str) -> tuple[int, int, int]:
    """(cases, controls, non-evaluable) actually present in a matrix column."""
    return duckdb.sql(
        f'SELECT count(*) FILTER (WHERE "{phecode}" = 1), count(*) FILTER (WHERE "{phecode}" = 0),'
        f'       count(*) FILTER (WHERE "{phecode}" IS NULL)'
        f" FROM read_parquet('{matrix}')").fetchone()


def _counts(run: Path, phecode: str) -> tuple[int, int, bool]:
    return duckdb.sql(
        f"SELECT case_count, control_count_after_exclusions, retained"
        f" FROM read_parquet('{run / 'phecode_counts.parquet'}') WHERE phecode = '{phecode}'"
    ).fetchone()


def test_counts_and_matrix_agree_for_sex_restricted_phecodes(
    tmp_path: Path, full_release: Path
) -> None:
    """The counts must describe exactly the matrix's population."""
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    exclusions = tmp_path / "exclusions.csv"
    write_csv(cohort, ["person_id", "sex"], COHORT)
    write_csv(events, ["person_id", "code", "vocabulary"], EVENTS)
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"], EXCLUSIONS)
    map_phecodes(full_release, cohort, events, output, exclusions=exclusions,
                 min_cases=1, min_controls=1)

    matrix = output / "phenotype_matrix.parquet"
    columns = {r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{matrix}')").fetchall()}

    for phecode in columns - {"person_id"}:
        cases, controls, retained = _counts(output, phecode)
        ones, zeros, nas = _column_tally(matrix, phecode)
        assert cases == ones, f"{phecode}: case_count {cases} != {ones} ones in the matrix"
        assert controls == zeros, f"{phecode}: control_count {controls} != {zeros} zeros in the matrix"
        assert ones + zeros + nas == len(COHORT)

    # The specific numbers, so a change of convention has to be deliberate.
    # GU_001 is Female-only: denominator is the 3 women, not all 8 people. f1 is the
    # case; f2 is an excluded control; f3 is the one ordinary control. m2 is also an
    # A01.1 carrier but is not evaluable for GU_001, so he is not an excluded control.
    assert _counts(output, "GU_001") == (1, 1, True)
    assert _column_tally(matrix, "GU_001") == (1, 1, 6)
    # CV_003 is unrestricted, so it still uses the whole cohort and is untouched
    # by an exclusion rule scoped to GU_001.
    assert _counts(output, "CV_003") == (2, 6, True)
    assert _column_tally(matrix, "CV_003") == (2, 6, 0)


def test_opposite_sex_carriers_are_not_cases(
    tmp_path: Path, full_release: Path
) -> None:
    """m1 carries the Female-only code and must not appear as a case for it.

    person_phecodes is a person-level output an analyst may inspect directly, so
    the assertion is on that file rather than only on the aggregate.
    """
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], COHORT)
    write_csv(events, ["person_id", "code", "vocabulary"], EVENTS)
    map_phecodes(full_release, cohort, events, output, min_cases=1, min_controls=1)

    people = {r[0] for r in duckdb.sql(
        f"SELECT person_id FROM read_parquet('{output / 'person_phecodes.parquet'}')"
        f" WHERE phecode = 'GU_001'").fetchall()}
    # m1 is the opposite sex, u1's sex is unknown; neither is evaluable for GU_001.
    assert people == {"f1"}, "a non-evaluable carrier was recorded as a case"


def test_retention_uses_evaluable_controls_not_the_whole_cohort(tmp_path: Path, full_release: Path) -> None:
    """A sex-restricted phecode must be judged on the controls it actually has.

    GU_001 has 2 evaluable controls (f2, f3) but 6 whole-cohort non-cases. With
    --min-controls 3 the whole-cohort figure clears the bar and the evaluable one
    does not, so this fails if retention is computed over the wrong population --
    which previously shipped an all-NA column as a retained phenotype.
    """
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], COHORT)
    write_csv(events, ["person_id", "code", "vocabulary"], EVENTS)
    map_phecodes(full_release, cohort, events, output, min_cases=1, min_controls=3)

    cases, controls, retained = _counts(output, "GU_001")
    assert (cases, controls) == (1, 2)
    assert retained is False, "retained on a denominator that includes ineligible people"

    columns = {r[0] for r in duckdb.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{output / 'phenotype_matrix.parquet'}')").fetchall()}
    assert "GU_001" not in columns
    assert "CV_003" in columns  # unrestricted, 5 controls, still clears --min-controls 3


def test_unknown_sex_is_neither_a_case_nor_a_control_for_a_restricted_phecode(
    tmp_path: Path, full_release: Path
) -> None:
    """Blank sex must be non-evaluable in every direction.

    u1 carries the Female-only code, so a rule that treats unknown sex as
    evaluable makes them a case; one that treats them as an ordinary person makes
    them a control. Both are wrong, and asserting the full (1, 1, 1) tally rather
    than just the counts is what distinguishes them.
    """
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [["f1", "Female"], ["f2", "Female"], ["u1", ""]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["f1", "123.4", "ICD9CM"], ["u1", "123.4", "ICD9CM"]])
    map_phecodes(full_release, cohort, events, output, min_cases=1, min_controls=1)

    cases, controls, _ = _counts(output, "GU_001")
    assert (cases, controls) == (1, 1), "unknown-sex person leaked into a sex-restricted count"
    assert _column_tally(output / "phenotype_matrix.parquet", "GU_001") == (1, 1, 1)
    people = {r[0] for r in duckdb.sql(
        f"SELECT person_id FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchall()}
    assert people == {"f1"}
