"""Tests that kill specific mutation survivors.

A sampled mutation run over the source (50 mutants, 48 executable) scored 65%:
31 killed, 17 survived. Most survivors were cosmetic (wording of a warning) or
equivalent (mapper.py:497's trim() is redundant because the regexp already
strips \\s; mapper.py:29's .upper() is a no-op because DuckDB's DESCRIBE already
returns uppercase types -- both verified, and unkillable by anyone).

Five survivors were not cosmetic. Each would let a wrong number through, and each
is killed by a test below. They cluster in de-duplication and threshold wiring,
which is where a defect produces a plausible number rather than a crash.

One of them, validation.py:163, is in code written the same day as the fix it
belongs to -- the commit message claimed it was tested. It was covered only for
the branch that fires, not the branch that must not.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.validation import validate_phecodex_counts
from phecodex_mapper.vocabulary import build_vocabulary

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 1. validation.py:104 -- count(*) - count(DISTINCT person_id)
#    Dropping DISTINCT makes this identically zero, so duplicate detection is
#    silently dead. Verified: on ('A','p1'),('A','p1'),('A','p2') the correct
#    expression gives 1 and the mutant gives 0.
# --------------------------------------------------------------------------

def _release(tmp_path: Path, name: str = "rel") -> Path:
    source = tmp_path / f"{name}_map.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / f"{name}_info.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "Hypertension", "Cardiovascular"]])
    out = tmp_path / name
    build_vocabulary(source, info, out, None)
    return out


def _external(path: Path) -> Path:
    write_csv(path, ["phecode", "description", "sex", "ancestry", "case_count",
                     "control_count", "sample_count", "source", "source_version"],
              [["CV_003", "Hypertension", "Both", "ALL", "20", "180", "200", "ext", "1"]])
    return path


def test_duplicate_person_phecode_rows_are_detected(tmp_path: Path) -> None:
    """Behaviour test, NOT a mutation guard -- the mutant there is cosmetic.

    I first reported validation.py:104's surviving mutant as "duplicate detection
    silently disabled", having checked the arithmetic of
    count(*) - count(DISTINCT person_id) in isolation. That was wrong: the
    HAVING clause on the same line does the detection independently, so dropping
    DISTINCT from the SELECT changes only the number quoted in the error message.
    The check still fires. This test pins that it fires, which is worth having on
    its own terms.
    """
    release = _release(tmp_path)
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"], ["p3", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    run = tmp_path / "run"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=0)

    # Corrupt person_phecodes exactly as a bad upstream join would: p1 twice.
    pp = run / "person_phecodes.parquet"
    duckdb.sql(f"""COPY (SELECT * FROM read_parquet('{pp}')
                         UNION ALL SELECT * FROM read_parquet('{pp}'))
                   TO '{pp}' (FORMAT PARQUET)""")

    with pytest.raises(ValueError, match="duplicate person/phecode"):
        validate_phecodex_counts(run, release, _external(tmp_path / "ext.csv"), tmp_path / "val")


def test_a_clean_run_passes_the_duplicate_check(tmp_path: Path) -> None:
    """Positive control: the check must not reject an honest run."""
    release = _release(tmp_path, "rel2")
    cohort, events = tmp_path / "c2.csv", tmp_path / "e2.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"], ["p3", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    run = tmp_path / "run2"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=0)
    validate_phecodex_counts(run, release, _external(tmp_path / "ext2.csv"), tmp_path / "val2")
    assert (tmp_path / "val2" / "phecodex_comparison.csv").is_file()


# --------------------------------------------------------------------------
# 2. mapper.py:215 -- SELECT DISTINCT when resolving category rules
#    Two category rules matching the same phecode would insert it twice,
#    inflating phecodes_excluded -- the counter fixed in f061cc1.
# --------------------------------------------------------------------------

def test_two_rules_matching_one_phecode_count_it_once(tmp_path: Path) -> None:
    """Behaviour test, NOT a mutation guard -- mapper.py:215's mutant is equivalent.

    The two branches are combined with UNION rather than UNION ALL, which already
    deduplicates across both, so the inner SELECT DISTINCT is redundant and no
    test can kill its removal. Kept because the behaviour it asserts -- one
    phecode matched by two rules counts once -- is worth pinning regardless.
    """
    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["SS_004", "R10", "ICD10CM"], ["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "i.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["SS_004", "Both", "Abdominal pain", "Symptoms"],
               ["CV_003", "Both", "Hypertension", "Cardiovascular"]])
    release = tmp_path / "rel3"
    build_vocabulary(source, info, release, None)

    cohort, events = tmp_path / "c3.csv", tmp_path / "e3.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    drop = tmp_path / "drop.csv"
    # Both rules resolve to SS_004: once by category, once by name.
    write_csv(drop, ["match_type", "match_value"],
              [["category", "Symptoms"], ["phecode", "SS_004"]])

    out = tmp_path / "run3"
    map_phecodes(release, cohort, events, out, exclude_phenotypes=drop, min_cases=1, min_controls=0)
    summary = json.loads((out / "audit.json").read_text())["exclude_phenotypes"]
    assert summary["phecodes_excluded"] == 1, "one phecode matched by two rules was counted twice"


# --------------------------------------------------------------------------
# 3. validation.py:163 -- the sex note must not fire on unrestricted phecodes
#    Written in f061cc1, whose message says it is tested. Only the branch that
#    fires was covered; the branch that must NOT fire was not.
# --------------------------------------------------------------------------

def test_the_sex_note_never_fires_on_an_unrestricted_phecode(tmp_path: Path) -> None:
    """MUTATION GUARD for validation.py:163.

    A 'Both' phecode has no sex restriction, so no denominator caveat applies to
    it. Flipping the case-fold makes the comparison 'both' != 'BOTH' -- true --
    and the note appears on every row.
    """
    # The note is gated on `not local_denominator_is_sex_aware`, so it can only be
    # reached by a run whose release carries NO sex metadata. A fixture with sex
    # metadata short-circuits the condition and cannot distinguish the case-fold
    # at all -- which is how the first version of this test passed under the mutant.
    source = tmp_path / "s4.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "I10", "ICD10CM"]])
    info_nosex = tmp_path / "i4.csv"
    write_csv(info_nosex, ["phecode", "phecode_string", "category"],
              [["CV_003", "Hypertension", "Cardiovascular"]])
    release = tmp_path / "rel4"
    build_vocabulary(source, info_nosex, release, None)

    cohort, events = tmp_path / "c4.csv", tmp_path / "e4.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"], ["p3", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    run = tmp_path / "run4"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=0)
    assert json.loads((run / "audit.json").read_text())["sex"]["release_has_sex_metadata"] is False

    # Give the validator the sex column it requires, with CV_003 unrestricted.
    info_parquet = release / "phecode_info.parquet"
    duckdb.sql(f"""COPY (SELECT phecode, 'Both' AS sex, phecode_string, category
                         FROM read_parquet('{info_parquet}')) TO '{info_parquet}' (FORMAT PARQUET)""")
    out = tmp_path / "val4"
    validate_phecodex_counts(run, release, _external(tmp_path / "ext4.csv"), out)

    import csv
    with open(out / "phecodex_comparison.csv") as fh:
        rows = list(csv.DictReader(fh))
    both = [r for r in rows if r["phecode"] == "CV_003"]
    assert both, "the unrestricted phecode should appear in the comparison"
    for row in both:
        assert "not_sex_restricted" not in row["notes"], \
            "a sex caveat was attached to a phecode that carries no sex restriction"


# --------------------------------------------------------------------------
# 4. vocabulary.py:161 -- the SNOMED concept filter
#    Every existing Athena fixture is homogeneous (all SNOMED, all standard,
#    all valid), so AND could become OR with nothing noticing. On a real extract
#    that widens the concept set enormously.
# --------------------------------------------------------------------------

def test_non_standard_and_invalid_concepts_are_excluded(tmp_path: Path) -> None:
    """MUTATION GUARD for vocabulary.py:161.

    The fixture deliberately mixes what the filter must reject: a non-standard
    SNOMED concept, an invalid one, and a non-SNOMED concept sharing the shape.
    Only the standard, valid SNOMED concept may reach the bridge.
    """
    source = tmp_path / "s.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["ID_052", "B02.31", "ICD10CM"]])
    athena = tmp_path / "athena"
    athena.mkdir()
    write_csv(athena / "CONCEPT.csv",
              ["concept_id", "concept_code", "vocabulary_id", "domain_id",
               "standard_concept", "invalid_reason"],
              [[1, "10698009", "SNOMED", "Condition", "S", ""],      # keep
               [2, "B02.31", "ICD10CM", "Condition", "", ""],
               [3, "99999001", "SNOMED", "Condition", "", ""],       # not standard
               [4, "99999002", "SNOMED", "Condition", "S", "D"],     # invalid
               [5, "99999003", "OTHER", "Condition", "S", ""]])      # not SNOMED
    write_csv(athena / "CONCEPT_RELATIONSHIP.csv",
              ["concept_id_1", "concept_id_2", "relationship_id", "invalid_reason"],
              [[2, 1, "Maps to", ""], [2, 3, "Maps to", ""],
               [2, 4, "Maps to", ""], [2, 5, "Maps to", ""]])

    release = tmp_path / "rel5"
    build_vocabulary(source, None, release, athena)
    codes = {r[0] for r in duckdb.sql(
        f"SELECT DISTINCT source_code FROM read_parquet('{release / 'snomed_map.parquet'}')").fetchall()}
    assert codes == {"10698009"}, f"non-standard, invalid or non-SNOMED concepts leaked in: {codes}"


# --------------------------------------------------------------------------
# 5. min_cases / min_controls wiring -- cli.py:122,127 and mapper.py:610
#    Swappable at the CLI boundary and in audit.json with nothing asserting it.
# --------------------------------------------------------------------------

def test_audit_records_the_thresholds_it_was_given(tmp_path: Path) -> None:
    """MUTATION GUARD for mapper.py:610. Deliberately unequal values."""
    release = _release(tmp_path, "rel6")
    cohort, events = tmp_path / "c6.csv", tmp_path / "e6.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    out = tmp_path / "run6"
    map_phecodes(release, cohort, events, out, min_cases=1, min_controls=0)
    audit = json.loads((out / "audit.json").read_text())
    assert audit["min_cases"] == 1
    assert audit["min_controls"] == 0


def test_the_cli_passes_the_two_thresholds_distinctly(tmp_path: Path) -> None:
    """MUTATION GUARD for cli.py:122/127.

    The values must differ, or a swap at the CLI boundary is invisible. A
    phecode with 1 case and 1 control is retained under (1,1) and dropped under
    (2,1), so the swap changes the output rather than only the audit.
    """
    release = _release(tmp_path, "rel7")
    cohort, events = tmp_path / "c7.csv", tmp_path / "e7.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    out = tmp_path / "run7"
    result = subprocess.run(
        [sys.executable, "-m", "phecodex_mapper.cli", "map-phecodes",
         "--release", str(release), "--cohort", str(cohort), "--events", str(events),
         "--output", str(out), "--min-cases", "1", "--min-controls", "1",
         "--max-unmapped-rate", "1.0"],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    assert result.returncode == 0, result.stderr

    audit = json.loads((out / "audit.json").read_text())
    assert (audit["min_cases"], audit["min_controls"]) == (1, 1)

    out2 = tmp_path / "run8"
    result = subprocess.run(
        [sys.executable, "-m", "phecodex_mapper.cli", "map-phecodes",
         "--release", str(release), "--cohort", str(cohort), "--events", str(events),
         "--output", str(out2), "--min-cases", "2", "--min-controls", "1",
         "--max-unmapped-rate", "1.0"],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    assert result.returncode == 0, result.stderr
    audit2 = json.loads((out2 / "audit.json").read_text())
    assert (audit2["min_cases"], audit2["min_controls"]) == (2, 1)

    # And the thresholds must actually bite differently: 1 case is enough at
    # min_cases=1 and not at min_cases=2.
    retained = lambda d: duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{d / 'phecode_counts.parquet'}') WHERE retained").fetchone()[0]
    assert retained(out) == 1
    assert retained(out2) == 0
