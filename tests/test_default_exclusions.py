"""Regression tests for the bundled default --exclude-phenotypes policy.

The bundled file previously had unquoted commas in its `reason` column, which made
DuckDB's CSV sniffer collapse it to a single column and aborted every documented
`phecodex-map run`. Nothing caught it: the exclusion tests all wrote their own
comma-free CSVs and called map_phecodes() directly, so neither the shipped data
file nor cli.py's DEFAULT_EXCLUSIONS wiring was ever exercised.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

from conftest import write_csv
from phecodex_mapper import cli
from phecodex_mapper.io import connect, relation_for


def test_bundled_default_exclusions_file_parses_as_a_real_csv() -> None:
    """The shipped file must survive DuckDB's sniffer with its columns intact.

    Asserting on the parsed columns rather than on the file's bytes is deliberate:
    the failure mode was a *parser* disagreement, not a missing field.
    """
    con = connect()
    source = relation_for(cli.DEFAULT_EXCLUSIONS)
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]
    assert columns == ["match_type", "match_value", "reason"]

    rules = con.execute(f"SELECT match_type, match_value FROM {source} ORDER BY match_type, match_value").fetchall()
    assert rules == [
        ("category", "Infections"),
        ("category", "Neonatal"),
        ("category", "Symptoms"),
        ("phecode", "PP_P001"),
        ("phecode", "PP_P002"),
        ("phecode", "PP_P003"),
    ]
    # Every reason must be non-empty: an unquoted comma would previously have
    # split one across columns rather than leaving it blank, but a blank match_value
    # would put a NULL in excluded_phecodes and make `NOT IN` drop every phecode.
    blank = con.execute(
        f"SELECT count(*) FROM {source} WHERE reason IS NULL OR trim(reason) = ''"
        f" OR match_value IS NULL OR trim(match_value) = ''"
    ).fetchone()[0]
    assert blank == 0


def test_cli_run_applies_the_bundled_default_exclusions(tmp_path: Path, full_release: Path, monkeypatch) -> None:
    """`run` with no --exclude-phenotypes must load and apply the bundled policy.

    This is the only test that goes through cli.main()'s `run` branch, so it is
    what pins the DEFAULT_EXCLUSIONS wiring at cli.py:128.
    """
    cohort, events, output = tmp_path / "cohort.csv", tmp_path / "events.csv", tmp_path / "run"
    write_csv(cohort, ["person_id", "sex"], [["p1", "Female"], ["p2", "Male"], ["p3", "Female"]])
    write_csv(events, ["person_id", "code", "vocabulary"], [
        ["p1", "A01.1", "ICD10CM"],  # CV_003, unrestricted -- must survive
        ["p2", "A02.0", "ICD10CM"],  # SS_004, category Symptoms -- must be excluded
        ["p3", "123.4", "ICD9CM"],   # GU_001, Female-only -- must survive
    ])
    monkeypatch.setattr(sys, "argv", [
        "phecodex-map", "run",
        "--release", str(full_release),
        "--cohort", str(cohort),
        "--events", str(events),
        "--output", str(output),
        "--min-cases", "1", "--min-controls", "1",
    ])
    cli.main()

    phecodes = {r[0] for r in duckdb.sql(
        f"SELECT phecode FROM read_parquet('{output / 'phecode_counts.parquet'}')").fetchall()}
    assert "SS_004" not in phecodes, "bundled 'Symptoms' category rule was not applied"
    assert {"CV_003", "GU_001"} <= phecodes

    import json
    audit = json.loads((output / "audit.json").read_text())
    assert audit["exclude_phenotypes"]["file"] == str(cli.DEFAULT_EXCLUSIONS)
    # 1 = SS_004, resolved from the 'Symptoms' category rule, and the only phecode
    # in this fixture release that any rule actually names. This asserted 4 until
    # the audit pointed out that counting the three PP_P00x rules the release does
    # not contain reports phenotypes as dropped that were never dropped -- worse
    # than silence, because it reads as confirmation the rules worked. The earlier
    # comment here rationalised that as "counting identifiers, not phecodes"; the
    # field is named phecodes_excluded and an analyst reads it as phecodes.
    assert audit["exclude_phenotypes"]["phecodes_excluded"] == 1
    assert audit["exclude_phenotypes"]["unmatched_phecode_rules"] == ["PP_P001", "PP_P002", "PP_P003"]
    assert set(audit["exclude_phenotypes"]["unmatched_category_rules"]) == {"Infections", "Neonatal"}
