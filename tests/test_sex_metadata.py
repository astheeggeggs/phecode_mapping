"""Regression tests: a broken sex configuration must be loud, not silent.

Three defects, one root cause. `has_sex` was the literal `True` at mapper.py:378,
never reassigned, so:

  * a cohort whose `sex` column was blank for everyone scored every sex-restricted
    phecode as 0 cases / 0 controls and dropped it from the matrix, with empty
    stdout and stderr;
  * a release whose `phecode_info` has no `sex` column left `phecode_sex` empty,
    degenerating EVALUABLE to TRUE and scoring every sex-specific phecode against
    the whole cohort -- reinstating the exact bug the sex fix removed;
  * the two audit fields designed to flag those cases were constants, so neither
    could ever fire, and the warning branch was unreachable.

The second is not hypothetical: the upstream phecodeX_info.csv has no `sex`
column, and scratchpad_ukb/release_ukb is such a release with a 2.6M-event run
recorded against it.

Each test below asserts a value that was previously constant, so each one fails
against the pre-fix code.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary

COHORT = [["f1", "Female"], ["f2", "Female"], ["m1", "Male"], ["m2", "Male"]]
EVENTS = [[p, "A01.1", "ICD10CM"] for p in ("f1", "f2", "m1")]


def _release(tmp_path: Path, *, with_sex: bool) -> Path:
    """A release carrying one Female-restricted phecode, with or without sex metadata."""
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["GU_001", "A01.1", "ICD10CM"]])
    info = None
    if with_sex:
        info = tmp_path / "info.csv"
        write_csv(info, ["phecode", "sex", "phecode_string", "category"],
                  [["GU_001", "Female", "Endometriosis", "Genitourinary"]])
    release = tmp_path / ("rel_sex" if with_sex else "rel_nosex")
    build_vocabulary(source, info, release, None)
    return release


def _run(tmp_path: Path, release: Path, cohort_rows: list, name: str) -> Path:
    cohort, events, out = tmp_path / f"c_{name}.csv", tmp_path / f"e_{name}.csv", tmp_path / f"run_{name}"
    write_csv(cohort, ["person_id", "sex"], cohort_rows)
    write_csv(events, ["person_id", "code", "vocabulary"], EVENTS)
    map_phecodes(release, cohort, events, out, min_cases=1, min_controls=0)
    return out


def test_blank_sex_for_everyone_is_refused_not_silently_dropped(tmp_path: Path) -> None:
    """The whole defect: 0 cases / 0 controls / no matrix column / no message."""
    release = _release(tmp_path, with_sex=True)
    blank = [[p, ""] for p, _ in COHORT]
    with pytest.raises(ValueError, match="no usable sex values"):
        _run(tmp_path, release, blank, "blank")


def test_the_refusal_names_the_phecodes_at_stake(tmp_path: Path) -> None:
    """An error that does not say what would have been lost invites a --force."""
    release = _release(tmp_path, with_sex=True)
    with pytest.raises(ValueError) as excinfo:
        _run(tmp_path, release, [[p, ""] for p, _ in COHORT], "blank2")
    message = str(excinfo.value)
    assert "1 phecode(s)" in message
    assert "silently dropped" in message


def test_partial_unknown_sex_is_allowed_but_counted(tmp_path: Path) -> None:
    """The realistic UKB case: some people have no genetic sex.

    Those people are correctly non-evaluable, so this must NOT raise -- but the
    count has to appear somewhere, or a case count deflated by the unknown-sex
    fraction looks identical to a rare phenotype.
    """
    release = _release(tmp_path, with_sex=True)
    partial = [["f1", "Female"], ["f2", ""], ["m1", "Male"], ["m2", ""]]
    out = _run(tmp_path, release, partial, "partial")
    audit = json.loads((out / "audit.json").read_text())
    assert audit["sex"]["n_unknown_sex"] == 2
    assert audit["sex"]["n_female"] == 1
    assert audit["phenotype_matrix"]["cohort_has_usable_sex"] is True

    # f2 has the code and is a real carrier, but with unknown sex cannot be
    # evaluated for a Female-restricted phecode: NA, never a control.
    matrix = dict(duckdb.sql(
        f'SELECT person_id, "GU_001" FROM read_parquet(\'{out / "phenotype_matrix.parquet"}\')').fetchall())
    assert matrix["f1"] == 1
    assert matrix["f2"] is None
    assert matrix["m2"] is None


def test_a_release_without_sex_metadata_warns_and_records_it(tmp_path: Path, capsys) -> None:
    """Silently unrestricting every phecode is the pre-fix bug returning."""
    release = _release(tmp_path, with_sex=False)
    out = _run(tmp_path, release, COHORT, "nosex")
    assert "no 'sex' column" in capsys.readouterr().err

    audit = json.loads((out / "audit.json").read_text())
    assert audit["sex"]["release_has_sex_metadata"] is False
    assert audit["sex"]["n_restricted_phecodes"] == 0

    # m1 carries the code and is scored a case for what should be a Female-only
    # phecode. That is the consequence of the missing metadata, and the warning
    # above is the only thing that makes it visible.
    cases = set(duckdb.sql(
        f"SELECT person_id FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchall())
    assert ("m1",) in cases


def test_audit_sex_fields_are_derived_not_constant(tmp_path: Path) -> None:
    """The decisive test: the same fields must differ between two runs.

    Pre-fix, cohort_has_usable_sex was hardwired True and
    sex_restricted_phecodes_treated_as_unrestricted was a constant 0, so no input
    could move either. Asserting a difference is what makes them load-bearing.
    """
    with_meta = _run(tmp_path, _release(tmp_path, with_sex=True), COHORT, "d1")
    without = _run(tmp_path, _release(tmp_path, with_sex=False), COHORT, "d2")
    a = json.loads((with_meta / "audit.json").read_text())
    b = json.loads((without / "audit.json").read_text())
    assert a["sex"]["release_has_sex_metadata"] != b["sex"]["release_has_sex_metadata"]
    assert a["sex"]["n_restricted_phecodes"] == 1
    assert b["sex"]["n_restricted_phecodes"] == 0


def test_a_phecode_info_identifier_that_only_nearly_matches_is_refused(tmp_path: Path) -> None:
    """A case or whitespace difference silently deletes a sex restriction.

    phecode_sex is joined on the exact identifier, so 'gu_001' against the map's
    'GU_001' contributes nothing: the phecode becomes unrestricted, opposite-sex
    people are scored as ordinary controls (0) instead of non-evaluable (blank), and
    audit.json still reports n_restricted_phecodes = 1 -- claiming a restriction that
    was never applied. Measured before the fix: males went from blank to 0.

    Info legitimately carries phecodes the map does not, so the check is narrow: an
    info phecode that matches a map phecode ONLY after normalising is a formatting
    mismatch, not an extra row.
    """
    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["GU_001", "A01.1", "ICD10CM"]])
    for index, bad in enumerate(("gu_001", " GU_001", "GU_001 ")):
        info = tmp_path / f"i_{index}.csv"
        write_csv(info, ["phecode", "sex", "phecode_string", "category"],
                  [[bad, "Female", "Endometriosis", "Genitourinary"]])
        with pytest.raises(ValueError, match="differ from the map.s by case or whitespace"):
            build_vocabulary(source, info, tmp_path / f"rel_{index}", None)


def test_info_may_still_carry_phecodes_the_map_does_not(tmp_path: Path) -> None:
    """Negative control: the published info file covers the whole phenome.

    Refusing extra rows would reject every real release, which would make the guard
    above useless in practice.
    """
    source = tmp_path / "m2.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["GU_001", "A01.1", "ICD10CM"]])
    info = tmp_path / "i2.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["GU_001", "Female", "Endometriosis", "Genitourinary"],
               ["ZZ_999", "Male", "Never mapped", "Other"]])
    release = tmp_path / "rel_extra"
    build_vocabulary(source, info, release, None)
    assert (release / "phecode_info.parquet").is_file()


def test_cohort_has_usable_sex_is_false_when_it_should_be(tmp_path: Path) -> None:
    """The field this file exists to keep honest was never asserted False.

    test_audit_sex_fields_are_derived_not_constant reasons about it in its docstring
    but reads only release_has_sex_metadata and n_restricted_phecodes, so hardcoding
    cohort_has_usable_sex back to a literal True -- the original defect -- left the
    whole suite green. The configuration is reachable: a release with no sex metadata
    (so nothing is restricted, and the hard guard does not fire) against a cohort whose
    sex column is entirely blank.
    """
    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "i.csv"          # no `sex` column at all
    write_csv(info, ["phecode", "phecode_string", "category"], [["CV_003", "Hypertension", "CV"]])
    release = tmp_path / "rel_nosex"
    build_vocabulary(source, info, release, None)

    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", ""], ["p2", ""], ["p3", ""]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    out = tmp_path / "out_nosex"
    map_phecodes(release, cohort, events, out, min_cases=1, min_controls=1)

    matrix_info = json.loads((out / "audit.json").read_text())["phenotype_matrix"]
    assert matrix_info["cohort_has_usable_sex"] is False
    assert json.loads((out / "audit.json").read_text())["sex"]["n_unknown_sex"] == 3


def test_a_wrong_sex_carrier_is_not_reported_as_an_excluded_control(tmp_path: Path) -> None:
    """excluded_control_count is the column that explains the control denominator.

    The sex filter on it had no test: test_sex_restriction reasons about exactly this
    number in a comment but its helper never selects the column, so deleting the filter
    changed the shipped value in phecode_counts and eligible_phecodes.xlsx and the whole
    suite stayed green. A male carrier of a Female-only phecode is not evaluable, so he
    cannot be an excluded control for it.
    """
    source = tmp_path / "ms.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GU_001", "A01.1", "ICD10CM"], ["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "is.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["GU_001", "Female", "Endometriosis", "Genitourinary"],
               ["CV_003", "Both", "Hypertension", "Cardiovascular"]])
    release = tmp_path / "rel_ec"
    build_vocabulary(source, info, release, None)

    cohort, events = tmp_path / "c_ec.csv", tmp_path / "e_ec.csv"
    write_csv(cohort, ["person_id", "sex"],
              [["f1", "Female"], ["f2", "Female"], ["m1", "Male"], ["m2", "Male"]])
    # f2 and m2 carry the exclusion code but are not cases; only f2 is evaluable for GU_001.
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["f1", "A01.1", "ICD10CM"], ["f2", "I10", "ICD10CM"], ["m2", "I10", "ICD10CM"]])
    exclusions = tmp_path / "x.csv"
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
              [["GU_001", "code", "I10", "ICD10CM"]])
    out = tmp_path / "out_ec"
    map_phecodes(release, cohort, events, out, exclusions=exclusions, min_cases=1, min_controls=1)

    excluded = duckdb.sql(
        f"SELECT excluded_control_count FROM read_parquet('{out / 'phecode_counts.parquet'}')"
        " WHERE phecode = 'GU_001'").fetchone()[0]
    assert excluded == 1, f"expected only the evaluable female carrier, got {excluded}"
