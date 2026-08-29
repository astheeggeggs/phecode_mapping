from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary


def test_release_artifacts_and_manifest(release) -> None:
    assert (release / "icd_map.csv").exists()
    assert (release / "icd_map.parquet").exists()
    assert (release / "phecodex_reference_maps.xlsx").exists()
    # One adapter per ICD-10 flavour: PheTK's custom format has only flag 9/10, so a
    # combined file made a WHO event match ICD-10-CM-only rows.
    assert (release / "phetk_custom_map_icd10.csv").exists()
    assert (release / "phetk_custom_map_icd10cm.csv").exists()
    assert not (release / "phetk_custom_map.csv").exists(), \
        "the ambiguous combined adapter should no longer be written"
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["counts"]["icd_map_rows"] == 4
    assert duckdb.sql(f"SELECT count(*) FROM read_parquet('{release / 'icd_map.parquet'}')").fetchone()[0] == 4


def test_build_vocabulary_and_map_phecodes_refuse_existing_output(tmp_path: Path, release: Path) -> None:
    with pytest.raises(FileExistsError, match="already exists"):
        build_vocabulary(tmp_path / "official.csv", None, release)  # release dir already exists
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "I10", "ICD10CM"]])
    existing = tmp_path / "existing_run"; existing.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        map_phecodes(release, cohort, events, existing)


def test_snomed_bridges_through_athena_extract(tmp_path: Path) -> None:
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["EM_202", "E11.9", "ICD10CM"]])
    athena = tmp_path / "athena"; athena.mkdir()
    write_csv(athena / "CONCEPT.csv", ["concept_id", "concept_code", "vocabulary_id", "domain_id", "standard_concept", "invalid_reason"], [
        [1, "44054006", "SNOMED", "Condition", "S", ""],      # standard SNOMED concept (diabetes)
        [2, "99999999", "SNOMED", "Condition", "S", "D"],     # invalid concept: must be excluded
        [3, "E11.9", "ICD10CM", "Condition", "", ""],         # ICD10CM source concept
    ])
    write_csv(athena / "CONCEPT_RELATIONSHIP.csv", ["concept_id_1", "concept_id_2", "relationship_id", "invalid_reason"], [
        [3, 1, "Maps to", ""],
    ])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    assert (release / "snomed_map.parquet").exists()
    rows = duckdb.sql(f"SELECT source_code, phecode FROM read_parquet('{release / 'snomed_map.parquet'}')").fetchall()
    assert rows == [("44054006", "EM_202")]

    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", " 44054006 ", "SNOMED"], ["p2", "99999999", "SNOMED"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, min_cases=1, min_controls=1)
    cases = duckdb.sql(f"SELECT person_id, phecode FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchall()
    assert cases == [("p1", "EM_202")]  # p2's unmapped SNOMED code produces no case
    unmapped = duckdb.sql(f"SELECT person_id FROM read_csv_auto('{output / 'unmapped_events.csv'}')").fetchall()
    assert unmapped == [("p2",)]


def test_snomed_bridges_through_who_icd10_alias(tmp_path: Path) -> None:
    # Athena tags WHO ICD-10 concepts vocabulary_id='ICD10', distinct from US ICD10CM.
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["EM_202", "E11.1", "ICD10"]])
    athena = tmp_path / "athena"; athena.mkdir()
    write_csv(athena / "CONCEPT.csv", ["concept_id", "concept_code", "vocabulary_id", "domain_id", "standard_concept", "invalid_reason"], [
        [1, "44054006", "SNOMED", "Condition", "S", ""],   # standard SNOMED concept
        [2, "E11.1", "ICD10", "Condition", "", ""],        # WHO ICD-10 source concept (not ICD10CM)
    ])
    write_csv(athena / "CONCEPT_RELATIONSHIP.csv", ["concept_id_1", "concept_id_2", "relationship_id", "invalid_reason"], [
        [2, 1, "Maps to", ""],
    ])
    release = tmp_path / "release"
    build_vocabulary(source, None, release, athena)
    rows = duckdb.sql(f"SELECT source_code, phecode FROM read_parquet('{release / 'snomed_map.parquet'}')").fetchall()
    assert rows == [("44054006", "EM_202")]


