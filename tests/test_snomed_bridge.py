"""Regression tests: the SNOMED bridge must not invert a many-to-one mapping.

OMOP maps ICD -> SNOMED many-to-one: where no exact SNOMED equivalent exists, many
specific ICD codes collapse onto the nearest broader concept. 410 ICD codes map to
"Third trimester pregnancy" alone, among them O10.x (pre-existing hypertension
complicating pregnancy) and O99.0x (anaemia complicating pregnancy). PhecodeX
rightly gives those codes BOTH a pregnancy phecode and the underlying disease
phecode -- forwards that is correct, since a woman coded O10.013 really does have
hypertension.

The bridge previously took the union of every source code's phecodes, which read
that backwards: one routine antenatal SNOMED code made a patient a case for 144
phecodes including hypertension, anaemia and autoimmune disease.

A SNOMED concept tells you only that the patient has *something* in the collapsed
set, so it implies a phecode only if EVERY source code implies it -- the
intersection. The fixtures below reproduce that shape in miniature.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary


def _athena(directory: Path, concepts: list[list], relationships: list[list]) -> Path:
    directory.mkdir()
    write_csv(directory / "CONCEPT.csv",
              ["concept_id", "concept_code", "vocabulary_id", "domain_id", "standard_concept", "invalid_reason"],
              concepts)
    write_csv(directory / "CONCEPT_RELATIONSHIP.csv",
              ["concept_id_1", "concept_id_2", "relationship_id", "invalid_reason"], relationships)
    return directory


@pytest.fixture
def collapsed_release(tmp_path: Path) -> Path:
    """Two ICD codes collapsing onto one SNOMED concept, as OMOP really does it.

    O10.013 "pre-existing hypertension complicating pregnancy, third trimester"
      -> pregnancy phecode PP_001 AND hypertension phecode CV_401
    O09.033 "supervision of pregnancy, third trimester"
      -> pregnancy phecode PP_001 only

    Both map to SNOMED 41587001 "Third trimester pregnancy". The concept therefore
    supports PP_001 (both agree) but NOT CV_401 (only one implies it).
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["PP_001", "O10.013", "ICD10CM"],
        ["CV_401", "O10.013", "ICD10CM"],
        ["PP_001", "O09.033", "ICD10CM"],
    ])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "41587001", "SNOMED", "Condition", "S", ""],
        [2, "O10.013", "ICD10CM", "Condition", "", ""],
        [3, "O09.033", "ICD10CM", "Condition", "", ""],
    ], relationships=[
        [2, 1, "Maps to", ""],
        [3, 1, "Maps to", ""],
    ])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    return release


def _bridge(release: Path) -> set[tuple[str, str]]:
    return set(duckdb.sql(
        f"SELECT source_code, phecode FROM read_parquet('{release / 'snomed_map.parquet'}')").fetchall())


def test_a_collapsed_concept_keeps_only_what_every_source_code_implies(collapsed_release: Path) -> None:
    """The whole defect, in four rows of fixture."""
    assert _bridge(collapsed_release) == {("41587001", "PP_001")}, \
        "the concept must not inherit a phecode only one of its source codes supports"


