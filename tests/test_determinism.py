"""Property 7: two sites running identical inputs must get identical outputs.

The whole federated design assumes sites cannot disagree, so anything that makes
two runs differ is a defect rather than an inconvenience. Two distinct causes were
found and are guarded here.

Row order. `preserve_insertion_order` is off and DuckDB writes in parallel, so an
unordered COPY produced a different file on every run from the same inputs -- and
phenotype_matrix.csv.gz and .parquet from the SAME run could disagree with each
other. The ORDER BY used when a table is built does not survive the COPY.

Wall clock. openpyxl stamps the current time into docProps and into every zip
member mtime, so eligible_phecodes.xlsx changed on every run regardless of content.
The pinning written for the release builder was never applied to the run outputs.
"""
from __future__ import annotations

import gzip
import hashlib
import random
from pathlib import Path

import duckdb

from conftest import write_csv
from phecodex_mapper.mapper import map_phecodes
from phecodex_mapper.vocabulary import build_vocabulary

RUN_OUTPUTS = ["phecode_counts.csv", "phecode_counts.parquet", "person_phecodes.parquet",
               "unmapped_events.csv", "phenotype_matrix.csv.gz", "phenotype_matrix.parquet",
               "eligible_phecodes.xlsx"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path):
    source = tmp_path / "m.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"],
              [["CV_003", "I10", "ICD10CM"], ["GU_001", "A01.1", "ICD10CM"],
               ["ID_052", "123.4", "ICD9CM"]])
    info = tmp_path / "i.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "Hypertension", "Cardiovascular"],
               ["GU_001", "Female", "Endometriosis", "Genitourinary"],
               ["ID_052", "Both", "Sepsis", "Infections"]])
    release = tmp_path / "rel"
    build_vocabulary(source, info, release, None)

    # Enough people that DuckDB actually parallelises the writes; a handful of rows
    # can come out ordered by luck and prove nothing.
    rng = random.Random(11)
    people = [[f"p{i:05d}", rng.choice(["Male", "Female"])] for i in range(2000)]
    # Shuffled on purpose. Written in id order, an UNORDERED write still comes out
    # sorted at this size and every assertion below passes by luck -- the matrix
    # ordering mutant survived exactly that way. Shuffling makes insertion order
    # differ from sorted order, so a missing ORDER BY is visible without needing the
    # ~123k rows it takes for DuckDB to parallelise the write.
    rng.shuffle(people)
    cohort, events = tmp_path / "c.csv", tmp_path / "e.csv"
    write_csv(cohort, ["person_id", "sex"], people)
    write_csv(events, ["person_id", "code", "vocabulary"],
              [[p[0], rng.choice(["I10", "A01.1", "123.4", "ZZ9.9"]),
                rng.choice(["ICD10CM", "ICD9CM"])] for p in people for _ in range(3)])
    return release, cohort, events


def test_two_identical_runs_produce_identical_files(tmp_path: Path) -> None:
    """Every run output, not just the matrix."""
    release, cohort, events = _fixture(tmp_path)
    digests = []
    for name in ("run_a", "run_b"):
        out = tmp_path / name
        map_phecodes(release, cohort, events, out, min_cases=5, min_controls=5)
        digests.append({f: _digest(out / f) for f in RUN_OUTPUTS})
    first, second = digests
    differing = sorted(f for f in first if first[f] != second[f])
    assert differing == [], f"not reproducible across runs: {differing}"


def test_the_two_matrix_formats_agree_on_row_order(tmp_path: Path) -> None:
    """A site comparing the CSV while another reads the Parquet must see the same rows.

    Both are also required to be sorted by person_id, which is what the code says it
    writes -- without that, two runs can agree with each other by luck and still not be
    the order anyone expects.
    """
    release, cohort, events = _fixture(tmp_path)
    out = tmp_path / "run_c"
    map_phecodes(release, cohort, events, out, min_cases=5, min_controls=5)

    parquet = [r[0] for r in duckdb.sql(
        f"SELECT person_id FROM read_parquet('{out / 'phenotype_matrix.parquet'}')").fetchall()]
    csv_rows = gzip.open(out / "phenotype_matrix.csv.gz", "rt").read().splitlines()
    csv_ids = [line.split(",")[0] for line in csv_rows[1:]]
    assert parquet == csv_ids, "the two matrix formats are in different row orders"
    assert csv_ids == sorted(csv_ids), "the matrix is not ordered by person_id"


def test_the_workbook_carries_no_wall_clock(tmp_path: Path) -> None:
    """Digest equality alone can pass by luck: timestamps have one-second resolution."""
    import zipfile
    release, cohort, events = _fixture(tmp_path)
    out = tmp_path / "run_d"
    map_phecodes(release, cohort, events, out, min_cases=5, min_controls=5)
    with zipfile.ZipFile(out / "eligible_phecodes.xlsx") as archive:
        stamps = {item.date_time for item in archive.infolist()}
        core = archive.read("docProps/core.xml").decode()
    assert stamps == {(2000, 1, 1, 0, 0, 0)}, f"zip member mtimes not pinned: {sorted(stamps)}"
    assert "2000-01-01T00:00:00Z" in core and core.count("2000-01-01T00:00:00Z") == 2


def test_the_matrix_is_ordered_at_a_scale_where_the_write_parallelises(tmp_path: Path) -> None:
    """The matrix ORDER BY is only reachable at scale, so this fixture pays for it.

    Below roughly 100k people DuckDB writes the matrix single-threaded and the rows
    come out sorted whether or not the COPY says so -- deleting the ORDER BY leaves
    the smaller fixtures above completely green. At 150k the write parallelises and
    the CSV comes out unsorted and disagreeing with the Parquet from the same run.
    Measured: with the ORDER BY both formats are sorted and identical; without it,
    parquet sorted True / csv sorted False / formats agree False.
    """
    source = tmp_path / "ms.csv"
    write_csv(source, ["phecode", "ICD", "vocabulary_id"], [["CV_003", "I10", "ICD10CM"]])
    info = tmp_path / "is.csv"
    write_csv(info, ["phecode", "sex", "phecode_string", "category"],
              [["CV_003", "Both", "Hypertension", "Cardiovascular"]])
    release = tmp_path / "rels"
    build_vocabulary(source, info, release, None)

    rng = random.Random(3)
    people = [[f"p{i:07d}", rng.choice(["Male", "Female"])] for i in range(150_000)]
    rng.shuffle(people)
    cohort, events = tmp_path / "cs.csv", tmp_path / "es.csv"
    write_csv(cohort, ["person_id", "sex"], people)
    write_csv(events, ["person_id", "code", "vocabulary"],
              [[p[0], "I10", "ICD10CM"] for p in people])

    out = tmp_path / "scale"
    map_phecodes(release, cohort, events, out, min_cases=5, min_controls=5)
    parquet = [r[0] for r in duckdb.sql(
        f"SELECT person_id FROM read_parquet('{out / 'phenotype_matrix.parquet'}')").fetchall()]
    csv_ids = [line.split(",")[0] for line in
               gzip.open(out / "phenotype_matrix.csv.gz", "rt").read().splitlines()[1:]]
    assert csv_ids == sorted(csv_ids), "matrix CSV is unordered once the write parallelises"
    assert parquet == csv_ids, "the two matrix formats disagree at scale"
