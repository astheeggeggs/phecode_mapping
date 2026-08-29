"""The de-identification scripts, exercised on simulated UK Biobank data.

These are the scripts that handle real patient data, and nothing had ever run them.
They can only be tested on synthetic input by construction -- which is the point: the
generator here produces UKB-shaped extracts with known structure, so the privacy
properties can be asserted rather than assumed.

What the script claims, from its own header:

    1. explodes every person's code string into one row per (eid, code)
    2. drops eid entirely and assigns fresh synthetic person_id integers
    3. block-shuffles codes across synthetic people, preserving each person's
       *code count* ... while destroying every real per-person code combination
    4. synthesizes event_date uniformly at random ... never reuse real dates

Each of those is a testable claim, and 2 and 3 are the ones that matter: a fake ID over
a preserved code combination is not de-identified, because a rare diagnosis pattern is
itself identifying.
"""
from __future__ import annotations

import csv
import gzip
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from conftest import write_csv

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deidentify_ukb_for_testing.R"
pytestmark = pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")

# Distinctive eids: long, unmistakable, and impossible to produce by accident from
# the synthetic person_id sequence (which is 1..N).
EIDS = [str(7_000_000 + i) for i in range(120)]


def _simulate_ukb(path: Path) -> dict[str, set[str]]:
    """A UKB-shaped wide extract where every person has a UNIQUE pair of codes.

    Uniqueness is what makes the block shuffle measurable: if per-person combinations
    survived, each synthetic person would still hold a matching pair. A pool of shared
    codes would hide that behind coincidence.
    """
    header = ["eid", "22001-0.0", "41270-0.0", "41270-0.1", "41271-0.0"]
    rows, expected = [], {}
    for i, eid in enumerate(EIDS):
        # A00..A99 then B00.. -- two ICD-10 codes unique to this person, one ICD-9.
        a = f"{chr(65 + i // 50)}{i % 50:02d}"
        b = f"{chr(75 + i // 50)}{i % 50:02d}"
        nine = f"{100 + i}"
        sex = "0" if i % 2 else "1"
        rows.append([eid, sex, a, b, nine])
        expected[eid] = {a, b, nine}
    write_csv(path, header, rows)
    return expected


def _run(tmp_path: Path, source: Path, name: str, extra: list[str] | None = None):
    events, cohort = tmp_path / f"e_{name}.csv.gz", tmp_path / f"c_{name}.csv.gz"
    proc = subprocess.run(
        ["Rscript", str(SCRIPT), "--input", str(source), "--events-out", str(events),
         "--cohort-out", str(cohort), "--seed", "7", "--female-code", "0",
         "--male-code", "1"] + (extra or []),
        capture_output=True, text=True)
    return proc, events, cohort


def _read_gz(path: Path) -> list[dict]:
    return list(csv.DictReader(gzip.open(path, "rt")))


def test_no_real_eid_survives_into_any_output(tmp_path: Path) -> None:
    """The property everything else rests on."""
    source = tmp_path / "ukb.csv"
    _simulate_ukb(source)
    proc, events, cohort = _run(tmp_path, source, "leak")
    assert proc.returncode == 0, proc.stderr

    leaked = set(EIDS) & {r["person_id"] for r in _read_gz(events)}
    assert leaked == set(), f"real eids appear as person_id in events: {sorted(leaked)[:5]}"
    leaked = set(EIDS) & {r["person_id"] for r in _read_gz(cohort)}
    assert leaked == set(), f"real eids appear as person_id in cohort: {sorted(leaked)[:5]}"

    # Nor anywhere in the raw bytes -- a crosswalk column, a comment, a stray field.
    for path in (events, cohort):
        text = gzip.open(path, "rt").read()
        assert not any(eid in text for eid in EIDS), f"an eid appears in {path.name}"


def test_per_person_code_combinations_are_destroyed(tmp_path: Path) -> None:
    """A fake ID over a preserved code combination is not de-identification.

    Every simulated person holds a pair of codes unique to them, so if combinations
    survived the shuffle each synthetic person would still hold a matching pair.
    """
    # ICD-10 ONLY, deliberately. With codes in both vocabularies the events table is
    # built ICD-9-first while the shuffle vector is built in row order, so the two are
    # misaligned anyway -- and a fixture that mixes them cannot tell a real shuffle from
    # that accident. Removing the shuffle then passes, which is precisely the
    # "relabelling is not sufficient" failure this test exists for.
    source = tmp_path / "ukb_icd10.csv"
    header = ["eid", "22001-0.0", "41270-0.0", "41270-0.1"]
    rows, expected = [], {}
    for i, eid in enumerate(EIDS):
        a, b = f"{chr(65 + i // 50)}{i % 50:02d}", f"{chr(75 + i // 50)}{i % 50:02d}"
        rows.append([eid, "0" if i % 2 else "1", a, b])
        expected[eid] = {a, b}
    write_csv(source, header, rows)
    proc, events, _ = _run(tmp_path, source, "combo")
    assert proc.returncode == 0, proc.stderr

    by_person: dict[str, set[str]] = {}
    for row in _read_gz(events):
        by_person.setdefault(row["person_id"], set()).add(row["code"])

    original_sets = [frozenset(v) for v in expected.values()]
    intact = sum(1 for codes in by_person.values() if frozenset(codes) in original_sets)
    assert intact <= 2, f"{intact} synthetic people kept an original code combination intact"


