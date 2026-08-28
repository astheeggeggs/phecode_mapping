"""Regression tests: sub-threshold carriers are non-evaluable, not clean controls.

Under --case-rule two-dates a person needs the phecode on two distinct dates to be
a case. Someone with exactly one date used to fall through into the control pool,
so the stricter case rule actively contaminated the control group with likely
cases -- the one configuration where tightening the case definition made the
comparison worse.

This follows PheTK, whose control set is everyone NOT appearing in phecode_counts
for the phecode, with no count threshold applied:

    exclude_range = [phecode] + self._exclude_range(...)
    exclude_ids = phecode_counts.filter(pl.col("phecode").is_in(exclude_range))...
    controls = covariate_df.filter(~(pl.col("person_id").is_in(exclude_ids)))

so a participant below min_phecode_count is neither case nor control.

Note one deliberate divergence: PheTK's `count` is COUNT(*) over mapped rows, so
two codes in a single visit satisfy min_phecode_count=2. Ours requires two
distinct dates, which is stricter and is what --case-rule two-dates says.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes

# a1/a2 have two dated events; b1/b2 exactly one; c1 none.
COHORT = [["a1", "Female"], ["a2", "Female"], ["b1", "Female"], ["b2", "Female"], ["c1", "Female"]]
EVENTS = ([[p, "123.4", "ICD9CM", "2020-01-01"] for p in ("a1", "a2")]
          + [[p, "123.4", "ICD9CM", "2021-06-01"] for p in ("a1", "a2")]
          + [[p, "123.4", "ICD9CM", "2020-01-01"] for p in ("b1", "b2")])


def _run(tmp_path: Path, release: Path, case_rule: str) -> Path:
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / f"run_{case_rule}"
    write_csv(cohort, ["person_id", "sex"], COHORT)
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"], EVENTS)
    map_phecodes(release, cohort, events, output, case_rule=case_rule, min_cases=1, min_controls=0)
    return output


def _counts(run: Path, phecode: str) -> tuple:
    return duckdb.sql(
        f"SELECT case_count, control_count_before_exclusions, subthreshold_control_count,"
        f" control_count_after_exclusions"
        f" FROM read_parquet('{run / 'phecode_counts.parquet'}') WHERE phecode = '{phecode}'").fetchone()


def _matrix(run: Path, phecode: str) -> dict[str, int | None]:
    return dict(duckdb.sql(
        f'SELECT person_id, "{phecode}" FROM read_parquet(\'{run / "phenotype_matrix.parquet"}\')').fetchall())


def test_single_date_carriers_are_not_controls_under_two_dates(tmp_path: Path, full_release: Path) -> None:
    """b1 and b2 carry the code once: neither cases nor evidence against the phenotype."""
    run = _run(tmp_path, full_release, "two-dates")
    cases, before, subthreshold, after = _counts(run, "GU_001")
    assert cases == 2                 # a1, a2
    assert subthreshold == 2          # b1, b2
    assert after == 1                 # only c1 is a clean control
    assert before == 3                # 5 evaluable people minus 2 cases

    assert _matrix(run, "GU_001") == {"a1": 1, "a2": 1, "b1": None, "b2": None, "c1": 0}


def test_any_event_rule_is_unchanged(tmp_path: Path, full_release: Path) -> None:
    """The default rule must be untouched: one event already makes a case.

    Without this, the change could silently alter every existing run rather than
    only two-dates runs.
    """
    run = _run(tmp_path, full_release, "any-event")
    cases, before, subthreshold, after = _counts(run, "GU_001")
    assert cases == 4                 # a1, a2, b1, b2 -- one event suffices
    assert subthreshold == 0, "any-event can have no sub-threshold carriers by construction"
    assert after == 1 == before

    assert _matrix(run, "GU_001") == {"a1": 1, "a2": 1, "b1": 1, "b2": 1, "c1": 0}


@pytest.mark.parametrize("case_rule", ["any-event", "two-dates"])
def test_counts_still_reconcile_with_the_matrix(tmp_path: Path, full_release: Path, case_rule: str) -> None:
    """The invariant that caught the original sex bug must survive this change."""
    run = _run(tmp_path, full_release, case_rule)
    columns = [r[0] for r in duckdb.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{run / 'phenotype_matrix.parquet'}')").fetchall()]
    for phecode in [c for c in columns if c != "person_id"]:
        cases, _, _, after = _counts(run, phecode)
        values = list(_matrix(run, phecode).values())
        assert cases == sum(1 for v in values if v == 1), f"{phecode}: case_count != matrix 1s"
        assert after == sum(1 for v in values if v == 0), f"{phecode}: control count != matrix 0s"


def test_a_case_is_never_downgraded_by_the_subthreshold_rule(tmp_path: Path, full_release: Path) -> None:
    """Someone qualifying as a case must not also be removed as sub-threshold."""
    run = _run(tmp_path, full_release, "two-dates")
    overlap = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{run / 'person_phecodes.parquet'}') p"
        f" WHERE p.person_id IN ('a1','a2') AND p.phecode = 'GU_001'").fetchone()[0]
    assert overlap == 2
    assert _matrix(run, "GU_001")["a1"] == 1


def test_two_dates_means_distinct_dates_not_two_events(tmp_path: Path) -> None:
    """The rule is named for the semantics no test could see.

    Dropping DISTINCT left the whole suite green, yet it is the documented
    divergence from PheTK: PheTK counts mapped rows, so two codes in one visit
    make a case; we require two calendar dates. A person with two events on the
    SAME day is the only input that separates those two readings.
    """
    from phecodex_mapper.vocabulary import build_vocabulary
    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "123.4", "ICD9CM"]])
    info = tmp_path / "i.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"], [["CV_003", "Both", "X", "Y"]])
    release = tmp_path / "rel"
    build_vocabulary(source, info, release, None)

    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"],
              [["same_day", "Female"], ["two_days", "Female"], ["ctrl", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary", "event_date"], [
        ["same_day", "123.4", "ICD9CM", "2020-01-01"],   # two events, ONE date
        ["same_day", "123.4", "ICD9CM", "2020-01-01"],
        ["two_days", "123.4", "ICD9CM", "2020-01-01"],   # two events, TWO dates
        ["two_days", "123.4", "ICD9CM", "2020-06-01"],
    ])
    out = tmp_path / "run"
    map_phecodes(release, cohort, events, out, case_rule="two-dates", min_cases=1, min_controls=1)
    cases = {r[0] for r in duckdb.sql(
        f"SELECT person_id FROM read_parquet('{out / 'person_phecodes.parquet'}')").fetchall()}
    assert "two_days" in cases
    assert "same_day" not in cases, "two events on one date satisfied a rule that says two dates"


def test_two_dates_gives_the_same_answer_in_every_machine_timezone(tmp_path: Path) -> None:
    """Property 7. A tz-aware event_date resolved through the machine's zone made a
    person a case at one site and non-evaluable at another, on byte-identical inputs.

    pyarrow writes TIMESTAMP WITH TIME ZONE for any tz-aware column, and DuckDB casts
    it to DATE through its TimeZone setting, which defaults to the local zone. Two
    events either side of midnight UTC are two dates in London and one in Los Angeles.
    """
    import os
    import subprocess
    import sys
    import pyarrow as pa
    import pyarrow.parquet as pq
    from phecodex_mapper.vocabulary import build_vocabulary

    source = tmp_path / "mtz.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "123.4", "ICD9CM"]])
    info = tmp_path / "itz.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"], [["CV_003", "Both", "X", "Y"]])
    release = tmp_path / "reltz"
    build_vocabulary(source, info, release, None)
    cohort = tmp_path / "ctz.csv"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Female"]])
    # 2020-01-01T23:00Z and 2020-01-02T01:00Z: two UTC dates, one Los Angeles date.
    events = tmp_path / "etz.parquet"
    pq.write_table(pa.table({
        "person_id": ["p1", "p1", "p2", "p2"],
        "code": ["123.4"] * 4,
        "vocabulary": ["ICD9CM"] * 4,
        "event_date": pa.array([1577919600_000000, 1577926800_000000,
                                1577919600_000000, 1580598000_000000],
                               type=pa.timestamp("us", tz="UTC")),
    }), events)

    child = tmp_path / "child.py"
    child.write_text(
        "import sys, duckdb\n"
        "from pathlib import Path\n"
        "from phecodex_mapper.mapper import map_phecodes\n"
        "release, cohort, events, out = (Path(a) for a in sys.argv[1:5])\n"
        "map_phecodes(release, cohort, events, out, case_rule='two-dates',"
        " min_cases=1, min_controls=1)\n"
        "rows = duckdb.sql(\"SELECT phecode, case_count FROM read_parquet('\""
        " + str(out / 'phecode_counts.parquet') + \"')\").fetchall()\n"
        "print(rows)\n"
    )
    results = {}
    for zone in ("UTC", "America/Los_Angeles", "Australia/Sydney"):
        env = {**os.environ, "TZ": zone,
               "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        out = tmp_path / f"o_{zone.replace('/', '_')}"
        proc = subprocess.run([sys.executable, str(child), str(release), str(cohort),
                               str(events), str(out)],
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        results[zone] = proc.stdout.strip()
    assert len(set(results.values())) == 1, f"case set depends on the machine timezone: {results}"
    assert "2" in results["UTC"], f"fixture no longer produces two distinct UTC dates: {results}"
