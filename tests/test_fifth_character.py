"""The optional WHO ICD-10 fifth character: a 5-character ICD10 code the map lacks matches as its
4-character parent when the map has that parent (default); nothing else changes.

Pinned because matching it exactly turned about half the carriers of a common musculoskeletal
code into controls on a hospital-coded cohort, silently, with the unmapped rate looking normal.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary
from conftest import write_csv


def _release(tmp_path: Path) -> Path:
    source = tmp_path / "who.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["MS_703.1", "M10.9", "ICD10"],      # 4-char parent in the map
        ["RE_479.31", "J96.00", "ICD10"],    # 5-char code the map HAS, finer than its parent
        ["RE_479.3", "J96.0", "ICD10"],
        ["RE_479.3", "J96.00", "ICD10"],     # unrolled maps list the parent phecode on the child code too
        ["BB_2", "A01.1", "ICD10CM"],        # CM: never touched
    ])
    out = tmp_path / "release"; build_vocabulary(source, None, out); return out


def _inputs(tmp_path: Path):
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [[str(i), "Female"] for i in range(1, 1001)])
    rows = [[str(i), "M10.9", "ICD10"] for i in range(1, 301)]            # spelled as in the map
    rows += [[str(i), "M10.99", "ICD10"] for i in range(301, 601)]        # fifth character, map lacks it
    rows += [[str(i), "M10.97", "ICD10"] for i in range(601, 701)]
    rows += [[str(i), "J96.00", "ICD10"] for i in range(701, 951)]        # fifth character the map HAS
    rows += [[str(i), "M10.9", "ICD10CM"] for i in range(951, 971)]       # CM label: no WHO fallback
    rows += [[str(i), "Q99.9", "ICD10"] for i in range(971, 981)]         # 4-char code the map lacks: stays unmapped
    rows += [[str(i), "Q99.91", "ICD10"] for i in range(981, 991)]        # 5-char whose parent is also absent: stays unmapped
    write_csv(events, ["person_id", "code", "vocabulary"], rows)
    return cohort, events


def _cases(output: Path, phecode: str) -> int:
    return duckdb.sql(f"SELECT case_count FROM read_parquet('{output / 'phecode_counts.parquet'}') WHERE phecode='{phecode}'").fetchone()[0]


def test_parent_fallback_is_default_and_counts_every_carrier(tmp_path: Path) -> None:
    release = _release(tmp_path); cohort, events = _inputs(tmp_path); out = tmp_path / "run"
    map_phecodes(release, cohort, events, out, min_cases=1, min_controls=1)
    assert _cases(out, "MS_703.1") == 700                      # 300 exact + 300 M10.99 + 100 M10.97
    assert _cases(out, "RE_479.31") == 250                     # the 5-char code the map has is matched as written
    assert _cases(out, "RE_479.3") == 250
    audit = json.loads((out / "audit.json").read_text())
    fc = audit["icd10_fifth_character"]
    assert fc == {"policy": "parent", "events_matched_via_parent": 400, "distinct_codes_matched_via_parent": 2,
                  "events_matched_via_parent_by_chapter": {"M": 400}}
    assert audit["mapping_policy"] == "exact-match-against-published-map; icd10-fifth-character-parent-fallback"
    unmapped = (out / "unmapped_events.csv").read_text()
    assert unmapped.count("\n") - 1 == 20 + 10 + 10            # CM-labelled, Q99.9, Q99.91
    assert "M1099" not in unmapped


def test_exact_reproduces_previous_behaviour(tmp_path: Path) -> None:
    release = _release(tmp_path); cohort, events = _inputs(tmp_path); out = tmp_path / "run_exact"
    map_phecodes(release, cohort, events, out, min_cases=1, min_controls=1, fifth_character="exact")
    assert _cases(out, "MS_703.1") == 300
    audit = json.loads((out / "audit.json").read_text())
    assert audit["icd10_fifth_character"]["policy"] == "exact" and audit["icd10_fifth_character"]["events_matched_via_parent"] == 0
    assert audit["mapping_policy"] == "exact-match-against-published-map"
