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


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    return con
