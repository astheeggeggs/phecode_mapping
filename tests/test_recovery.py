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


@pytest.fixture
def conflict(tmp_path: Path):
    """D01.1 is in conflict under ICD10: cross says GI_001, the SNOMED route CV_003."""
    source = tmp_path / "c.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GI_001", "D01.1", "ICD10CM"], ["CV_003", "E01.1", "ICD10CM"]])
    concepts = [[1, "D01.1", "ICD10CM", "Condition", "", ""], [2, "D01.1", "ICD10", "Condition", "", ""],
                [3, "E01.1", "ICD10CM", "Condition", "", ""], [4, "11111004", "SNOMED", "Condition", "S", ""]]
    return source, concepts, [[3, 4, "Maps to", ""], [2, 4, "Maps to", ""]]


def test_a_verdict_scoped_to_one_vocabulary_does_not_leak_to_another(tmp_path: Path, conflict) -> None:
    """The verdict file's `vocabulary` column is part of the key, not decoration.

    Joining on the bare code let one verdict close that code's conflict under every
    vocabulary at once, and -- worse -- let two rows for one code both match, so the
    build applied BOTH verdicts and added the union of the two contradicting routes.
    Here the ICD10-scoped `A` must decide, and the ICD10CM-scoped `B` must not fire.
    """
    source, concepts, rels = conflict
    verdicts = tmp_path / "scoped.csv"
    write_csv(verdicts, ["icd_code", "vocabulary", "adjudication_A_or_B"],
              [["D01.1", "ICD10", "A"], ["D01.1", "ICD10CM", "B"]])
    release = tmp_path / "scoped_rel"
    build_vocabulary(source, None, release, _athena(tmp_path / "ath_s", concepts, rels),
                     recover_unmapped=True, recovery_adjudication=verdicts)
    got = {p for v, c, p in _map(release) if v == "ICD10" and c == "D011"}
    assert got == {"GI_001"}, f"expected only the ICD10-scoped verdict to apply, got {got}"


@pytest.mark.parametrize("rows,why", [
    ([["D01.1", "ICD10", "A"], ["D011", "ICD10", "B"]], "same code and vocabulary, contradicting"),
    ([["D01.1", "ICD10", "A"], ["D01.1", "ICD10", "A"]], "same code and vocabulary, duplicated"),
    ([["D01.1", "", "A"], ["D01.1", "ICD10", "B"]], "a vocabulary-less row matches every vocabulary"),
])
def test_verdicts_that_could_both_match_one_code_are_rejected(tmp_path: Path, conflict, rows, why) -> None:
    """A review file must speak with one voice; two applicable verdicts is not a tie-break.

    Left unchecked these fanned the join out and inserted both contested phecode sets
    -- the exact guess between contradicting sources the feature refuses to make.
    """
    source, concepts, rels = conflict
    verdicts = tmp_path / "dup.csv"
    write_csv(verdicts, ["icd_code", "vocabulary", "adjudication_A_or_B"], rows)
    with pytest.raises(ValueError, match="more than one verdict"):
        build_vocabulary(source, None, tmp_path / f"dup_{abs(hash(why))}",
                         _athena(tmp_path / f"ath_{abs(hash(why))}", concepts, rels),
                         recover_unmapped=True, recovery_adjudication=verdicts)


def test_adjudication_without_recovery_is_refused_not_ignored(tmp_path: Path, fixture) -> None:
    """Reviewing conflicts and then not running recovery loses the reviewer's work."""
    source, athena = fixture
    verdicts = tmp_path / "v2.csv"
    write_csv(verdicts, ["icd_code", "adjudication_A_or_B"], [["D01.1", "A"]])
    with pytest.raises(ValueError, match="no effect without --recover-unmapped"):
        build_vocabulary(source, None, tmp_path / "noop", athena, recovery_adjudication=verdicts)


