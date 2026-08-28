from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb


def quote(path: Path | str) -> str:
    return str(path).replace("'", "''")


def relation_for(path: Path | str) -> str:
    path = Path(path)
    if path.suffix.lower() in {".parquet", ".pq"}:
        return f"read_parquet('{quote(path)}')"
    return f"read_csv_auto('{quote(path)}', header=true, all_varchar=true)"


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_release_metadata(output: Path, payload: dict) -> None:
    (output / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# Every site must resolve the same instant to the same calendar date. DuckDB defaults
# TimeZone to the machine's local zone, and casting a TIMESTAMP WITH TIME ZONE to DATE
# goes through it -- so an events file whose event_date carries a UTC offset (which is
# what pyarrow/pandas write for a tz-aware column, and what DuckDB reads for any Parquet
# timestamp with isAdjustedToUTC=true) yielded a DIFFERENT case set under --case-rule
# two-dates at two sites running byte-identical inputs. Two events either side of
# midnight UTC are two distinct dates in London and one in Los Angeles, so a person was
# a case at one site and non-evaluable at the other, with no warning and no unparseable
# dates reported. Pinning UTC makes the answer a property of the data rather than of the
# machine; audit.json records it so two sites can prove they agreed.
ANALYSIS_TIMEZONE = "UTC"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET TimeZone = '{ANALYSIS_TIMEZONE}'")
    return con
