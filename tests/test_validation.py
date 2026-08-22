from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from phecodex_mapper.io import checksum
from phecodex_mapper.validation import validate_phecodex_counts


def _parquet(path: Path, query: str) -> None:
    duckdb.connect().execute(f"COPY ({query}) TO '{path}' (FORMAT PARQUET)")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    run, release = tmp_path / "run", tmp_path / "release"
    run.mkdir(); release.mkdir()
    _parquet(run / "phecode_counts.parquet", "SELECT * FROM (VALUES ('EM_202', 2, 3, 0, 3, true), ('CV_401.1', 1, 4, 0, 4, false)) AS t(phecode, case_count, control_count_before_exclusions, excluded_control_count, control_count_after_exclusions, retained)")
    _parquet(run / "person_phecodes.parquet", "SELECT * FROM (VALUES ('p1', 'EM_202'), ('p2', 'EM_202'), ('p3', 'CV_401.1')) AS t(person_id, phecode)")
    _parquet(release / "phecode_info.parquet", "SELECT * FROM (VALUES ('EM_202', 'Both', 'Diabetes'), ('CV_401.1', 'Both', 'Hypertension')) AS t(phecode, sex, phecode_string)")
    (run / "unmapped_events.csv").write_text("person_id,code,vocabulary\np1,Z99,ICD10\np2,001,ICD9CM\n")
    (release / "manifest.json").write_text('{"version":"test"}\n')
    (run / "audit.json").write_text(json.dumps({"release_manifest_sha256": checksum(release / "manifest.json"), "events": 10, "unmapped_events": 2}) + "\n")
    external = tmp_path / "all_by_all.csv"
    external.write_text("phecode,description,sex,ancestry,case_count,control_count,sample_count,source,source_version\nEM_202,Diabetes,Both,META,4,6,10,All of Us All by All,v8\nCV_401.1,Hypertension,Both,META,2,8,10,All of Us All by All,v8\n")
    return run, release, external


def test_validation_reconstructs_cases_and_writes_aggregate_outputs(tmp_path: Path) -> None:
    run, release, external = _fixture(tmp_path)
    output = tmp_path / "validation"
    validate_phecodex_counts(run, release, external, output)
    report = json.loads((output / "validation.json").read_text())
    assert report["internal_case_reconstruction_errors"] == []
    assert report["matched_phecodes"] == 2
    assert report["unmapped_events_by_vocabulary"] == {"ICD10": 1, "ICD9CM": 1}
    assert (output / "phecodex_comparison.csv").exists()
    assert (output / "phecodex_review.csv").exists()
    assert (output / "prevalence_scatter.svg").exists()


def test_validation_rejects_conventional_phecodes(tmp_path: Path) -> None:
    run, release, external = _fixture(tmp_path)
    external.write_text("phecode,description,sex,ancestry,case_count,control_count,sample_count,source,source_version\n411,MI,Both,META,1,9,10,All by All,v8\n")
    with pytest.raises(ValueError, match="non-PhecodeX"):
        validate_phecodex_counts(run, release, external, tmp_path / "validation")


def test_validation_rejects_stale_manifest_checksum(tmp_path: Path) -> None:
    run, release, external = _fixture(tmp_path)
    (run / "audit.json").write_text(json.dumps({"release_manifest_sha256": "wrong"}) + "\n")
    with pytest.raises(ValueError, match="does not match"):
        validate_phecodex_counts(run, release, external, tmp_path / "validation")