def test_recovery_counters_reconcile_against_the_map_itself(tmp_path: Path) -> None:
    """The manifest's counts must match what the shipped map actually gained.

    Comparing the manifest against `recovered_codes.csv` proves little -- both are
    written from the same intermediate. Diffing the two builds' maps is an
    independent second computation, and it also pins the units: `codes_added` counts
    code strings while `assignments_added` counts (code, vocabulary) pairs, and
    reporting one under the other's name is how a 2,121 that was really 2,204 hid.

    The fixture is built so the two units genuinely differ -- B02.1 is unmapped in
    both ICD-10 flavours and reaches a concept bridged from the ICD-9 side, so it is
    one code but two assignments. Without that the units test nothing, which is how
    the original pair of counters went eight months without anyone noticing they
    disagreed.
    """
    source = tmp_path / "units.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["ID_052", "003.3", "ICD9CM"]])
    athena = _athena(tmp_path / "ath_u", concepts=[
        [4, "003.3", "ICD9CM", "Condition", "", ""],
        [3, "B02.1", "ICD10", "Condition", "", ""],
        [6, "B02.1", "ICD10CM", "Condition", "", ""],
        [5, "77386006", "SNOMED", "Condition", "S", ""],
    ], relationships=[[4, 5, "Maps to", ""], [3, 5, "Maps to", ""], [6, 5, "Maps to", ""]])
    plain, rec = tmp_path / "p", tmp_path / "r"
    build_vocabulary(source, None, plain, athena)
    build_vocabulary(source, None, rec, athena, recover_unmapped=True)
    gained = _map(rec) - _map(plain)
    summary = json.loads((rec / "manifest.json").read_text())["recovery"]
    assert len(gained) == summary["rows_added"]
    assert len({c for _, c, _ in gained}) == summary["codes_added"]
    assert len({(v, c) for v, c, _ in gained}) == summary["assignments_added"]
    assert summary["codes_added"] < summary["assignments_added"], \
        "fixture no longer separates the two units, so this test proves nothing"


def _snomed_rows(release: Path) -> int:
    return duckdb.sql(f"SELECT count(*) FROM read_parquet('{release / 'snomed_map.parquet'}')").fetchone()[0]


def test_the_snomed_bridge_cannot_be_widened_by_recovery(tmp_path: Path) -> None:
    """No circularity -- and this time the fixture can actually show it.

    Comparing snomed_map row counts between two builds proves nothing, because the
    file is written before recovery runs: the numbers are equal by construction. The
    invariant needs a concept that WOULD be retained if a recovered row reached the
    bridge. 11111004 has two source codes; only A01.1 is published, so the unanimity
    rule drops it. Recovery adds B05.5 under ICD10CM -- exactly the row that would
    complete the concept. The bridge must still be empty.
    """
    published = [["GI_001", "A01.1", "ICD10CM"], ["GI_001", "B05.5", "ICD10"]]
    concepts = [[1, "A01.1", "ICD10CM", "Condition", "", ""],
                [2, "B05.5", "ICD10CM", "Condition", "", ""],   # unmapped -> recoverable
                [3, "B05.5", "ICD10", "Condition", "", ""],
                [4, "11111004", "SNOMED", "Condition", "S", ""]]
    rels = [[1, 4, "Maps to", ""], [2, 4, "Maps to", ""]]

    source = tmp_path / "fb.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], published)
    release = tmp_path / "fb_rel"
    build_vocabulary(source, None, release, _athena(tmp_path / "ath_fb", concepts, rels),
                     recover_unmapped=True)
    assert ("ICD10CM", "B055", "GI_001") in _map(release), "fixture stopped exercising recovery"
    assert _snomed_rows(release) == 0, "a recovered row reached the SNOMED bridge"

    # Positive control: publish B05.5 under ICD10CM directly -- the state feedback
    # would have produced -- and the concept IS retained. Without this the assertion
    # above would pass on a fixture that could never have failed.
    source2 = tmp_path / "fb2.csv"
    write_csv(source2, ["phecode", "ICD", "vocabulary_id"], published + [["GI_001", "B05.5", "ICD10CM"]])
    fed = tmp_path / "fb_fed"
    build_vocabulary(source2, None, fed, _athena(tmp_path / "ath_fb2", concepts, rels))
    assert _snomed_rows(fed) > 0, "the fixture cannot show feedback, so the test above is vacuous"


