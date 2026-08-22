from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary


def test_hierarchy_fallback_is_vocabulary_specific_and_exact_wins(tmp_path: Path) -> None:
    source = tmp_path / "map.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["ICD9_PARENT", "401.9", "ICD9CM"],
        ["ICD10_PARENT", "K58", "ICD10"],
        ["CM_PARENT", "K58", "ICD10CM"],
        ["CM_EXACT", "K58.9", "ICD10CM"],
    ])
    hierarchy = tmp_path / "hierarchy.csv"
    rows = []
    for vocab, parent, child in [("ICD9CM", "401.9", "401.90"), ("ICD10", "K58", "K58.9"), ("ICD10CM", "K58", "K58.99")]:
        rows.append([vocab, parent, child, "test-1"])
    write_csv(hierarchy, ["vocabulary", "parent_code", "child_code", "source_version"], rows)
    hierarchy_files = []
    for vocab in ("ICD9CM", "ICD10", "ICD10CM"):
        path = tmp_path / f"{vocab}.csv"
        write_csv(path, ["vocabulary", "parent_code", "child_code", "source_version"], [r for r in rows if r[0] == vocab])
        hierarchy_files.append((vocab, path))
    release = tmp_path / "release"
    build_vocabulary(source, None, release, icd_hierarchy=hierarchy_files)
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"], ["p3", "Female"], ["p4", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [
        ["p1", "401.90", "ICD9CM"], ["p2", "K58.9", "ICD10"],
        ["p3", "K58.9", "ICD10CM"], ["p4", "K58.99", "ICD10CM"],
    ])
    output = tmp_path / "run"
    map_phecodes(release, cohort, events, output, min_cases=0, min_controls=0, hierarchy_aware=True)
    rows = duckdb.sql(f"SELECT person_id,phecode FROM read_parquet('{output / 'person_phecodes_hierarchy.parquet'}') ORDER BY person_id").fetchall()
    assert rows == [("p1", "ICD9_PARENT"), ("p2", "ICD10_PARENT"), ("p3", "CM_EXACT"), ("p4", "CM_PARENT")]
    fallback = duckdb.sql(f"SELECT vocabulary, parent_code, phecode FROM read_csv_auto('{output / 'hierarchy_fallbacks.csv'}') ORDER BY vocabulary, parent_code").fetchall()
    assert fallback == [("ICD10", "K58", "ICD10_PARENT"), ("ICD10CM", "K58", "CM_PARENT"), ("ICD9CM", "4019", "ICD9_PARENT")]
    audit = json.loads((output / "audit.json").read_text())
    assert audit["hierarchy_aware"]["fallback_events"] == 3


def test_hierarchy_rejects_conflicting_parent(tmp_path: Path) -> None:
    source = tmp_path / "map.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["AA_1", "401", "ICD9CM"]])
    hierarchy = tmp_path / "hierarchy.csv"
    write_csv(hierarchy, ["vocabulary", "parent_code", "child_code", "source_version"], [
        ["ICD9CM", "401", "4011", "v1"], ["ICD9CM", "402", "4011", "v1"]])
    with pytest.raises(ValueError, match="conflicting"):
        build_vocabulary(source, None, tmp_path / "release", icd_hierarchy=[("ICD9CM", hierarchy)])
