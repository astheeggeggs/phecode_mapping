"""The single definition of evaluability, cross-checked against itself.

retention.py ships each rule twice -- once as a SQL fragment for the mapper, once in
Python for the attrition curve -- and nothing had ever asserted that the two spellings
answer the same question. A module whose whole purpose is to stop three consumers
drifting apart is worth exactly as much as the check that its own halves have not.

Anything computable two ways is computed both ways here and must reconcile.
"""
from __future__ import annotations

from itertools import product
from pathlib import Path

from phecodex_mapper.io import connect, relation_for
from phecodex_mapper.retention import (
    eligible_count, eligible_count_sql, is_retained, load_phecode_restrictions, retained_sql)


def test_the_sql_and_python_denominators_agree() -> None:
    """eligible_count_sql and eligible_count, on the same inputs, including bad ones.

    'Male' and '' must fall through to the unrestricted branch in BOTH -- that is the
    documented contract (the caller canonicalises first), and a SQL CASE that matched
    case-insensitively while the dict did not would give the mapper and the curve
    different denominators for every sex-restricted phecode.
    """
    con = connect()
    for restrict in (None, "MALE", "FEMALE", "Male", "female", "", "BOTH", "Unknown"):
        literal = "NULL" if restrict is None else f"'{restrict}'"
        sql = eligible_count_sql(literal, n_male="30", n_female="20", n_all="55")
        assert con.execute(f"SELECT {sql}").fetchone()[0] == \
            eligible_count(restrict, n_male=30, n_female=20, n_all=55), restrict


def test_the_sql_and_python_retention_rules_agree() -> None:
    """retained_sql and is_retained, across both thresholds and their boundaries."""
    con = connect()
    for cases, controls in product((0, 199, 200, 201), repeat=2):
        sql = retained_sql(str(cases), str(controls), min_cases=200, min_controls=200)
        assert con.execute(f"SELECT {sql}").fetchone()[0] is \
            is_retained(cases, controls, min_cases=200, min_controls=200), (cases, controls)


def _messy_sex_release(tmp_path: Path) -> Path:
    """A release whose `sex` column carries the spellings build_vocabulary will accept.

    Only Both/Male/Female get through the build (blank and 'M' are refused there), but
    surrounding whitespace does, and that is the case the two loaders had to agree on.
    """
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary

    write_csv(tmp_path / "m.csv", ["phecode", "ICD", "vocabulary_id"],
              [["MM_001", "A01.1", "ICD10CM"], ["FF_002", "A02.1", "ICD10CM"],
               ["BB_003", "A03.1", "ICD10CM"]])
    write_csv(tmp_path / "i.csv", ["phecode", "sex", "phecode_string", "category"],
              [["MM_001", "Male", "male-only", "X"], ["FF_002", " female ", "female-only", "X"],
               ["BB_003", "Both", "unrestricted", "X"]])
    release = tmp_path / "rel"
    build_vocabulary(tmp_path / "m.csv", tmp_path / "i.csv", release, None)
    return release


def test_only_genuinely_restricted_phecodes_are_loaded(tmp_path: Path) -> None:
    """'Both', blank and an unrecognised 'M' must be ABSENT, not present-and-ignored.

    eligible_count reaches its unrestricted branch by a missing key. A loader that kept
    'BOTH' or 'M' as a value would put a phecode in the dict that the CASE then failed
    to match -- unrestricted by accident rather than by rule.

    Read straight from a CSV relation rather than a built release: build_vocabulary
    refuses blank and 'M', so a release cannot carry them, but load_phecode_restrictions
    is documented to take any relation and this is the branch that decides what a
    missing key means.
    """
    from conftest import write_csv

    info = tmp_path / "raw_info.csv"
    write_csv(info, ["phecode", "sex"],
              [["MM_001", "Male"], ["FF_002", " female "], ["BB_003", "Both"],
               ["EE_004", ""], ["UU_005", "M"]])
    restrictions = load_phecode_restrictions(connect(), relation_for(info))
    assert restrictions == {"MM_001": "MALE", "FF_002": "FEMALE"}, restrictions


def test_the_mapper_and_the_loader_restrict_the_same_phecodes(tmp_path: Path) -> None:
    """The third rule the docstring claims one home for, checked where it is observable.

    The mapper fills its own `phecode_sex` table; the curve calls
    load_phecode_restrictions. Nothing compares them directly, but the mapper's
    restriction decision is visible in phecode_counts -- case_count +
    control_count_before_exclusions IS the evaluable denominator -- so run a real
    cohort through and require the two to produce the same number for every phecode.
    """
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes

    release = _messy_sex_release(tmp_path)
    people = ([[f"m{i:03d}", "Male"] for i in range(30)]
              + [[f"f{i:03d}", "Female"] for i in range(20)]
              + [[f"u{i:03d}", ""] for i in range(5)])
    cohort = tmp_path / "c.csv"
    write_csv(cohort, ["person_id", "sex"], people)
    # One case per phecode, from a person of a sex that can actually be evaluated for it.
    write_csv(tmp_path / "e.csv", ["person_id", "code", "vocabulary"],
              [["m000", "A01.1", "ICD10CM"], ["f000", "A02.1", "ICD10CM"],
               ["m001", "A03.1", "ICD10CM"]])
    run = tmp_path / "run"
    map_phecodes(release, cohort, tmp_path / "e.csv", run, min_cases=1, min_controls=0)

    con = connect()
    restrictions = load_phecode_restrictions(
        con, relation_for(release / "phecode_info.parquet"))
    rows = con.execute(
        "SELECT phecode, case_count + control_count_before_exclusions FROM "
        f"{relation_for(run / 'phecode_counts.parquet')} ORDER BY phecode").fetchall()
    assert len(rows) == 3, rows
    for phecode, mapper_denominator in rows:
        assert mapper_denominator == eligible_count(
            restrictions.get(phecode), n_male=30, n_female=20, n_all=55), phecode
    # Named explicitly too: an agreement where both sides restrict nothing would pass
    # the loop above while proving the opposite of what this test is for. The 5
    # unknown-sex people are in n_all and in neither restricted denominator.
    assert dict(rows) == {"MM_001": 30, "FF_002": 20, "BB_003": 55}
