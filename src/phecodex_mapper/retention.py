"""The single definition of which phecodes are analysable, and for whom.

Three things were previously re-implemented once per consumer: which phecodes a
release restricts by sex, the evaluable denominator that restriction implies, and
the case/control retention rule. `mapper` expressed them in SQL,
`scripts/plot_phecode_attrition.py` in Python and `scripts/reconcile_attrition.py`
in a third, differently-spelled SQL. A whole script existed to detect the resulting
drift -- which is a way of maintaining three copies, not of having one answer.

Everything here is that one answer, in whichever form the caller needs. The SQL
helpers emit fragments rather than run queries, following the existing `EVALUABLE`
idiom: the mapper composes them into large statements, so they cannot be functions
that execute.

What is deliberately NOT here: the control-exclusion and sub-threshold removals.
Those are person-level facts a run computes and only the run holds, so a consumer
working from `person_phecodes.parquet` alone cannot reproduce them. See
`controls_from_evaluable` for the one place that gap is named.
"""
from __future__ import annotations

MALE, FEMALE = "MALE", "FEMALE"


def evaluable_predicate(*, phecode_sex: str = "ps", cohort: str = "c") -> str:
    """SQL: is this person evaluable for this phecode?

    A person counts toward a phecode only if the phecode carries no sex restriction
    or their cohort sex matches it. Unknown sex is never evaluable for a restricted
    phecode -- `c.sex` is neither MALE nor FEMALE, so the equality is false rather
    than UNKNOWN-and-then-true.
    """
    return f"({phecode_sex}.restrict_sex IS NULL OR {cohort}.sex = {phecode_sex}.restrict_sex)"


def eligible_count_sql(restrict_sex: str, *, n_male: str, n_female: str, n_all: str) -> str:
    """SQL: the evaluable denominator for a phecode, given whole-cohort tallies.

    `restrict_sex` must already be canonical upper-case (NULL, 'MALE' or 'FEMALE') --
    use `load_phecode_restrictions`, or the mapper's `phecode_sex` table, so the CASE
    cannot silently fall through to the unrestricted branch on 'Male'.
    """
    return f"CASE {restrict_sex} WHEN '{MALE}' THEN {n_male} WHEN '{FEMALE}' THEN {n_female} ELSE {n_all} END"


def eligible_count(restrict_sex: str | None, *, n_male: int, n_female: int, n_all: int) -> int:
    """Python: the same rule as `eligible_count_sql`, for row-at-a-time consumers."""
    return {MALE: n_male, FEMALE: n_female}.get(restrict_sex, n_all)


def retained_sql(case_count: str, control_count: str, *, min_cases: int, min_controls: int) -> str:
    """SQL: the retention rule -- both thresholds, against the evaluable denominator."""
    return f"{case_count} >= {int(min_cases)} AND {control_count} >= {int(min_controls)}"


def is_retained(case_count: int, control_count: int, *, min_cases: int, min_controls: int) -> bool:
    """Python: the same rule as `retained_sql`."""
    return case_count >= min_cases and control_count >= min_controls


def controls_from_evaluable(evaluable: int, case_count: int) -> int:
    """The control count a consumer can compute from cases and a denominator alone.

    This is an UPPER BOUND on the run's `control_count_after_exclusions`, and equals
    it only when the run removed nobody from the control pool. The shortfall is
    exactly the run's `control_count_before_exclusions - control_count_after_exclusions`
    -- sub-threshold carriers under `--case-rule two-dates`, plus non-cases named by
    `--control-exclusions`. Measured on a fixture: a control-exclusion rule covering
    15 non-cases moved a phecode's controls from 30 (here) to 15 (the run), which is
    the difference between retained and dropped at --min-controls 20.

    Callers that cannot see those removals must say so rather than present this as
    the run's answer.
    """
    return evaluable - case_count


def restriction_query_sql(relation: str) -> str:
    """SQL: (phecode, restrict_sex) for the phecodes a release genuinely restricts.

    The third rule that was re-implemented per consumer. The mapper filled its
    `phecode_sex` table with `upper(trim(sex))` while this module read the same
    column as `upper(trim(CAST(sex AS VARCHAR)))`, so a release whose `sex` column
    is not VARCHAR -- a Parquet ENUM, say -- could be canonicalised by one caller
    and not the other, and the two would then disagree about which phecodes are
    restricted while both claimed to be applying "the" rule. One spelling, here.

    Only genuinely restricted phecodes come back: 'Both', blank, NULL and
    unrecognised values are filtered out, so absence means unrestricted and
    `eligible_count`'s default branch is reached by a missing row rather than by a
    value the CASE failed to match.
    """
    canonical = "upper(trim(CAST(sex AS VARCHAR)))"
    return (f"SELECT phecode, {canonical} AS restrict_sex FROM {relation} "
            f"WHERE {canonical} IN ('{MALE}', '{FEMALE}')")


def load_phecode_restrictions(con, phecode_info_relation: str) -> dict[str, str]:
    """{phecode: 'MALE'|'FEMALE'}, for Python consumers of `restriction_query_sql`."""
    columns = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {phecode_info_relation}").fetchall()}
    if not {"phecode", "sex"} <= columns:
        return {}
    return dict(con.execute(restriction_query_sql(phecode_info_relation)).fetchall())
