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
