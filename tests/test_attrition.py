"""The cohort-size attrition curve, and the reconciler that checks it against a run.

Split out of test_distribution.py, which had become a grab-bag: packaging, licences,
docs-consistency and this. These tests are about one thing -- whether the curve tells
an analyst the truth about how many phenotypes a cohort of a given size yields.

The curve reapplies phecodex_mapper.retention's rule rather than its own copy of it,
so what is left to test is the part it genuinely cannot see: the run's control
REMOVALS (sub-threshold carriers under --case-rule two-dates, and non-cases named by
--control-exclusions). Those are the realistic ways curve and run diverge, and both
have a positive control below.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_attrition_script_runs_as_documented(tmp_path: Path) -> None:
    """ANALYST_GUIDE now tells analysts to run this; nothing had ever executed it.

    test_bundled_scripts_run_from_the_extracted_bundle checks --help. This runs the
    documented invocation against real mapper output and checks the two files it
    promises actually appear.
    """
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary
    import random

    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "I10", "ICD10CM"], ["GU_001", "A01.1", "ICD10CM"]])
    info = tmp_path / "i.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "Hypertension", "CV"], ["GU_001", "Both", "Other", "GU"]])
    release = tmp_path / "rel"
    build_vocabulary(source, info, release, None)

    rng = random.Random(5)
    people = [[f"p{i:05d}", rng.choice(["Male", "Female"])] for i in range(600)]
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], people)
    write_csv(events, ["person_id", "code", "vocabulary"],
              [[p[0], rng.choice(["I10", "A01.1"]), "ICD10CM"] for p in people])
    run = tmp_path / "run"
    map_phecodes(release, cohort, events, run, min_cases=5, min_controls=5)

    out_csv, out_svg = tmp_path / "attrition.csv", tmp_path / "attrition.svg"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/plot_phecode_attrition.py"),
         "--cohort", str(cohort), "--person-phecodes", str(run / "person_phecodes.parquet"),
         "--output-csv", str(out_csv), "--output-svg", str(out_svg),
         "--sample-sizes", "100,300,600", "--min-cases", "5", "--min-controls", "5"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert out_svg.is_file() and out_svg.stat().st_size > 0
    rows = out_csv.read_text().strip().splitlines()
    assert rows[0].startswith("sample_size,retained_phecodes")
    assert len(rows) == 4, f"expected a row per requested size, got {rows}"


def test_sample_sizes_above_the_cohort_are_skipped_not_fabricated(tmp_path: Path) -> None:
    """The flag's help promises this; a fabricated point would misread as real attrition."""
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary

    source = tmp_path / "m2.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "i2.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"], [["CV_003", "Both", "H", "CV"]])
    release = tmp_path / "rel2"
    build_vocabulary(source, info, release, None)
    cohort, events = tmp_path / "c2.csv", tmp_path / "e2.csv"
    write_csv(cohort, ["person_id", "sex"], [[f"p{i}", "Female"] for i in range(50)])
    write_csv(events, ["person_id", "code", "vocabulary"], [[f"p{i}", "I10", "ICD10CM"] for i in range(50)])
    run = tmp_path / "run2"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=1)

    out_csv, out_svg = tmp_path / "a2.csv", tmp_path / "a2.svg"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/plot_phecode_attrition.py"),
         "--cohort", str(cohort), "--person-phecodes", str(run / "person_phecodes.parquet"),
         "--output-csv", str(out_csv), "--output-svg", str(out_svg),
         "--sample-sizes", "10,50,100000", "--min-cases", "1", "--min-controls", "1"],
        check=True, capture_output=True, text=True)
    sizes = [line.split(",")[0] for line in out_csv.read_text().strip().splitlines()[1:]]
    assert sizes == ["10", "50"], f"a size larger than the cohort was reported: {sizes}"


def test_every_sample_size_above_the_cohort_leaves_a_readable_output(tmp_path: Path) -> None:
    """Skipping all of them must not become a traceback from max() on an empty list."""
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary

    source = tmp_path / "m3.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "i3.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"], [["CV_003", "Both", "H", "CV"]])
    release = tmp_path / "rel3"
    build_vocabulary(source, info, release, None)
    cohort, events = tmp_path / "c3.csv", tmp_path / "e3.csv"
    write_csv(cohort, ["person_id", "sex"], [[f"p{i}", "Female"] for i in range(20)])
    write_csv(events, ["person_id", "code", "vocabulary"], [[f"p{i}", "I10", "ICD10CM"] for i in range(20)])
    run = tmp_path / "run3"
    map_phecodes(release, cohort, events, run, min_cases=1, min_controls=1)

    out_csv, out_svg = tmp_path / "a3.csv", tmp_path / "a3.svg"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/plot_phecode_attrition.py"),
         "--cohort", str(cohort), "--person-phecodes", str(run / "person_phecodes.parquet"),
         "--output-csv", str(out_csv), "--output-svg", str(out_svg),
         "--sample-sizes", "5000,10000", "--min-cases", "1", "--min-controls", "1"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert out_csv.read_text().strip().splitlines()[1:] == [], "a skipped size produced a row"
    assert out_svg.is_file() and "No sample size" in out_svg.read_text()


