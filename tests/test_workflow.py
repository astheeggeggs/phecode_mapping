from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.workflow import preflight, run_workflow


def test_preflight_reports_schema_and_vocabularies(tmp_path: Path, release: Path) -> None:
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES ('AA_1','Both','Test'),('AA_1.1','Both','Test'),('BB_2','Both','Test')) AS t(phecode,sex,phecode_string)) TO '{release / 'phecode_info.parquet'}' (FORMAT PARQUET)")
    cohort = tmp_path / "cohort.csv"
    events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01.1", "ICD10CM"], ["p2", "123.4", "ICD9CM"]])
    report = preflight(release, cohort, events)
    assert report["cohort_rows"] == 2
    assert report["event_rows"] == 2
    assert report["vocabulary_counts"] == {"ICD10CM": 1, "ICD9CM": 1}


def test_run_workflow_writes_an_audited_matrix(tmp_path: Path, release: Path) -> None:
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES ('AA_1','Both','Test'),('AA_1.1','Both','Test'),('BB_2','Both','Test')) AS t(phecode,sex,phecode_string)) TO '{release / 'phecode_info.parquet'}' (FORMAT PARQUET)")
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    output = tmp_path / "run"
    audit = run_workflow(release=release, cohort=cohort, events=events, output=output, min_cases=1, min_controls=1)
    assert audit["mapping_policy"] == "exact-match-against-published-map"
    assert (output / "phenotype_matrix.csv.gz").exists()
    stored = json.loads((output / "audit.json").read_text())
    assert stored["workflow"] == "analyst-run"
    assert stored["preflight"]["vocabulary_counts"] == {"ICD9CM": 1}


def test_preflight_rejects_bad_sex_and_vocab(tmp_path: Path, release: Path) -> None:
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES ('AA_1','Both','Test')) AS t(phecode,sex,phecode_string)) TO '{release / 'phecode_info.parquet'}' (FORMAT PARQUET)")
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "unknown"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "A01", "BAD"]])
    with pytest.raises(ValueError, match="sex"):
        preflight(release, cohort, events)


def test_run_workflow_requires_a_complete_release(tmp_path: Path, release: Path) -> None:
    """run must refuse a release that is genuinely missing a required artefact.

    This used to lean on the `release` fixture being built without --phecodex-info, but
    such a release is now complete by construction, so the premise had quietly become
    untrue. Delete a required file instead: that is the condition actually being guarded.
    """
    (release / "phecode_info.parquet").unlink()
    cohort = tmp_path / "cohort.csv"; events = tmp_path / "events.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [["p1", "123.4", "ICD9CM"]])
    with pytest.raises(ValueError, match="phecode_info.parquet"):
        run_workflow(release=release, cohort=cohort, events=events, output=tmp_path / "run", min_cases=1, min_controls=1)
