"""Regression tests: a vocabulary that maps badly must be visible.

--max-unmapped-rate defaults to 1.0 and the check is `rate > max`, so the hard
guard cannot fire at any input. That default is defensible -- a site cannot know
its own rate before the first run -- but it left nothing at all to notice a bad
one, and the aggregate rate hides the specific failure that matters.

The failure: UK Biobank codes WHO ICD-10. Events declared as ICD10CM are matched
against the CM map, and every WHO-only code is silently discarded. `vocabulary`
is taken as ground truth, so nothing else in the tool can detect it. Two real
runs sat at 23.7-23.8% unmapped and no output remarked on it.

Verified on the real de-identified extract against releases/FIXED-cm-who-snomed:

    ICD10CM   77,115 / 306,953   25.1%   <- warns
    ICD9CM     1,379 /   6,935   19.9%

A per-vocabulary breakdown separates "one vocabulary is wrong" from "a long tail
of odd codes", which the single aggregate number cannot.
"""
from __future__ import annotations

import json
from pathlib import Path

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary


def _release(tmp_path: Path) -> Path:
    """A map holding ICD10CM A01.1 and ICD9CM 123.4 -- nothing else."""
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GU_001", "A01.1", "ICD10CM"], ["CV_003", "123.4", "ICD9CM"]])
    info = tmp_path / "info.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["GU_001", "Both", "Something", "Genitourinary"],
               ["CV_003", "Both", "Other", "Cardiovascular"]])
    release = tmp_path / "rel"
    build_vocabulary(source, info, release, None)
    return release


def _run(tmp_path: Path, release: Path, events: list, name: str) -> dict:
    cohort, ev = tmp_path / f"c_{name}.csv", tmp_path / f"e_{name}.csv"
    people = sorted({r[0] for r in events}) or ["p1"]
    write_csv(cohort, ["person_id", "sex"], [[p, "Female"] for p in people] + [["ctrl", "Female"]])
    write_csv(ev, ["person_id", "code", "vocabulary"], events)
    out = tmp_path / f"run_{name}"
    map_phecodes(release, cohort, ev, out, min_cases=1, min_controls=0, max_unmapped_rate=1.0)
    return json.loads((out / "audit.json").read_text())


def test_unmapped_rate_is_reported_per_vocabulary(tmp_path: Path) -> None:
    """The aggregate cannot distinguish one broken vocabulary from a long tail."""
    release = _release(tmp_path)
    events = ([["p1", "A01.1", "ICD10CM"]] * 2          # maps
              + [["p2", "Z99.9", "ICD10CM"]]            # does not
              + [["p3", "123.4", "ICD9CM"]] * 3)        # maps
    audit = _run(tmp_path, release, events, "split")

    by_vocab = audit["unmapped_by_vocabulary"]
    assert by_vocab["ICD10CM"]["events"] == 3
    assert by_vocab["ICD10CM"]["unmapped"] == 1
    assert by_vocab["ICD9CM"]["unmapped"] == 0
    # The aggregate alone would read as a mild 1-in-6 problem rather than one
    # vocabulary doing a third worse than the other.
    assert audit["unmapped_rate"] == 1 / 6


def test_a_badly_mapping_vocabulary_warns(tmp_path: Path, capsys) -> None:
    """The real scenario: a whole vocabulary matched against the wrong map."""
    release = _release(tmp_path)
    # 1,200 events, none of which the map contains -- the shape of WHO ICD-10
    # codes declared as ICD10CM.
    events = [[f"p{i}", "Z99.9", "ICD10CM"] for i in range(1200)]
    _run(tmp_path, release, events, "bad")
    err = capsys.readouterr().err
    assert "did not map" in err
    assert "ICD10CM" in err
    assert "mislabelled" in err, "the warning should name the likely cause, not just the number"


def test_a_healthy_vocabulary_does_not_warn(tmp_path: Path, capsys) -> None:
    """Positive control: the warning must not fire on a normal run.

    Without this, a warning printed unconditionally would satisfy the test above
    while telling an analyst nothing.
    """
    release = _release(tmp_path)
    events = [[f"p{i}", "A01.1", "ICD10CM"] for i in range(1200)]
    _run(tmp_path, release, events, "good")
    assert "did not map" not in capsys.readouterr().err


def test_a_small_vocabulary_does_not_warn(tmp_path: Path, capsys) -> None:
    """A handful of odd codes is not evidence of a mislabelled vocabulary.

    The threshold is on volume as well as rate, so a rare vocabulary with a few
    unmapped events does not produce noise that trains analysts to ignore it.
    """
    release = _release(tmp_path)
    events = [["p1", "A01.1", "ICD10CM"], ["p2", "999.9", "ICD9CM"]]
    _run(tmp_path, release, events, "small")
    assert "did not map" not in capsys.readouterr().err