def _attrition_fixture(tmp_path: Path, *, female_cases: int):
    """A Female-only phecode where the sex-aware and sex-blind answers can differ."""
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary

    write_csv(tmp_path / "m.csv", ["phecode", "ICD", "vocabulary_id"],
              [["FE_001", "B01.1", "ICD10CM"]])
    write_csv(tmp_path / "i.csv", ["phecode", "sex", "phecode_string", "category"],
              [["FE_001", "Female", "F1", "X"]])
    release = tmp_path / "rel"
    build_vocabulary(tmp_path / "m.csv", tmp_path / "i.csv", release, None)

    people = ([[f"f{i:05d}", "Female"] for i in range(2000)]
              + [[f"m{i:05d}", "Male"] for i in range(2000)])
    write_csv(tmp_path / "c.csv", ["person_id", "sex"], people)
    write_csv(tmp_path / "e.csv", ["person_id", "code", "vocabulary"],
              [[f"f{i:05d}", "B01.1", "ICD10CM"] for i in range(female_cases)])
    run = tmp_path / "run"
    map_phecodes(release, tmp_path / "c.csv", tmp_path / "e.csv", run,
                 min_cases=100, min_controls=100)
    return release, tmp_path / "c.csv", run


def _attrition(tmp_path: Path, cohort: Path, run: Path, name: str, release: Path | None):
    out_csv = tmp_path / f"{name}.csv"
    cmd = [sys.executable, str(ROOT / "scripts/plot_phecode_attrition.py"),
           "--cohort", str(cohort), "--person-phecodes", str(run / "person_phecodes.parquet"),
           "--output-csv", str(out_csv), "--output-svg", str(tmp_path / f"{name}.svg"),
           "--sample-sizes", "4000", "--min-cases", "100", "--min-controls", "100"]
    if release:
        cmd += ["--release", str(release)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return int(out_csv.read_text().strip().splitlines()[1].split(",")[1])


def test_the_attrition_curve_reproduces_a_real_run_at_full_cohort_size(tmp_path: Path) -> None:
    """If the curve disagrees with the mapper at n = the whole cohort, it is wrong
    everywhere else too. 1,950 of 2,000 females are cases, so female controls are 50 --
    below the threshold -- and a real run drops the phecode."""
    import json
    release, cohort, run = _attrition_fixture(tmp_path, female_cases=1950)
    actual = json.loads((run / "audit.json").read_text())["phenotype_matrix"]["n_columns"]
    assert actual == 0
    assert _attrition(tmp_path, cohort, run, "aware", release) == actual


def test_ignoring_sex_restrictions_overstates_how_many_phecodes_survive(tmp_path: Path) -> None:
    """The reason --release matters for a published curve.

    Scored against the whole sample the phecode has 2,050 controls and looks retainable;
    scored against females, which is what a real run does, it has 50 and is dropped.
    Without --release the curve therefore promises analysts phenotypes they will not get.
    """
    release, cohort, run = _attrition_fixture(tmp_path, female_cases=1950)
    assert _attrition(tmp_path, cohort, run, "blind", None) == 1
    assert _attrition(tmp_path, cohort, run, "aware2", release) == 0


def test_a_cohort_without_sex_is_refused_when_the_release_restricts_phecodes(tmp_path: Path) -> None:
    """Silently falling back to sex-blind counting would publish the overstated curve."""
    from conftest import write_csv
    release, _, run = _attrition_fixture(tmp_path, female_cases=500)
    nosex = tmp_path / "nosex.csv"
    write_csv(nosex, ["person_id"], [[f"f{i:05d}"] for i in range(2000)])
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/plot_phecode_attrition.py"),
         "--cohort", str(nosex), "--release", str(release),
         "--person-phecodes", str(run / "person_phecodes.parquet"),
         "--output-csv", str(tmp_path / "x.csv"), "--output-svg", str(tmp_path / "x.svg"),
         "--sample-sizes", "1000"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "overstate retention" in result.stderr


def test_the_reconciler_agrees_when_the_curve_is_faithful(tmp_path: Path) -> None:
    """At full cohort size the curve must reproduce the run. If it does not, the curve
    is wrong at every smaller sample size too."""
    release, cohort, run = _attrition_fixture(tmp_path, female_cases=500)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/reconcile_attrition.py"),
         "--run", str(run), "--release", str(release), "--cohort", str(cohort),
         "--min-cases", "100", "--min-controls", "100"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all three agree" in result.stdout


def test_the_reconciler_names_the_phecodes_behind_a_discrepancy(tmp_path: Path) -> None:
    """Positive control for a TAMPERED run directory, not for a realistic divergence.

    This mixes person_phecodes from a run without phenotype exclusions against counts
    from a run with them. map_phecodes cannot produce that: `cases` (-> person_phecodes)
    and `all_phecodes` (-> phecode_counts) are built from the same not_excluded
    predicate, so phecode_counts is always a superset and a phecode can never be
    'absent'. What this pins is that the reconciler notices a directory assembled from
    two runs. The divergences a SINGLE run really produces -- control exclusions and
    two-dates sub-threshold carriers -- are the two tests below, and for years they had
    no coverage at all while this one stood in for them.
    """
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary

    write_csv(tmp_path / "m.csv", ["phecode", "ICD", "vocabulary_id"],
              [[f"PH_{i:03d}", f"A{i:02d}.1", "ICD10CM"] for i in range(4)])
    write_csv(tmp_path / "i.csv", ["phecode", "sex", "phecode_string", "category"],
              [[f"PH_{i:03d}", "Both", f"P{i}", "Symptoms" if i < 2 else "Other"]
               for i in range(4)])
    release = tmp_path / "rel"
    build_vocabulary(tmp_path / "m.csv", tmp_path / "i.csv", release, None)
    people = [[f"p{i:05d}", "Female" if i % 2 else "Male"] for i in range(4000)]
    cohort = tmp_path / "c.csv"
    write_csv(cohort, ["person_id", "sex"], people)
    # Varying rates so each phecode has BOTH cases and controls; giving everyone every
    # code leaves zero controls, nothing is retained either way, and the comparison
    # agrees trivially at 0 -- proving nothing.
    import random as _random
    rng = _random.Random(3)
    write_csv(tmp_path / "e.csv", ["person_id", "code", "vocabulary"],
              [[p[0], f"A{i:02d}.1", "ICD10CM"] for p in people
               for i, rate in enumerate((0.30, 0.25, 0.20, 0.15)) if rng.random() < rate])
    drop = tmp_path / "drop.csv"
    write_csv(drop, ["match_type", "match_value"], [["category", "Symptoms"]])

    map_phecodes(release, cohort, tmp_path / "e.csv", tmp_path / "with_excl",
                 min_cases=100, min_controls=100, exclude_phenotypes=drop)
    map_phecodes(release, cohort, tmp_path / "e.csv", tmp_path / "without_excl",
                 min_cases=100, min_controls=100)

    mixed = tmp_path / "mixed"
    mixed.mkdir()
    for name in ("audit.json", "phecode_counts.parquet"):
        (mixed / name).write_bytes((tmp_path / "with_excl" / name).read_bytes())
    (mixed / "person_phecodes.parquet").write_bytes(
        (tmp_path / "without_excl" / "person_phecodes.parquet").read_bytes())

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/reconcile_attrition.py"),
         "--run", str(mixed), "--release", str(release), "--cohort", str(cohort),
         "--min-cases", "100", "--min-controls", "100"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "all three agree" not in result.stdout
    assert "the CURVE keeps but the RUN dropped" in result.stdout
    assert "PH_000" in result.stdout


def _divergence_fixture(tmp_path: Path, *, case_rule: str, control_exclusions: bool):
    """A run where the curve's control model and the run's must differ, by construction.

    CV_003 has 10 cases out of 40 people, so the curve sees 30 controls. The run removes
    15 of them -- either as non-cases named by --control-exclusions, or (under
    two-dates) as single-date sub-threshold carriers. At --min-controls 20 that is
    exactly the difference between retained and dropped, so the curve promises a
    phenotype the run does not deliver.
    """
    from conftest import write_csv
    from phecodex_mapper.mapper import map_phecodes
    from phecodex_mapper.vocabulary import build_vocabulary

    write_csv(tmp_path / "m.csv", ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "A01.1", "ICD10CM"], ["SS_004", "A02.0", "ICD10CM"]])
    write_csv(tmp_path / "i.csv", ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "Unrestricted", "Cardiovascular"],
               ["SS_004", "Both", "Other", "Cardiovascular"]])
    release = tmp_path / "rel"
    build_vocabulary(tmp_path / "m.csv", tmp_path / "i.csv", release, None)

    cohort = tmp_path / "c.csv"
    write_csv(cohort, ["person_id", "sex"],
              [[f"p{i:03d}", "Female" if i % 2 else "Male"] for i in range(40)])
    # CV_003's 10 cases carry two distinct dates, so they are cases under BOTH rules.
    events = [[f"p{i:03d}", "A01.1", "ICD10CM", d]
              for i in range(10) for d in ("2010-01-01", "2011-01-01")]
    # The 15 people the run removes from CV_003's control pool, by whichever mechanism
    # is under test. Under --control-exclusions they carry a DIFFERENT code that a rule
    # names; under two-dates they carry CV_003's OWN code once, which makes them
    # sub-threshold for CV_003 -- neither case nor control -- rather than for some other
    # phecode, which is the mistake that makes this fixture prove nothing.
    removed_code = "A02.0" if control_exclusions else "A01.1"
    events += [[f"p{i:03d}", removed_code, "ICD10CM", "2010-01-01"] for i in range(10, 25)]
    write_csv(tmp_path / "e.csv", ["person_id", "code", "vocabulary", "event_date"], events)

    exclusions = None
    if control_exclusions:
        exclusions = tmp_path / "cx.csv"
        write_csv(exclusions, ["phecode", "exclusion_type", "exclusion_value", "vocabulary"],
                  [["CV_003", "code", "A02.0", "ICD10CM"]])

    run = tmp_path / "run"
    map_phecodes(release, cohort, tmp_path / "e.csv", run, case_rule=case_rule,
                 exclusions=exclusions, min_cases=5, min_controls=20)
    return release, cohort, run