def test_each_phetk_adapter_carries_exactly_one_icd10_flavour(tmp_path: Path) -> None:
    """PheTK matches on `flag`, which is 9 or 10 and cannot distinguish WHO from CM.

    A combined adapter therefore let PheTK resolve a WHO ICD-10 event against
    ICD-10-CM-only map rows. Measured on a 2.6M-event extract, PheTK's output became a
    strict superset of ours -- 288,724 extra (person, phecode) pairs, 91.9% agreement --
    from the 105,511 (code, phecode) pairs that exist only under ICD10CM. Splitting the
    adapter is what makes a PheTK run reproduce this tool's phenotypes.

    ICD9CM rows belong in both files: flag=9 is unambiguous.
    """
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary

    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["CV_003", "A01.1", "ICD10CM"],   # CM only
        ["GI_001", "B02.2", "ICD10"],     # WHO only
        ["ID_052", "123.4", "ICD9CM"],    # shared
        ["CV_003", "C03.3", "ICD10CM"], ["GI_001", "C03.3", "ICD10"],   # same code, both
    ])
    release = tmp_path / "rel"
    build_vocabulary(source, None, release, None)

    who = {(r[0], r[1]) for r in duckdb.sql(
        f"SELECT phecode, ICD FROM read_csv_auto('{release / 'phetk_custom_map_icd10.csv'}')").fetchall()}
    cm = {(r[0], r[1]) for r in duckdb.sql(
        f"SELECT phecode, ICD FROM read_csv_auto('{release / 'phetk_custom_map_icd10cm.csv'}')").fetchall()}

    assert ("GI_001", "B02.2") in who and ("GI_001", "B02.2") not in cm
    assert ("CV_003", "A01.1") in cm and ("CV_003", "A01.1") not in who
    assert ("ID_052", "123.4") in who and ("ID_052", "123.4") in cm, "ICD-9 belongs in both"
    # The same code carrying different phecodes under each flavour is the whole point.
    assert ("GI_001", "C03.3") in who and ("GI_001", "C03.3") not in cm
    assert ("CV_003", "C03.3") in cm and ("CV_003", "C03.3") not in who


@pytest.mark.skipif(shutil.which("phetk") is None and
                    not (Path(sys.executable).parent / "phetk").exists(),
                    reason="phetk not installed (optional extra)")
def test_phetk_reproduces_our_phenotypes_with_the_matching_adapter(tmp_path: Path) -> None:
    """End-to-end parity, not just file shape.

    p1 is a WHO ICD-10 cohort holding A01.1 (mapped only under ICD-10-CM) and B02.2
    (mapped only under WHO). The correct answer for a WHO cohort is GI_001 alone --
    which is what our exact (code, vocabulary) join gives. The combined adapter used to
    hand PheTK both, because its `flag` column cannot tell WHO from CM.
    """
    import subprocess
    from conftest import write_csv
    from phecodex_mapper.vocabulary import build_vocabulary

    write_csv(tmp_path / "m.csv", ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "A01.1", "ICD10CM"], ["GI_001", "B02.2", "ICD10"],
               ["ID_052", "123.4", "ICD9CM"]])
    write_csv(tmp_path / "i.csv", ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "CM only", "X"], ["GI_001", "Both", "WHO only", "Y"],
               ["ID_052", "Both", "Nine", "Z"]])
    release = tmp_path / "rel"
    build_vocabulary(tmp_path / "m.csv", tmp_path / "i.csv", release, None)
    write_csv(tmp_path / "fixture.csv", ["person_id", "date", "vocabulary_id", "ICD"],
              [["p1", "2015-01-01", "ICD10", "A01.1"], ["p1", "2016-01-01", "ICD10", "B02.2"]])

    phetk = shutil.which("phetk") or str(Path(sys.executable).parent / "phetk")
    got = {}
    for flavour in ("icd10", "icd10cm"):
        out = tmp_path / f"out_{flavour}.tsv"
        subprocess.run([phetk, "phecode", "count-phecode", "--platform", "custom",
                        "--icd_file_path", str(tmp_path / "fixture.csv"), "--icd_version", "custom",
                        "--phecode_map_file_path", str(release / f"phetk_custom_map_{flavour}.csv"),
                        "--output_file_path", str(out)], check=True, capture_output=True)
        got[flavour] = {line.split("\t")[1] for line in
                        out.read_text().strip().splitlines()[1:]}

    assert got["icd10"] == {"GI_001"}, f"WHO adapter over-matched: {got['icd10']}"
    assert got["icd10cm"] == {"CV_003"}, f"CM adapter wrong: {got['icd10cm']}"