def test_recovery_never_adds_a_phecode_to_an_already_published_code(tmp_path: Path, fixture) -> None:
    """"Purely additive" means new codes only -- not new phecodes on existing ones.

    Asserting the published map is a subset of the recovered one allows a published
    code to silently acquire an extra phecode from another vocabulary, which would
    rewrite a curated assignment rather than fill a gap. The phecode set of every
    published (vocabulary, code) must come out untouched.
    """
    source, athena = fixture
    plain, rec = tmp_path / "pa", tmp_path / "pb"
    build_vocabulary(source, None, plain, athena)
    build_vocabulary(source, None, rec, athena, recover_unmapped=True)

    def by_code(release: Path) -> dict:
        out: dict = {}
        for vocabulary, code, phecode in _map(release):
            out.setdefault((vocabulary, code), set()).add(phecode)
        return out

    before, after = by_code(plain), by_code(rec)
    assert set(before) <= set(after)
    for key, phecodes in before.items():
        assert after[key] == phecodes, f"{key} gained {after[key] - phecodes} from recovery"


def test_both_routes_agreeing_is_reported_as_such(tmp_path: Path) -> None:
    """The both_routes_agree branch carries 863 of the real release's codes.

    No fixture reached it, so turning that branch into a skip passed the suite.
    """
    source = tmp_path / "agree.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "A01.1", "ICD10CM"]])
    athena = _athena(tmp_path / "ath_ag", concepts=[
        [1, "A01.1", "ICD10CM", "Condition", "", ""],
        [2, "A01.1", "ICD10", "Condition", "", ""],
        [5, "77386006", "SNOMED", "Condition", "S", ""],
    ], relationships=[[1, 5, "Maps to", ""], [2, 5, "Maps to", ""]])
    release = tmp_path / "ag"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)
    routes = {r[0] for r in duckdb.sql(
        f"SELECT route FROM read_csv_auto('{release / 'recovered_codes.csv'}') "
        "WHERE normalized_code = 'A011' AND vocabulary = 'ICD10'").fetchall()}
    assert routes == {"both_routes_agree"}, f"expected both routes to corroborate, got {routes}"
    assert json.loads((release / "manifest.json").read_text())["recovery"][
        "codes_added_by_route"].get("both_routes_agree") == 1


def test_the_snomed_route_is_labelled_in_the_audit_trail(tmp_path: Path, fixture) -> None:
    """recovered_codes.csv claims which evidence justified each row; only the
    cross-vocabulary label was ever asserted, so the SNOMED one could say anything."""
    source, athena = fixture
    release = tmp_path / "route_rel"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)
    route = duckdb.sql(f"SELECT route FROM read_csv_auto('{release / 'recovered_codes.csv'}') "
                       "WHERE normalized_code = 'B021'").fetchone()[0]
    assert route == "snomed_bridge"


def test_the_cli_flag_actually_switches_recovery_on(tmp_path: Path, fixture, monkeypatch) -> None:
    """The wiring was untested: making --recover-unmapped a no-op passed the suite."""
    from phecodex_mapper import cli
    source, athena = fixture
    release = tmp_path / "cli_rel"
    monkeypatch.setattr("sys.argv", [
        "phecodex-map", "build-vocabulary", "--phecodex-map", str(source),
        "--athena-dir", str(athena), "--recover-unmapped", "--output", str(release)])
    cli.main()
    assert ("ICD10", "A011", "CV_003") in _map(release)
    assert json.loads((release / "manifest.json").read_text())["recovery"]["rows_added"] > 0


