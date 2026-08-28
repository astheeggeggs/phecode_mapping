"""Tests for --recover-unmapped: adding codes the published map omits.

PhecodeX's WHO ICD-10 map holds 8,560 distinct codes against ICD-10-CM's 55,338,
and WHO retires codes the map never catches up with -- I84.x haemorrhoids was
reclassified to K64.x, so a cohort spanning 2000-2022 loses every older
haemorrhoid episode silently. Measured on a 2.6M-event UK Biobank extract,
recovery took unmapped from 23.74% to 20.26%: 90,503 events across 1,007 codes.

Two routes supply evidence, and only evidence already inside the release:

    cross_vocabulary  the same code carries phecodes under another vocabulary
    snomed_bridge     the code maps to a SNOMED concept the bridge accepted

The rules under test:
  * both routes agreeing, or only one firing, is enough
  * routes that DISAGREE are skipped unless adjudicated -- guessing between two
    contradicting sources is the inference this tool refuses to make elsewhere
  * recovery is purely additive and never rewrites a published assignment
  * recovery runs after the SNOMED bridge and never feeds back into it, so it
    cannot bootstrap itself
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.vocabulary import build_vocabulary


def _athena(directory: Path, concepts: list[list], relationships: list[list]) -> Path:
    directory.mkdir()
    write_csv(directory / "CONCEPT.csv",
              ["concept_id", "concept_code", "vocabulary_id", "domain_id", "standard_concept", "invalid_reason"],
              concepts)
    write_csv(directory / "CONCEPT_RELATIONSHIP.csv",
              ["concept_id_1", "concept_id_2", "relationship_id", "invalid_reason"], relationships)
    return directory


def _map(release: Path) -> set[tuple[str, str, str]]:
    return {(r[0], r[1], r[2]) for r in duckdb.sql(
        f"SELECT vocabulary, normalized_code, phecode FROM read_parquet('{release / 'icd_map.parquet'}')").fetchall()}


@pytest.fixture
def fixture(tmp_path: Path):
    """A01.1 is mapped under ICD10CM only; B02.1 is unmapped but bridges via SNOMED.

    Both are absent from the ICD10 (WHO) side of the map, which is the real shape:
    PhecodeX's WHO map is far sparser than its CM map.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "A01.1", "ICD10CM"], ["ID_052", "C03.3", "ICD10CM"]])
    athena = _athena(tmp_path / "athena", concepts=[
        [1, "A01.1", "ICD10CM", "Condition", "", ""],
        [2, "A01.1", "ICD10", "Condition", "", ""],     # same code, WHO side, unmapped
        [3, "B02.1", "ICD10", "Condition", "", ""],     # unmapped; reaches a bridged concept
        [4, "C03.3", "ICD10CM", "Condition", "", ""],
        [5, "77386006", "SNOMED", "Condition", "S", ""],
    ], relationships=[
        [4, 5, "Maps to", ""],   # the only mapped source for the concept -> unanimous
        [3, 5, "Maps to", ""],   # B02.1 reaches the same concept
    ])
    return source, athena


def test_without_the_flag_the_map_is_exactly_the_published_one(tmp_path: Path, fixture) -> None:
    """Recovery must be opt-in; the default stays faithful to what PhecodeX published."""
    source, athena = fixture
    release = tmp_path / "plain"
    build_vocabulary(source, None, release, athena)
    assert ("ICD10", "A011", "CV_003") not in _map(release)
    assert json.loads((release / "manifest.json").read_text())["recovery"] is None
    assert not (release / "recovered_codes.csv").exists()


def test_cross_vocabulary_route_recovers_a_code(tmp_path: Path, fixture) -> None:
    """A01.1 is mapped under ICD10CM, so the WHO side can take the same assignment."""
    source, athena = fixture
    release = tmp_path / "rec"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)
    assert ("ICD10", "A011", "CV_003") in _map(release)
    summary = json.loads((release / "manifest.json").read_text())["recovery"]
    assert summary["codes_added_by_route"].get("cross_vocabulary", 0) >= 1


