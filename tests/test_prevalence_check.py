"""check_prevalence.py, run end to end -- including on a Parquet cohort.

The script's own comment records what went wrong: a hardcoded read_csv_auto on the
cohort killed it on a Parquet cohort the mapper itself accepts, AFTER printing the
prevalence bands and BEFORE the sex-restriction block, which is the sharpest check it
has. The fix moved it onto io.relation_for -- and nothing executed the script, so the
fix had no coverage at all. test_distribution only imports it for its constants and
runs --help.

The named phecodes here (GU_608 Male-only, PP_901 Female-only, CV_401 hypertension)
are the script's own SEX_CHECKS and EXPECTED identifiers, so the blocks under test
actually run rather than printing "not retained in this run".
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_with_cohort(tmp_path: Path):
    """A run whose matrix retains one Male-only, one Female-only and one unrestricted
    phecode, with the cohort written as both CSV and Parquet from the same rows."""
    from conftest import write_csv
    from phecodex_mapper.io import connect, quote
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary

    write_csv(tmp_path / "m.csv", ["phecode", "ICD", "vocabulary_id"],
              [["GU_608", "A01.1", "ICD10CM"], ["PP_901", "A02.1", "ICD10CM"],
               ["CV_401", "A03.1", "ICD10CM"]])
    write_csv(tmp_path / "i.csv", ["phecode", "sex", "phecode_string", "category"],
              [["GU_608", "Male", "Male-only trait", "GU"],
               ["PP_901", "Female", "Female-only trait", "PP"],
               ["CV_401", "Both", "Hypertension", "CV"]])
    release = tmp_path / "rel"
    build_vocabulary(tmp_path / "m.csv", tmp_path / "i.csv", release, None)

    # Integer person_id, which is what a Parquet cohort extracted from a biobank
    # actually carries, and what the matrix stores as VARCHAR.
    people = ([[i, "Male"] for i in range(1, 101)] + [[i, "Female"] for i in range(101, 201)])
    cohort_csv = tmp_path / "c.csv"
    write_csv(cohort_csv, ["person_id", "sex"], people)
    events = [[i, "A01.1", "ICD10CM"] for i in range(1, 31)]          # 30/100 males
    events += [[i, "A02.1", "ICD10CM"] for i in range(101, 126)]      # 25/100 females
    events += [[i, "A03.1", "ICD10CM"] for i in range(1, 41)]         # 40/200, in band
    write_csv(tmp_path / "e.csv", ["person_id", "code", "vocabulary"], events)

    run = tmp_path / "run"
    map_phecodes(release, cohort_csv, tmp_path / "e.csv", run, min_cases=5, min_controls=5)

    cohort_parquet = tmp_path / "c.parquet"
    connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{quote(cohort_csv)}') ORDER BY person_id) "
        f"TO '{quote(cohort_parquet)}' (FORMAT PARQUET)")
    return release, run, cohort_csv, cohort_parquet


def _check(tmp_path: Path, release: Path, run: Path, cohort: Path, name: str):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_prevalence.py"),
         "--run", str(run), "--release", str(release), "--cohort", str(cohort),
         "--out", str(tmp_path / f"{name}.csv")], capture_output=True, text=True)
    return result, tmp_path / f"{name}.csv"


def test_the_prevalence_check_completes_on_a_parquet_cohort(tmp_path: Path) -> None:
    """The regression itself: it must reach the sex block and write its CSV."""
    release, run, _, parquet = _run_with_cohort(tmp_path)
    result, out = _check(tmp_path, release, run, parquet, "parquet")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== Sex restriction on real data" in result.stdout
    assert "GU_608     Male  -only: 100 scored, 0 of the other sex   OK" in result.stdout
    assert "PP_901     Female-only: 100 scored, 0 of the other sex   OK" in result.stdout
    assert out.is_file() and "CV_401" in out.read_text()
    # The in-band phecode must not be flagged, or the check cries wolf on a correct run.
    assert "Hypertension" in result.stdout
    assert "CV_401 at" not in result.stdout


def test_a_parquet_cohort_gives_the_same_report_as_the_csv_it_was_written_from(
        tmp_path: Path) -> None:
    """Computable two ways, so it must reconcile. A Parquet path that ran but joined
    nothing would pass the test above -- `scored` comes from the matrix alone -- and
    only differ from the CSV path here."""
    release, run, csv_cohort, parquet = _run_with_cohort(tmp_path)
    from_csv, _ = _check(tmp_path, release, run, csv_cohort, "from_csv")
    from_parquet, _ = _check(tmp_path, release, run, parquet, "from_parquet")
    assert from_csv.returncode == 0, from_csv.stdout + from_csv.stderr

    def body(text: str) -> str:
        return "\n".join(l for l in text.splitlines() if "wrote aggregate prevalences" not in l)

    assert body(from_parquet.stdout) == body(from_csv.stdout)


def test_the_sex_check_catches_wrong_sex_scoring_on_a_parquet_cohort(tmp_path: Path) -> None:
    """Negative control for the join, not for the mapper.

    The run above is correct, so `0 of the other sex` is the only answer available and a
    join matching nothing would report it too. Feeding the same matrix a cohort whose
    sexes are inverted is the only way to make the wrong-sex count non-zero: if the
    Parquet join is really joining -- VARCHAR matrix person_id against integer cohort
    person_id -- all 100 people scored for GU_608 now read as female.
    """
    from conftest import write_csv
    from phecodex_mapper.io import connect, quote

    release, run, _, _ = _run_with_cohort(tmp_path)
    flipped_csv = tmp_path / "flipped.csv"
    write_csv(flipped_csv, ["person_id", "sex"],
              [[i, "Female"] for i in range(1, 101)] + [[i, "Male"] for i in range(101, 201)])
    flipped = tmp_path / "flipped.parquet"
    connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{quote(flipped_csv)}') ORDER BY person_id) "
        f"TO '{quote(flipped)}' (FORMAT PARQUET)")

    result, _ = _check(tmp_path, release, run, flipped, "flipped")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "100 WRONG-SEX PEOPLE SCORED" in result.stdout
    assert "GU_608 scored 100 people of the wrong sex" in result.stdout