def test_a_partial_overlap_between_the_routes_is_a_disagreement(tmp_path: Path) -> None:
    """Codes carrying several phecodes are the norm, and no fixture had one.

    The routes are compared as whole sorted lists, so `{GI_001, GI_002}` against
    `{GI_001}` must count as a disagreement. Tested only on singletons, a comparison
    that took the first element, or any overlap, would look correct -- and would
    quietly assert the narrower of two contradicting answers.
    """
    source = tmp_path / "multi.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["GI_001", "D01.1", "ICD10CM"], ["GI_002", "D01.1", "ICD10CM"],  # two phecodes
        ["GI_001", "E01.1", "ICD10CM"],                                   # bridged: GI_001 only
    ])
    athena = _athena(tmp_path / "ath_multi", concepts=[
        [1, "D01.1", "ICD10CM", "Condition", "", ""],
        [2, "D01.1", "ICD10", "Condition", "", ""],
        [3, "E01.1", "ICD10CM", "Condition", "", ""],
        [4, "11111004", "SNOMED", "Condition", "S", ""],
    ], relationships=[[3, 4, "Maps to", ""], [2, 4, "Maps to", ""]])
    release = tmp_path / "multi_rel"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)

    assert not {p for v, c, p in _map(release) if v == "ICD10" and c == "D011"}, \
        "a partial overlap was treated as agreement and recovered anyway"
    summary = json.loads((release / "manifest.json").read_text())["recovery"]
    assert summary["codes_skipped_unresolved_disagreement"] == 1


def test_a_verdict_does_not_resolve_a_different_code(tmp_path: Path, conflict) -> None:
    """The join key is the code. A verdict for Z99.9 must leave D01.1 unresolved."""
    source, concepts, rels = conflict
    verdicts = tmp_path / "other.csv"
    write_csv(verdicts, ["icd_code", "vocabulary", "adjudication_A_or_B"], [["Z99.9", "ICD10", "A"]])
    release = tmp_path / "other_rel"
    build_vocabulary(source, None, release, _athena(tmp_path / "ath_o", concepts, rels),
                     recover_unmapped=True, recovery_adjudication=verdicts)
    assert not {p for v, c, p in _map(release) if v == "ICD10" and c == "D011"}, \
        "an unrelated verdict resolved this code's conflict"
    assert json.loads((release / "manifest.json").read_text())["recovery"][
        "codes_skipped_unresolved_disagreement"] == 1


def test_one_code_can_take_different_routes_in_different_vocabularies(tmp_path: Path) -> None:
    """The routes are paired per (code, vocabulary), not per code.

    B02.1 is unmapped under both ICD10CM and ICD9CM. Under ICD10CM it has same-era
    cross evidence; under ICD9CM it has only the SNOMED bridge, the era guard having
    correctly refused the ICD-10 row. Pairing the two routes on the code alone would
    fuse these into one row and lose the ICD9CM recovery entirely -- and this is also
    the only coverage of recovery into ICD9CM, which the real release does twice.
    """
    source = tmp_path / "routes.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["GI_001", "B02.1", "ICD10"]])
    athena = _athena(tmp_path / "ath_rt", concepts=[
        [1, "B02.1", "ICD10", "Condition", "", ""],       # published
        [6, "B02.1", "ICD10CM", "Condition", "", ""],     # candidate: cross evidence only
        [9, "B02.1", "ICD9CM", "Condition", "", ""],      # candidate: SNOMED evidence only
        [4, "11111004", "SNOMED", "Condition", "S", ""],
    ], relationships=[[1, 4, "Maps to", ""], [9, 4, "Maps to", ""]])
    release = tmp_path / "routes_rel"
    build_vocabulary(source, None, release, athena, recover_unmapped=True)

    routes = dict(duckdb.sql(
        f"SELECT vocabulary, route FROM read_csv_auto('{release / 'recovered_codes.csv'}') "
        "WHERE normalized_code = 'B021'").fetchall())
    assert routes == {"ICD10CM": "cross_vocabulary", "ICD9CM": "snomed_bridge"}, routes
