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
    """ICD10CM A01.1, ICD9CM 123.4, and WHO-only ICD10 B02.2 -- nothing else.

    B02.2 exists under ICD10 and not ICD10CM, which is the shape that makes a
    mislabel detectable: declared as ICD10CM it fails, and the sibling map would
    have taken it.
    """
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["GU_001", "A01.1", "ICD10CM"], ["CV_003", "123.4", "ICD9CM"],
               ["GU_001", "B02.2", "ICD10"]])
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


def test_a_mislabelled_vocabulary_warns(tmp_path: Path, capsys) -> None:
    """The real scenario: WHO ICD-10 codes declared as ICD10CM.

    What makes this detectable is not the unmapped rate -- it is that the failing
    codes would map under the sibling label.
    """
    release = _release(tmp_path)
    events = [[f"p{i}", "B02.2", "ICD10CM"] for i in range(1200)]
    _run(tmp_path, release, events, "bad")
    err = capsys.readouterr().err
    assert "looks mislabelled" in err
    assert "ICD10CM" in err and "ICD10" in err
    assert "WOULD map" in err, "the warning must show the evidence, not just the rate"


def test_a_coarse_map_does_not_warn_however_bad_the_rate(tmp_path: Path, capsys) -> None:
    """The decisive negative control, and the reason this check was rewritten.

    A correctly-labelled UK Biobank extract sits at 20.3% unmapped because PhecodeX's
    WHO map is coarse, not because the label is wrong. The old rate-only threshold
    fired on exactly that -- flagging the right answer and advising a relabel that
    corrupts the run. Codes absent from BOTH maps must produce silence no matter how
    many of them there are.
    """
    release = _release(tmp_path)
    events = [[f"p{i}", "Z99.9", "ICD10CM"] for i in range(1200)]
    _run(tmp_path, release, events, "coarse")
    assert "mislabelled" not in capsys.readouterr().err


def test_the_audit_records_the_counterfactual_not_just_the_rate(tmp_path: Path) -> None:
    """The evidence behind the warning belongs in audit.json, fired or not."""
    release = _release(tmp_path)
    events = [[f"p{i}", "B02.2", "ICD10CM"] for i in range(1200)]
    audit = _run(tmp_path, release, events, "cf")
    cm = audit["unmapped_by_vocabulary"]["ICD10CM"]
    assert cm["sibling_vocabulary"] == "ICD10"
    assert cm["unmapped_events_that_would_map_as_sibling"] == 1200
    assert cm["share_of_unmapped_rescued_by_sibling"] == 1.0


def test_a_healthy_vocabulary_does_not_warn(tmp_path: Path, capsys) -> None:
    """Positive control: the warning must not fire on a normal run.

    Without this, a warning printed unconditionally would satisfy the test above
    while telling an analyst nothing.
    """
    release = _release(tmp_path)
    events = [[f"p{i}", "A01.1", "ICD10CM"] for i in range(1200)]
    _run(tmp_path, release, events, "good")
    assert "mislabelled" not in capsys.readouterr().err


def test_a_small_vocabulary_does_not_warn(tmp_path: Path, capsys) -> None:
    """A handful of odd codes is not evidence of a mislabelled vocabulary.

    The threshold is on volume as well as rate, so a rare vocabulary with a few
    unmapped events does not produce noise that trains analysts to ignore it.
    """
    release = _release(tmp_path)
    events = [["p1", "A01.1", "ICD10CM"], ["p2", "B02.2", "ICD10CM"]]
    _run(tmp_path, release, events, "small")
    assert "mislabelled" not in capsys.readouterr().err


def test_the_threshold_is_pinned_from_both_sides(tmp_path: Path, capsys) -> None:
    """A constant no test can move is a constant that can be silently disabled.

    Both cases below are the measured reality rather than round numbers. On 2.5M
    UK Biobank events the sibling label rescues 19.8% of the failures when the
    vocabulary really is mislabelled, and 0.8% when it is labelled correctly and
    the map is merely coarse. The 5% threshold has to separate those two, so the
    test asserts against both -- with only the extreme 100% case, raising the
    threshold to 0.99 disables the check and nothing notices.
    """
    release = _release(tmp_path)

    # 240 rescuable of 1,200 unmapped = 20%, the true-mislabel case.
    events = ([[f"p{i}", "B02.2", "ICD10CM"] for i in range(240)]
              + [[f"q{i}", "Z99.9", "ICD10CM"] for i in range(960)])
    audit = _run(tmp_path, release, events, "mixed_hi")
    share = audit["unmapped_by_vocabulary"]["ICD10CM"]["share_of_unmapped_rescued_by_sibling"]
    assert 0.19 < share < 0.21
    assert "looks mislabelled" in capsys.readouterr().err, f"share {share:.1%} should warn"

    # 10 rescuable of 1,250 unmapped = 0.8%, the correctly-labelled coarse-map case.
    events = ([[f"r{i}", "B02.2", "ICD10CM"] for i in range(10)]
              + [[f"s{i}", "Z99.9", "ICD10CM"] for i in range(1240)])
    audit = _run(tmp_path, release, events, "mixed_lo")
    share = audit["unmapped_by_vocabulary"]["ICD10CM"]["share_of_unmapped_rescued_by_sibling"]
    assert 0.005 < share < 0.01
    assert "looks mislabelled" not in capsys.readouterr().err, f"share {share:.1%} must not warn"