def _reconcile(release: Path, cohort: Path, run: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/reconcile_attrition.py"),
         "--run", str(run), "--release", str(release), "--cohort", str(cohort),
         "--min-cases", "5", "--min-controls", "20"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_the_reconciler_attributes_a_control_exclusion_gap(tmp_path: Path) -> None:
    """The realistic divergence the reconciler used to mis-diagnose.

    A --control-exclusions rule removes 15 non-cases from CV_003's control pool. The
    curve cannot see person-level removals, so it keeps a phecode the run drops. The
    reconciler must name the phecode AND attribute the gap to the run's own removal
    columns -- it used to print, as "the likeliest cause", a --exclude-phenotypes story
    that map_phecodes cannot produce, while this cause went unmentioned.
    """
    release, cohort, run = _divergence_fixture(tmp_path, case_rule="any-event",
                                               control_exclusions=True)
    out = _reconcile(release, cohort, run)
    assert "all three agree" not in out
    assert "the CURVE keeps but the RUN dropped" in out
    assert "CV_003" in out
    # 30 curve controls, 15 run controls, all 15 removed as excluded non-cases.
    row = next(line for line in out.splitlines() if "CV_003" in line and "Both" in line)
    assert row.split() == ["CV_003", "Both", "10", "30", "15", "+15", "0", "15"], row
    assert "NOT explained by the known removals" not in out


def test_the_reconciler_attributes_a_two_dates_subthreshold_gap(tmp_path: Path) -> None:
    """The second realistic divergence, which had no coverage and no mention anywhere.

    Under --case-rule two-dates a single-date carrier is neither case nor control. The
    curve counts them as controls, so it again keeps a phecode the run drops -- with no
    --control-exclusions file involved at all.
    """
    release, cohort, run = _divergence_fixture(tmp_path, case_rule="two-dates",
                                               control_exclusions=False)
    out = _reconcile(release, cohort, run)
    assert "all three agree" not in out
    assert "CV_003" in out
    row = next(line for line in out.splitlines() if "CV_003" in line and "Both" in line)
    # Same +15 gap, attributed to sub-threshold carriers rather than exclusions.
    assert row.split() == ["CV_003", "Both", "10", "30", "15", "+15", "15", "0"], row
    assert "NOT explained by the known removals" not in out


def test_the_curve_and_the_run_agree_when_nothing_is_removed(tmp_path: Path) -> None:
    """Negative control. Without exclusions and under any-event no one is removed from a
    control pool, so the curve's model and the run's must agree exactly. A reconciler
    that flagged a discrepancy here would be crying wolf on a correct run."""
    release, cohort, run = _divergence_fixture(tmp_path, case_rule="any-event",
                                               control_exclusions=False)
    assert "all three agree" in _reconcile(release, cohort, run)