def test_a_one_to_one_concept_keeps_every_phecode(tmp_path: Path) -> None:
    """The intersection must not penalise genuine equivalences.

    A single ICD code mapping to a single SNOMED concept is a real equivalence, and
    a code legitimately carrying several phecodes keeps all of them.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["ID_052", "B02.31", "ICD10CM"],   # herpes zoster iridocyclitis really is
        ["SO_367", "B02.31", "ICD10CM"],   # both an infection and an eye disorder
    ])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "10698009", "SNOMED", "Condition", "S", ""],
        [2, "B02.31", "ICD10CM", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    assert _bridge(release) == {("10698009", "ID_052"), ("10698009", "SO_367")}


def test_a_wholly_ambiguous_concept_maps_to_nothing(tmp_path: Path) -> None:
    """When no phecode is common to every source code, the concept maps nowhere.

    That is the correct outcome, not a failure: the concept genuinely carries no
    information about which of its source conditions the patient has.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["CV_401", "O10.013", "ICD10CM"],
        ["BI_164", "O99.013", "ICD10CM"],
    ])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "41587001", "SNOMED", "Condition", "S", ""],
        [2, "O10.013", "ICD10CM", "Condition", "", ""],
        [3, "O99.013", "ICD10CM", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""], [3, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    assert _bridge(release) == set()

    manifest = json.loads((release / "manifest.json").read_text())
    summary = manifest["snomed_bridge"]
    assert summary["mappings_dropped_as_ambiguous"] == 2
    assert summary["snomed_codes_left_with_no_phecode"] == 1


def test_an_ambiguous_concept_produces_no_case_end_to_end(tmp_path: Path, collapsed_release: Path) -> None:
    """The consequence that matters: a patient coded only with the broad concept.

    Previously this person was a hypertension case on the strength of an antenatal
    code. They should be a case for the pregnancy phecode both source codes agree
    on, and nothing else.
    """
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [["patient-A", "Female"], ["patient-B", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["patient-A", "41587001", "SNOMED"]])
    map_phecodes(collapsed_release, cohort, events, output, min_cases=1, min_controls=0)

    cases = set(duckdb.sql(
        f"SELECT person_id, phecode FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchall())
    assert cases == {("patient-A", "PP_001")}
    assert ("patient-A", "CV_401") not in cases, "antenatal code made the patient a hypertension case"


def test_bridge_records_how_many_mappings_it_discarded(collapsed_release: Path) -> None:
    """Dropping mappings silently would trade one invisible decision for another."""
    manifest = json.loads((collapsed_release / "manifest.json").read_text())
    summary = manifest["snomed_bridge"]
    assert summary["rule"].startswith("phecode retained only where every source ICD code")
    assert summary["mappings_dropped_as_ambiguous"] == 1   # 41587001 -> CV_401
    assert summary["snomed_codes_left_with_no_phecode"] == 0


# ---------------------------------------------------------------------------
# The intersection rule was applied against the wrong universe.
#
# snomed_bridge_triples is inner-joined to icd_map, so computing
# n_source_icd_codes from it made BOTH sides of the HAVING enumerate only the
# codes PhecodeX happens to map. The equality was then satisfied trivially and
# the concept inherited whatever slice of its sources the map covered -- the
# same many-to-one inversion the rule exists to prevent.
#
# Every fixture above puts 100% of each concept's Athena sources in the map, so
# none of them exercises this path. That is why the defect survived a suite
# specifically written for this bridge. Measured on a full Athena extract:
# 799 of 13,397 concepts (6.0%) had a truncated denominator, and correcting it
# removes 1,635 mappings across 801 concepts.
# ---------------------------------------------------------------------------

def test_a_source_code_absent_from_the_map_still_counts_against_the_concept(tmp_path: Path) -> None:
    """The minimal repro: two sources, one mapped, one not.

    SNOMED 7895008 "Poisoning by drug" collapses both an intentional-self-harm
    code and an accidental-poisoning code. PhecodeX maps only the first. The
    concept therefore says nothing about intent and must imply no phecode --
    but if the accidental code is dropped from the denominator, the self-harm
    phecode is retained on a unanimity of one.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["MB_284.2", "T39.012", "ICD10CM"],   # intentional self-harm -- in the map
        # 965.1 (accidental poisoning) deliberately absent, as in the real map.
        # An unrelated ICD9CM row so the release SHIPS ICD9CM: the universe is
        # filtered to vocabularies the map covers, so without this the absent
        # 965.1 would be correctly ignored rather than counted.
        ["ID_001", "008.45", "ICD9CM"],
    ])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "7895008", "SNOMED", "Condition", "S", ""],
        [2, "T39.012", "ICD10CM", "Condition", "", ""],
        [3, "965.1", "ICD9CM", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""], [3, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)

    assert _bridge(release) == set(), \
        "the concept kept a phecode only one of its two source codes implies"


def test_a_fully_covered_concept_is_unaffected(tmp_path: Path) -> None:
    """Positive control: the fix must not simply empty the bridge.

    Where the map covers every source code and they agree, the mapping stands.
    Verified on real data too: SNOMED 10698009 "Herpes zoster iridocyclitis"
    keeps all 10 of its phecodes under both the old and the corrected rule.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["ID_052", "B02.31", "ICD10CM"],
        ["ID_052", "B02.32", "ICD10CM"],
    ])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "10698009", "SNOMED", "Condition", "S", ""],
        [2, "B02.31", "ICD10CM", "Condition", "", ""],
        [3, "B02.32", "ICD10CM", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""], [3, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    assert _bridge(release) == {("10698009", "ID_052")}


def test_partial_map_coverage_is_recorded_in_the_manifest(tmp_path: Path) -> None:
    """Dropping mappings for a reason nobody can see trades one silence for another."""
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["MB_284.2", "T39.012", "ICD10CM"], ["ID_001", "008.45", "ICD9CM"]])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "7895008", "SNOMED", "Condition", "S", ""],
        [2, "T39.012", "ICD10CM", "Condition", "", ""],
        [3, "965.1", "ICD9CM", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""], [3, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    summary = json.loads((release / "manifest.json").read_text())["snomed_bridge"]
    assert summary["snomed_codes_with_partial_map_coverage"] == 1
    # The rule string must describe what the code does. The previous wording said
    # "every source ICD code" while the denominator counted only mapped ones.
    assert "absent from the PhecodeX map" in summary["rule"]


def test_a_source_in_an_unshipped_vocabulary_does_not_empty_the_bridge(tmp_path: Path) -> None:
    """The opposite failure: too WIDE a universe makes the rule unsatisfiable.

    A SNOMED concept also collapsing a vocabulary PhecodeX does not ship (here
    ICD-O) must not be penalised for it -- such a code could never imply a
    phecode, so counting it would silently empty the bridge.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["ID_052", "B02.31", "ICD10CM"]])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "10698009", "SNOMED", "Condition", "S", ""],
        [2, "B02.31", "ICD10CM", "Condition", "", ""],
        [3, "8000/0", "ICDO3", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""], [3, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    assert _bridge(release) == {("10698009", "ID_052")}


def test_an_ambiguous_concept_produces_no_case_end_to_end_under_truncation(tmp_path: Path) -> None:
    """The consequence that matters, on the real release.

    A patient whose only event is the generic SNOMED poisoning concept was a case
    for MB_284 and MB_284.2 ("Suicide and self-inflicted harm") under the
    truncated denominator, and is a case for nothing under the corrected one.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["MB_284.2", "T39.012", "ICD10CM"], ["ID_001", "008.45", "ICD9CM"]])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "7895008", "SNOMED", "Condition", "S", ""],
        [2, "T39.012", "ICD10CM", "Condition", "", ""],
        [3, "965.1", "ICD9CM", "Condition", "", ""],
    ], relationships=[[2, 1, "Maps to", ""], [3, 1, "Maps to", ""]])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)

    cohort, events, output = tmp_path / "c.csv", tmp_path / "e.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [["patient-A", "Female"], ["patient-B", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["patient-A", "7895008", "SNOMED"]])
    map_phecodes(release, cohort, events, output, min_cases=1, min_controls=0, max_unmapped_rate=1.0)

    cases = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchone()[0]
    assert cases == 0, "a generic poisoning code made the patient a self-harm case"
