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
