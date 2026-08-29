"""The UK Biobank extraction script is shipped to analysts and had no functional test.

test_distribution.py only checks the file is present in the bundle. This runs it.

It also had no event_date handling at all, which made --case-rule two-dates
unreachable on the documented UKB path: map-phecodes refuses two-dates without the
column, so the option could not be used by anyone following the guide. UKB pairs each
diagnosis array with a parallel date array of the same length (41270 with 41280, 41271
with 41281, 259 arrays each), matched by array index.
"""
from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_ukb_for_mapping.R"
pytestmark = pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")


def _run_script(tmp_path: Path, header: list[str], rows: list[list[str]], name: str):
    source = tmp_path / f"{name}.csv"
    write_csv(source, header, rows)
    cohort, events = tmp_path / f"c_{name}.csv.gz", tmp_path / f"e_{name}.csv.gz"
    proc = subprocess.run(
        ["Rscript", str(SCRIPT), "--input", str(source), "--cohort-out", str(cohort),
         "--events-out", str(events), "--female-code", "0", "--male-code", "1"],
        capture_output=True, text=True)
    return proc, cohort, events


def _read(path: Path) -> list[str]:
    return gzip.open(path, "rt").read().strip().splitlines()


def test_codes_are_paired_with_their_own_array_index_date(tmp_path: Path) -> None:
    """41270-0.N must take its date from 41280-0.N, not from any other index."""
    proc, _, events = _run_script(
        tmp_path,
        ["eid", "22001-0.0", "41270-0.0", "41270-0.1", "41280-0.0", "41280-0.1"],
        [["1", "0", "I10", "E11", "2010-01-01", "2015-06-30"],
         ["2", "1", "I10", "", "2012-03-04", ""]],
        "paired")
    assert proc.returncode == 0, proc.stderr
    rows = _read(events)
    assert rows[0] == "person_id,code,vocabulary,event_date"
    got = sorted(rows[1:])
    assert got == sorted(["1,I10,ICD10,2010-01-01", "1,E11,ICD10,2015-06-30",
                          "2,I10,ICD10,2012-03-04"]), got
    assert "two-dates is usable" in proc.stdout


def test_no_date_columns_omits_event_date_rather_than_emitting_blanks(tmp_path: Path) -> None:
    """An all-blank column satisfies "two-dates requires event_date" and then yields
    zero cases in silence. Omitting it makes that check fire instead."""
    proc, _, events = _run_script(
        tmp_path, ["eid", "22001-0.0", "41270-0.0"], [["1", "0", "I10"]], "undated")
    assert proc.returncode == 0, proc.stderr
    assert _read(events)[0] == "person_id,code,vocabulary"
    assert "event_date omitted" in proc.stdout


def test_unpairable_date_columns_stop_rather_than_mis_date(tmp_path: Path) -> None:
    """If UKB ever changes the pairing, mis-dating every event silently is the worst
    available outcome. 41270-0.1 has no 41280-0.1 here."""
    proc, _, _ = _run_script(
        tmp_path,
        ["eid", "22001-0.0", "41270-0.0", "41270-0.1", "41280-0.0"],
        [["1", "0", "I10", "E11", "2010-01-01"]],
        "orphan")
    assert proc.returncode != 0
    assert "could not be paired by array index" in proc.stderr


def test_the_dotted_field_naming_is_handled_too(tmp_path: Path) -> None:
    """UKB extracts appear as both `41270-0.0` and `f.41270.0.0`."""
    proc, _, events = _run_script(
        tmp_path,
        ["f.eid", "f.22001.0.0", "f.41270.0.0", "f.41280.0.0"],
        [["1", "0", "I10", "2010-01-01"]],
        "dotted")
    assert proc.returncode == 0, proc.stderr
    assert _read(events)[1] == "1,I10,ICD10,2010-01-01"


def test_the_output_feeds_two_dates_end_to_end(tmp_path: Path) -> None:
    """The point of the change: a site following the guide can now use two-dates.

    Note what it means on this source. 41280 is the date a code was FIRST recorded, so
    a person is a case when two DIFFERENT codes mapping to one phecode were first
    recorded on different days -- p1 below. p2 has two codes on the SAME day and is
    correctly not a case.
    """
    proc, cohort, events = _run_script(
        tmp_path,
        ["eid", "22001-0.0", "41270-0.0", "41270-0.1", "41280-0.0", "41280-0.1"],
        [["1", "0", "I10", "I11", "2010-01-01", "2015-06-30"],
         ["2", "0", "I10", "I11", "2011-02-02", "2011-02-02"],
         ["3", "0", "", "", "", ""]],
        "e2e")
    assert proc.returncode == 0, proc.stderr

    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_401", "I10", "ICD10"], ["CV_401", "I11", "ICD10"]])
    info = tmp_path / "i.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_401", "Both", "Hypertension", "Cardiovascular"]])
    release = tmp_path / "rel"
    build_vocabulary(source, info, release, None)

    out = tmp_path / "run"
    map_phecodes(release, cohort, events, out, case_rule="two-dates", min_cases=1, min_controls=1)
    cases = {r[0] for r in duckdb.sql(
        f"SELECT person_id FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchall()}
    assert cases == {"1"}, f"expected only the person with two distinct dates, got {cases}"