def test_code_counts_and_totals_are_preserved(tmp_path: Path) -> None:
    """The shuffle must preserve the marginals it claims to, or the fixture stops
    being realistic: per-phecode case counts come from the global code multiset, and
    the codes-per-person distribution drives how many people carry anything at all."""
    source = tmp_path / "ukb.csv"
    expected = _simulate_ukb(source)
    proc, events, _ = _run(tmp_path, source, "marg")
    assert proc.returncode == 0, proc.stderr
    rows = _read_gz(events)

    want = Counter(code for codes in expected.values() for code in codes)
    got = Counter(row["code"] for row in rows)
    assert got == want, "the global multiset of codes changed"

    per_person = Counter(row["person_id"] for row in rows)
    assert sorted(per_person.values()) == sorted(len(v) for v in expected.values())


def test_people_with_no_codes_are_kept_in_the_cohort(tmp_path: Path) -> None:
    """They are the control pool. Dropping them silently biases every case/control
    count downstream, which is the script's own stated reason for keeping them."""
    source = tmp_path / "ukb_sparse.csv"
    write_csv(source, ["eid", "22001-0.0", "41270-0.0", "41271-0.0"],
              [["7000001", "0", "A01", ""], ["7000002", "1", "", ""],
               ["7000003", "0", "", ""], ["7000004", "1", "B02", ""]])
    proc, events, cohort = _run(tmp_path, source, "sparse")
    assert proc.returncode == 0, proc.stderr
    assert len(_read_gz(cohort)) == 4, "people with no codes were dropped from the cohort"
    assert len(_read_gz(events)) == 2


def test_dates_are_synthetic_in_range_and_distinct_per_person_and_code(tmp_path: Path) -> None:
    """Dates are invented, never reused -- the source has none, and reusing real ones
    would itself be a re-identification risk. Within a (person, code) group they are
    sampled without replacement so a repeated code cannot collapse onto one day and
    silently look like a single event to --case-rule two-dates."""
    source = tmp_path / "ukb_dates.csv"
    write_csv(source, ["eid", "22001-0.0", "41270-0.0", "41270-0.1"],
              [["7000001", "0", "A01 A01 A01", "B02"], ["7000002", "1", "A01", "B02"]])
    proc, events, _ = _run(tmp_path, source, "dates")
    assert proc.returncode == 0, proc.stderr
    rows = _read_gz(events)

    assert all("2000-01-01" <= r["event_date"] <= "2022-12-31" for r in rows), \
        "a date fell outside the documented range"
    seen: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        seen.setdefault((r["person_id"], r["code"]), []).append(r["event_date"])
    for key, dates in seen.items():
        assert len(dates) == len(set(dates)), f"{key} has a repeated date, so two events look like one"


def test_the_same_seed_reproduces_the_same_fixture(tmp_path: Path) -> None:
    """A fixture nobody can regenerate is not a fixture."""
    source = tmp_path / "ukb.csv"
    _simulate_ukb(source)
    first = _run(tmp_path, source, "seed_a")
    second = _run(tmp_path, source, "seed_b")
    assert first[0].returncode == 0 and second[0].returncode == 0
    assert _read_gz(first[1]) == _read_gz(second[1])
    assert _read_gz(first[2]) == _read_gz(second[2])


def test_an_unexpected_sex_code_is_refused(tmp_path: Path) -> None:
    """Guessing the encoding silently mislabels every person's sex."""
    source = tmp_path / "ukb_sex.csv"
    write_csv(source, ["eid", "22001-0.0", "41270-0.0", "41271-0.0"],
              [["7000001", "0", "A01", ""], ["7000002", "M", "B02", ""]])
    proc, _, _ = _run(tmp_path, source, "sex")
    assert proc.returncode != 0
    assert "Unexpected sex code" in proc.stderr