def test_snomed_route_recovers_a_code_absent_from_every_vocabulary(tmp_path: Path, fixture) -> None:
    """B02.1 is in no vocabulary's map; it is recovered only via the bridge."""
    source, athena = fixture
    release = tmp_path / "rec2"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)
    assert ("ICD10", "B021", "ID_052") in _map(release)


def test_recovery_is_purely_additive(tmp_path: Path, fixture) -> None:
    """It must never remove or rewrite a published assignment.

    The whole justification for doing this at build time is that the result stays
    auditable against the published map; silently altering an existing row would
    destroy that.
    """
    source, athena = fixture
    plain, rec = tmp_path / "a", tmp_path / "b"
    build_vocabulary(source, None, plain, athena)
    build_vocabulary(source, None, rec, athena, recover_unmapped=True)
    assert _map(plain) <= _map(rec), "recovery dropped or rewrote a published row"


def test_recovery_does_not_feed_back_into_the_snomed_bridge(tmp_path: Path, fixture) -> None:
    """No circularity: the bridge is built from the published map alone.

    If recovered rows reached the bridge, a recovered code could widen a concept's
    source set and change which phecodes that concept implies -- recovery
    bootstrapping itself.
    """
    source, athena = fixture
    plain, rec = tmp_path / "c", tmp_path / "d"
    build_vocabulary(source, None, plain, athena)
    build_vocabulary(source, None, rec, athena, recover_unmapped=True)
    rows = lambda p: duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{p / 'snomed_map.parquet'}')").fetchone()[0]
    assert rows(plain) == rows(rec)


def test_conflicting_routes_are_skipped_not_guessed(tmp_path: Path) -> None:
    """The decisive rule: two sources disagreeing is not resolved by picking one."""
    source = tmp_path / "conflict.csv"
    # D01.1 is mapped to GI_001 under CM; via SNOMED it would imply CV_003.
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GI_001", "D01.1", "ICD10CM"], ["CV_003", "E01.1", "ICD10CM"]])
    athena = _athena(tmp_path / "athena_c", concepts=[
        [1, "D01.1", "ICD10CM", "Condition", "", ""],
        [2, "D01.1", "ICD10", "Condition", "", ""],
        [3, "E01.1", "ICD10CM", "Condition", "", ""],
        [4, "11111004", "SNOMED", "Condition", "S", ""],
    ], relationships=[[3, 4, "Maps to", ""], [2, 4, "Maps to", ""]])
    release = tmp_path / "conf"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)

    added = {c for v, c, _ in _map(release) if v == "ICD10"}
    assert "D011" not in added, "a conflicting code was recovered without adjudication"
    summary = json.loads((release / "manifest.json").read_text())["recovery"]
    assert summary["codes_skipped_unresolved_disagreement"] >= 1


def test_an_adjudication_file_resolves_a_conflict_both_ways(tmp_path: Path) -> None:
    """A selects the cross-vocabulary assignment, B the SNOMED route."""
    source = tmp_path / "adj.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GI_001", "D01.1", "ICD10CM"], ["CV_003", "E01.1", "ICD10CM"]])
    concepts = [[1, "D01.1", "ICD10CM", "Condition", "", ""], [2, "D01.1", "ICD10", "Condition", "", ""],
                [3, "E01.1", "ICD10CM", "Condition", "", ""], [4, "11111004", "SNOMED", "Condition", "S", ""]]
    rels = [[3, 4, "Maps to", ""], [2, 4, "Maps to", ""]]

    for choice, expected in (("A", "GI_001"), ("B", "CV_003")):
        athena = _athena(tmp_path / f"ath_{choice}", concepts, rels)
        verdicts = tmp_path / f"verdict_{choice}.csv"
        write_csv(verdicts, ["icd_code", "adjudication_A_or_B"], [["D01.1", choice]])
        release = tmp_path / f"rel_{choice}"
        build_vocabulary(source, None, release, athena, recover_unmapped=True,
                         recovery_adjudication=verdicts)
        got = {p for v, c, p in _map(release) if v == "ICD10" and c == "D011"}
        assert got == {expected}, f"verdict {choice} gave {got}, expected {expected}"


