from __future__ import annotations

import csv
from pathlib import Path

import pytest

from phecodex_mapper.vocabulary import build_vocabulary


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


@pytest.fixture
def release(tmp_path: Path) -> Path:
    source = tmp_path / "official.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["AA_1", "123.4", "ICD9CM"],
        ["AA_1", "123.45", "ICD9CM"],
        ["AA_1.1", "123.45", "ICD9CM"],
        ["BB_2", "A01.1", "ICD10CM"],
    ])
    destination = tmp_path / "release"
    build_vocabulary(source, None, destination)
    return destination


@pytest.fixture
def full_release(tmp_path: Path) -> Path:
    """A release carrying the part `release` omits: phecode_info with sex and category.

    The minimal fixture cannot reach the documented workflow at all -- without
    phecode_info there is no sex restriction and no category to exclude on. Tests
    that need the real analyst path use this one.
    """
    source = tmp_path / "official_full.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [
        ["GU_001", "123.4", "ICD9CM"],
        ["GU_002", "125.0", "ICD9CM"],
        ["CV_003", "A01.1", "ICD10CM"],
        ["SS_004", "A02.0", "ICD10CM"],
    ])
    info = tmp_path / "info_full.csv"
    # 'Symptoms' matches src/phecodex_mapper/data/recommended_exclusions.csv. Category
    # rules are in fact matched case-insensitively (both sides go through upper() in
    # _load_excluded_phecodes), so the exact spelling is not what makes this work --
    # measured: 'symptoms' excludes SS_004 just as 'Symptoms' does. It is *phecode*
    # rules that are case-sensitive: 'gu_001' matches nothing and is reported in
    # unmatched_phecode_rules. Both are pinned -- see
    # test_exclusion_matching.test_exclude_phenotypes_category_matches_regardless_of_case
    # and test_misleading_output.py's unmatched_phecode_rules assertion.
    write_csv(info, ["phecode", "sex", "category", "phecode_string"], [
        ["GU_001", "Female", "Genitourinary", "Female-only trait"],
        ["GU_002", "Male", "Genitourinary", "Male-only trait"],
        ["CV_003", "Both", "Cardiovascular", "Unrestricted trait"],
        ["SS_004", "Both", "Symptoms", "Non-specific symptom"],
    ])
    destination = tmp_path / "release_full"
    build_vocabulary(source, info, destination)
    return destination