def test_uncompressed_output_paths_are_refused(tmp_path: Path) -> None:
    """Individual-level-shaped output should not be written in the clear by accident."""
    source = tmp_path / "ukb.csv"
    _simulate_ukb(source)
    proc = subprocess.run(
        ["Rscript", str(SCRIPT), "--input", str(source), "--events-out", str(tmp_path / "e.csv"),
         "--cohort-out", str(tmp_path / "c.csv.gz"), "--female-code", "0", "--male-code", "1"],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "must be gzip-compressed" in proc.stderr


# ---------------------------------------------------------------------------
# The primary-care script
# ---------------------------------------------------------------------------

GP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deidentify_ukb_gp_for_testing.R"
GP_EIDS = [str(8_000_000 + i) for i in range(60)]


def _simulate_gp(path: Path) -> None:
    """A gp_clinical-shaped extract where each person has a unique pair of codes.

    Code i pairs 100000+i with 900000+i, so an intact pair is arithmetically
    detectable and cannot be confused with coincidence.
    """
    lines = ["eid\tevent_dt\tsnomed_code"]
    for i, eid in enumerate(GP_EIDS):
        lines.append(f"{eid}\t0{(i % 9) + 1}/03/2015\t{100000 + i}")
        lines.append(f"{eid}\t1{(i % 9) + 1}/06/2016\t{900000 + i}")
    lines.append("8000099\t01/01/1901\t555555")   # sentinel date, must be dropped
    path.write_text("\n".join(lines) + "\n")


def _run_gp(tmp_path: Path, source: Path, name: str):
    events, cohort = tmp_path / f"ge_{name}.csv", tmp_path / f"gc_{name}.csv"
    proc = subprocess.run(
        ["Rscript", str(GP_SCRIPT), "--input", str(source), "--events-out", str(events),
         "--cohort-out", str(cohort), "--seed", "1"], capture_output=True, text=True)
    return proc, events, cohort


def _pairs_intact(events: Path) -> int:
    by: dict[str, set[str]] = {}
    for row in csv.DictReader(events.open()):
        by.setdefault(row["person_id"], set()).add(row["code"])
    return sum(1 for codes in by.values()
               if len(codes) == 2 and int(max(codes)) - 900_000 == int(min(codes)) - 100_000)


def test_gp_code_combinations_are_destroyed_not_merely_relabelled(tmp_path: Path) -> None:
    """The script's own comment claimed this and the code did not do it.

    It merged a fresh id onto each person, which keeps that person's entire event
    history together under a new name -- and a rare code sequence is identifying
    whatever the label says, which is exactly what the sibling ICD script's header
    warns about. Measured before the fix: 60 of 60 original code pairs intact.
    """
    source = tmp_path / "gp.txt"
    _simulate_gp(source)
    proc, events, _ = _run_gp(tmp_path, source, "combo")
    assert proc.returncode == 0, proc.stderr
    intact = _pairs_intact(events)
    assert intact <= 5, f"{intact} of 60 people kept their original code pair"


def test_gp_output_preserves_event_counts_and_drops_no_one(tmp_path: Path) -> None:
    """The shuffle must move codes between people, not lose them."""
    source = tmp_path / "gp2.txt"
    _simulate_gp(source)
    proc, events, cohort = _run_gp(tmp_path, source, "counts")
    assert proc.returncode == 0, proc.stderr
    rows = list(csv.DictReader(events.open()))
    assert len(rows) == 120, "events were lost or invented"
    assert len(list(csv.DictReader(cohort.open()))) == 60
    per_person = Counter(r["person_id"] for r in rows)
    assert sorted(per_person.values()) == [2] * 60


def test_gp_cohort_is_usable_by_the_mapper(tmp_path: Path) -> None:
    """It emitted person_id only, so map-phecodes could not consume it at all.

    gp_clinical carries no sex, so the column is blank rather than invented -- those
    people are non-evaluable for sex-restricted phecodes instead of silently scored
    as controls.
    """
    source = tmp_path / "gp3.txt"
    _simulate_gp(source)
    proc, _, cohort = _run_gp(tmp_path, source, "cohort")
    assert proc.returncode == 0, proc.stderr
    reader = csv.DictReader(cohort.open())
    assert set(reader.fieldnames) == {"person_id", "sex"}, reader.fieldnames
    assert all(row["sex"] == "" for row in reader)


def test_gp_no_real_eid_and_no_sentinel_date_survives(tmp_path: Path) -> None:
    """UKB's placeholder dates are not real events and must not become ones."""
    source = tmp_path / "gp4.txt"
    _simulate_gp(source)
    proc, events, cohort = _run_gp(tmp_path, source, "leak")
    assert proc.returncode == 0, proc.stderr
    for path in (events, cohort):
        text = path.read_text()
        assert not any(eid in text for eid in GP_EIDS), f"a real eid appears in {path.name}"
    assert "555555" not in events.read_text(), "a sentinel-dated event survived"