def test_the_adjudication_file_is_recorded_in_the_manifest(tmp_path: Path, fixture) -> None:
    """A map that depends on a review file must say which file, and its checksum."""
    source, athena = fixture
    verdicts = tmp_path / "v.csv"
    write_csv(verdicts, ["icd_code", "adjudication_A_or_B"], [["Z99.9", "A"]])
    release = tmp_path / "rel_m"
    build_vocabulary(source, None, release, athena, recover_unmapped=True, recovery_adjudication=verdicts)
    adj = json.loads((release / "manifest.json").read_text())["recovery"]["adjudication"]
    assert adj["path"] == str(verdicts)
    assert len(adj["sha256"]) == 64
    assert adj["verdicts"] == 1


def test_every_added_row_is_listed_with_its_route(tmp_path: Path, fixture) -> None:
    """recovered_codes.csv is the audit trail; the manifest count must match it."""
    source, athena = fixture
    release = tmp_path / "rel_a"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)
    rows = duckdb.sql(f"SELECT count(*) FROM read_csv_auto('{release / 'recovered_codes.csv'}')").fetchone()[0]
    assert rows == json.loads((release / "manifest.json").read_text())["recovery"]["rows_added"]
    columns = [r[0] for r in duckdb.sql(
        f"DESCRIBE SELECT * FROM read_csv_auto('{release / 'recovered_codes.csv'}')").fetchall()]
    assert "route" in columns and "phecode" in columns


def test_recovery_requires_athena(tmp_path: Path, fixture) -> None:
    """Both routes need the Athena vocabulary; failing loudly beats a silent no-op."""
    source, _ = fixture
    with pytest.raises(ValueError, match="requires --athena-dir"):
        build_vocabulary(source, None, tmp_path / "rel_no", None, recover_unmapped=True)


def test_cross_vocabulary_never_crosses_the_icd9_icd10_boundary(tmp_path: Path) -> None:
    """A code string shared by ICD-9 and ICD-10 is NOT the same code.

    The two generations reuse strings for unrelated diseases, and the collision is
    systematic rather than incidental: ICD-9's E chapter is external causes while
    ICD-10's is endocrine/metabolic, and ICD-9's V chapter is health status while
    ICD-10's is transport accidents. Matching on the bare string put 265 codes into
    a shipped release with the phecodes of an unrelated disease -- a pedestrian
    struck by a car recovered as `ID_097` drug-resistant infection.

    The same-era pair in this fixture is the positive control: if the guard is
    written too broadly it takes that with it, and the route stops doing its job.
    """
    source = tmp_path / "eras.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["ID_097", "V09.0", "ICD9CM"],    # ICD-9: infection resistant to penicillins
        ["EM_252", "E88.89", "ICD10CM"],  # ICD-10: other specified metabolic disorders
        ["CV_003", "A01.1", "ICD10CM"],   # same-era donor for the positive control
    ])
    athena = _athena(tmp_path / "athena_era", concepts=[
        [1, "V09.0", "ICD9CM", "Condition", "", ""],
        [2, "V09.0", "ICD10", "Condition", "", ""],    # pedestrian injured, unmapped
        [3, "E88.89", "ICD10CM", "Condition", "", ""],
        [4, "E888.9", "ICD9CM", "Condition", "", ""],  # unspecified fall, unmapped
        [5, "A01.1", "ICD10CM", "Condition", "", ""],
        [6, "A01.1", "ICD10", "Condition", "", ""],    # same code, same era, unmapped
    ], relationships=[])
    release = tmp_path / "era"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)
    got = _map(release)

    assert ("ICD10", "V090", "ID_097") not in got, \
        "an ICD-10 transport-accident code took an ICD-9 infection phecode"
    assert ("ICD9CM", "E8889", "EM_252") not in got, \
        "an ICD-9 fall code took an ICD-10 metabolic phecode"
    assert ("ICD10", "A011", "CV_003") in got, \
        "the guard also blocked ICD10CM -> ICD10, which is the route's whole purpose"
