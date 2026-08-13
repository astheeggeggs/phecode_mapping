from __future__ import annotations

import json
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
    assert (release / "phetk_custom_map.csv").exists()
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["counts"]["icd_map_rows"] == 4
    assert duckdb.sql(f"SELECT count(*) FROM read_parquet('{release / 'icd_map.parquet'}')").fetchone()[0] == 4


def test_build_vocabulary_and_map_phecodes_refuse_existing_output(tmp_path: Path, release: Path) -> None:
    with pytest.raises(FileExistsError, match="already exists"):
        build_vocabulary(tmp_path / "official.csv", None, release)  # release dir already exists
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id"], [["p1"]])
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
    write_csv(cohort, ["person_id"], [["p1"], ["p2"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", " 44054006 ", "SNOMED"], ["p2", "99999999", "SNOMED"]])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, min_cases=1, min_controls=1)
    cases = duckdb.sql(f"SELECT person_id, phecode FROM read_parquet('{output / 'person_phecodes.parquet'}')").fetchall()
    assert cases == [("p1", "EM_202")]  # p2's unmapped SNOMED code produces no case
    unmapped = duckdb.sql(f"SELECT person_id FROM read_csv_auto('{output / 'unmapped_events.csv'}')").fetchall()
    assert unmapped == [("p2",)]


def test_snomed_bridges_through_who_icd10_alias(tmp_path: Path) -> None:
    # Athena tags WHO ICD-10 concepts vocabulary_id='ICD10' (distinct from the US
    # clinical-modification 'ICD10CM'); PhecodeX map rows built from the WHO unrolled file
    # are relabeled 'ICD10CM'. The SNOMED bridge must treat Athena's 'ICD10' as that same
    # alias, or SNOMED codes that only cross-map through WHO ICD-10 never bridge.
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["EM_202", "E11.1", "ICD10CM"]])
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
