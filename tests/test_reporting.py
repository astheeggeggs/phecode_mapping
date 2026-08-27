"""Regression tests: the numbers an analyst reads must describe what was delivered.

This file previously covered three reporting defects caused by the mapper writing
two mapping variants and reporting the wrong one. Hierarchy fallback has since been
removed -- mapping is exact against the published map -- so those defects are gone
by construction. What remains worth pinning is that the audit's headline figures and
the CLI's summary line describe the run that actually happened, and that
--max-unmapped-rate gates on that same number.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes


def _run(tmp_path: Path, release: Path, events_rows: list[list[str]], **kwargs) -> Path:
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [[f"p{i}", "Female"] for i in range(1, 7)])
    write_csv(events, ["person_id", "code", "vocabulary"], events_rows)
    map_phecodes(release, cohort, events, output, min_cases=1, min_controls=0, **kwargs)
    return output


def test_audit_unmapped_figures_describe_the_run(tmp_path: Path, full_release: Path) -> None:
    events = ([[f"p{i}", "A01.1", "ICD10CM"] for i in range(1, 4)]   # CV_003, mapped
              + [[f"p{i}", "ZZ9", "ICD10CM"] for i in range(4, 6)])  # unmapped
    audit = json.loads((_run(tmp_path, full_release, events) / "audit.json").read_text())

    assert audit["events"] == 5
    assert audit["unmapped_events"] == 2
    assert audit["unmapped_rate"] == pytest.approx(2 / 5)
    assert audit["mapping_policy"] == "exact-match-against-published-map"
    # The two-variant fields are gone; nothing should reintroduce them silently.
    assert "hierarchy_aware" not in audit
    assert "phenotype_matrix_exact" not in audit


def test_max_unmapped_rate_gates_on_the_reported_rate(tmp_path: Path, full_release: Path) -> None:
    events = ([[f"p{i}", "A01.1", "ICD10CM"] for i in range(1, 4)]
              + [[f"p{i}", "ZZ9", "ICD10CM"] for i in range(4, 6)])
    output = _run(tmp_path, full_release, events, max_unmapped_rate=0.5)
    assert json.loads((output / "audit.json").read_text())["unmapped_rate"] <= 0.5

    with pytest.raises(RuntimeError, match="Unmapped rate"):
        map_phecodes(full_release, tmp_path / "cohort.csv", tmp_path / "events.csv",
                     tmp_path / "run_strict", min_cases=1, min_controls=0, max_unmapped_rate=0.1)


def test_audit_phenotype_matrix_describes_the_delivered_matrix(tmp_path: Path, full_release: Path) -> None:
    """audit['phenotype_matrix'] is what the CLI prints as matrix_columns."""
    output = _run(tmp_path, full_release, [[f"p{i}", "A01.1", "ICD10CM"] for i in range(1, 4)])
    audit = json.loads((output / "audit.json").read_text())
    columns = [r[0] for r in duckdb.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{output / 'phenotype_matrix.parquet'}')").fetchall()]
    assert audit["phenotype_matrix"]["n_columns"] == len(columns) - 1 == 1


def test_no_hierarchy_outputs_are_written(tmp_path: Path, full_release: Path) -> None:
    """Deleting a feature should delete its outputs, not leave stale siblings."""
    output = _run(tmp_path, full_release, [[f"p{i}", "A01.1", "ICD10CM"] for i in range(1, 4)])
    stale = sorted(p.name for p in output.iterdir() if "hierarchy" in p.name)
    assert stale == []
