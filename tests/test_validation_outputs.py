"""Regression tests for validate-phecodex: it must run, and its output must mean something.

Three defects made the cross-biobank check unusable:

  B2  the identifier regex rejected 14 genuine PhecodeX 1.1 codes (all NB_N* and
      PP_P00*), so any real All by All export aborted the command outright;
  S15 `denominator_mismatch` was a review reason, and it is always true between two
      different biobanks, so the review file was a copy of the full comparison and
      the triage step triaged nothing;
  S16 the emitted local control count was the *before-exclusions* figure, so the
      denominators did not describe the run being validated.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.validation import PHECODEX_RE, validate_phecodex_counts

# Real PhecodeX 1.1 identifiers whose body starts with a letter.
LETTER_BODY_CODES = ["NB_N000", "NB_N000.1", "NB_N000.71", "NB_N010", "PP_P001", "PP_P003"]


@pytest.mark.parametrize("phecode", LETTER_BODY_CODES + ["ID_008", "CA_106.11", "GU_614.55"])
def test_regex_accepts_real_phecodex_identifiers(phecode: str) -> None:
    assert PHECODEX_RE.fullmatch(phecode), f"{phecode} is a genuine PhecodeX 1.1 code"


@pytest.mark.parametrize("phecode", ["250.2", "008", "E11.9", "phecode", "AAA_1", "A_1"])
def test_regex_still_rejects_non_phecodex_identifiers(phecode: str) -> None:
    """Loosening the body must not turn the check into a rubber stamp."""
    assert not PHECODEX_RE.fullmatch(phecode)


def test_regex_accepts_every_identifier_in_the_shipped_metadata() -> None:
    """The authoritative check: the release's own phecode list must all validate."""
    info = Path(__file__).resolve().parents[1] / "phecodeX_info_1.1_with_sex.csv"
    if not info.exists():
        pytest.skip("phecodeX_info_1.1_with_sex.csv is not present in this checkout")
    codes = [r["phecode"] for r in csv.DictReader(info.open(encoding="latin1"))]
    rejected = [c for c in codes if not PHECODEX_RE.fullmatch(c)]
    assert rejected == [], f"{len(rejected)} shipped phecodes fail the validator, e.g. {rejected[:5]}"


@pytest.fixture
def run_and_release(tmp_path: Path, full_release: Path) -> tuple[Path, Path]:
    """A completed run with a control exclusion, so before/after counts differ."""
    cohort, events, exclusions = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "cx.csv"
    write_csv(cohort, ["person_id", "sex"], [[f"p{i}", "Female"] for i in range(1, 11)])
    write_csv(events, ["person_id", "code", "vocabulary"],
              [["p1", "A01.1", "ICD10CM"], ["p2", "A01.1", "ICD10CM"], ["p3", "A02.0", "ICD10CM"]])
    write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
              [["CV_003", "code", "A02.0", "ICD10CM"]])
    output = tmp_path / "run"
    map_phecodes(full_release, cohort, events, output, exclusions=exclusions, min_cases=1, min_controls=0)
    return output, full_release


def _external(path: Path, rows: list[list[str]]) -> Path:
    write_csv(path, ["phecode", "description", "sex", "ancestry", "case_count",
                     "control_count", "sample_count", "source", "source_version"], rows)
    return path


def test_letter_bodied_identifiers_do_not_abort_the_comparison(
    tmp_path: Path, run_and_release: tuple[Path, Path]
) -> None:
    """An export containing NB_N000 / PP_P001 must be comparable, not rejected."""
    run, release = run_and_release
    external = _external(tmp_path / "external.csv", [
        ["CV_003", "Unrestricted trait", "Both", "ALL", "500", "9500", "10000", "AllByAll", "v1"],
        ["NB_N000", "Neonatal", "Both", "ALL", "100", "9900", "10000", "AllByAll", "v1"],
        ["PP_P001", "Supervision of pregnancy", "Female", "ALL", "50", "4950", "5000", "AllByAll", "v1"],
    ])
    out = tmp_path / "validation"
    validate_phecodex_counts(run, release, external, out)
    assert (out / "phecodex_comparison.csv").exists()
    qc = json.loads((out / "validation.json").read_text())
    assert qc["comparison_rows"] == 3
    assert qc["missing_local_phecodes"] == 2  # NB_N000 and PP_P001 are not in this tiny release


def test_review_file_is_a_subset_not_a_copy(tmp_path: Path, run_and_release: tuple[Path, Path]) -> None:
    """A differing denominator alone must not flag a row for manual review."""
    run, release = run_and_release
    # Local prevalences are CV_003 2/10 = 0.20 and SS_004 1/10 = 0.10. The external
    # rows match those proportions on a 10,000-person denominator, so the ONLY thing
    # separating them from the local run is the sample size -- which is the normal
    # cross-biobank case and previously made every row a review row.
    #
    # GU_001 is the positive control: absent locally, so it must still be flagged.
    # Without it this test could pass by the review filter being broken the other way.
    external = _external(tmp_path / "external.csv", [
        ["CV_003", "Unrestricted trait", "Both", "ALL", "2000", "8000", "10000", "AllByAll", "v1"],
        ["SS_004", "Non-specific symptom", "Both", "ALL", "1000", "9000", "10000", "AllByAll", "v1"],
        ["GU_001", "Female-only trait", "Female", "ALL", "300", "4700", "5000", "AllByAll", "v1"],
    ])
    out = tmp_path / "validation"
    validate_phecodex_counts(run, release, external, out)

    comparison = list(csv.DictReader((out / "phecodex_comparison.csv").open()))
    review = list(csv.DictReader((out / "phecodex_review.csv").open()))
    assert len(comparison) == 3
    assert [r["phecode"] for r in review] == ["GU_001"], \
        "review must contain the genuinely reviewable row and nothing else"

    # The denominator difference is still recorded -- as a note and as a column.
    both = next(r for r in comparison if r["phecode"] == "CV_003")
    assert "denominator_mismatch" in both["notes"]
    assert "denominator_mismatch" not in both["review_reason"]
    assert both["denominator_difference"] == str(10 - 10000)
    assert float(both["relative_proportion_difference"]) == pytest.approx(0.0)


def test_comparison_reports_control_counts_that_match_the_run(
    tmp_path: Path, run_and_release: tuple[Path, Path]
) -> None:
    """Before- and after-exclusion controls must both be present and correct."""
    run, release = run_and_release
    external = _external(tmp_path / "external.csv", [
        ["CV_003", "Unrestricted trait", "Both", "ALL", "500", "9500", "10000", "AllByAll", "v1"]])
    out = tmp_path / "validation"
    validate_phecodex_counts(run, release, external, out)

    local = duckdb.sql(
        f"SELECT case_count, control_count_before_exclusions, control_count_after_exclusions,"
        f" excluded_control_count FROM read_parquet('{run / 'phecode_counts.parquet'}')"
        f" WHERE phecode = 'CV_003'").fetchone()
    assert local[3] > 0, "fixture must actually exercise an exclusion"
    assert local[1] != local[2], "before/after must differ or this test proves nothing"

    row = next(r for r in csv.DictReader((out / "phecodex_comparison.csv").open())
               if r["phecode"] == "CV_003")
    assert int(row["local_case_count"]) == local[0]
    assert int(row["local_control_count_before_exclusions"]) == local[1]
    assert int(row["local_control_count_after_exclusions"]) == local[2]
    assert int(row["local_excluded_control_count"]) == local[3]
