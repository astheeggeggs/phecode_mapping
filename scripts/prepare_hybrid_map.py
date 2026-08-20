#!/usr/bin/env python3
"""Prepare a PhecodeX 1.1 CM + 1.0 WHO hybrid map for this mapper."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def read(path: Path, source: str) -> set[tuple[str, str, str]]:
    with path.open(newline="", encoding="latin1") as f:
        rows = list(csv.DictReader(f))
    required = {"phecode", "vocabulary_id"} | ({"icd"} if source == "CM" else {"ICD"})
    missing = required - set(rows[0] if rows else [])
    if missing: raise ValueError(f"{source} map missing columns: {sorted(missing)}")
    result = set()
    for row in rows:
        code = row["icd"] if source == "CM" else row["ICD"]
        vocab = row["vocabulary_id"].strip().upper()
        if source == "CM" and vocab not in {"ICD9CM", "ICD10CM"}:
            raise ValueError(f"Unexpected CM vocabulary: {vocab}")
        if source == "WHO" and vocab not in {"ICD", "ICD10"}:
            raise ValueError(f"Unexpected WHO vocabulary: {vocab}")
        result.add((row["phecode"], code, vocab))
    return result


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cm-map", type=Path, required=True)
parser.add_argument("--who-map", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists() or args.output.with_name(args.output.stem + "_manifest.json").exists():
    raise SystemExit("Output already exists; choose a new path")
rows = sorted(read(args.cm_map, "CM") | read(args.who_map, "WHO"), key=lambda x: (x[2], x[1], x[0]))
args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f); writer.writerow(["phecode", "ICD", "vocabulary_id"]); writer.writerows(rows)
manifest = {"format": "phecodex hybrid map", "rows": len(rows), "sources": {
    "ICD9CM": {"version": "PhecodeX 1.1 CM", "path": str(args.cm_map), "sha256": digest(args.cm_map)},
    "ICD10CM": {"version": "PhecodeX 1.1 CM plus PhecodeX 1.0 WHO", "cm_path": str(args.cm_map), "cm_sha256": digest(args.cm_map), "who_path": str(args.who_map), "who_sha256": digest(args.who_map)},
}}
manifest_path = args.output.with_name(args.output.stem + "_manifest.json")
manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
print(f"map: {args.output}\nmanifest: {manifest_path}\nrows: {len(rows)}")
