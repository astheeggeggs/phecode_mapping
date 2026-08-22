#!/usr/bin/env python3
"""Verify a PhecodeX release directory or release archive before use."""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(directory: Path, hierarchy: bool) -> dict:
    required = {"manifest.json", "icd_map.parquet", "phecode_info.parquet"}
    if hierarchy:
        required.add("icd_hierarchy.parquet")
    missing = sorted(name for name in required if not (directory / name).is_file())
    if missing:
        raise SystemExit(f"Release is incomplete; missing: {missing}")
    manifest = json.loads((directory / "manifest.json").read_text())
    if not manifest.get("tool_version") or not manifest.get("counts"):
        raise SystemExit("manifest.json is missing required release metadata")
    if hierarchy and not manifest.get("icd_hierarchy"):
        raise SystemExit("Hierarchy release is missing icd_hierarchy manifest metadata")
    return {"release": str(directory), "manifest_sha256": sha256(directory / "manifest.json"), "hierarchy_required": hierarchy, "status": "ok"}


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--release", type=Path, required=True)
parser.add_argument("--hierarchy-aware", action="store_true")
parser.add_argument("--archive", type=Path, help="Optional .tar.gz archive whose .sha256 sidecar should be checked")
args = parser.parse_args()
if args.archive:
    sidecar = args.archive.with_name(args.archive.name + ".sha256")
    if not sidecar.exists() or sha256(args.archive) != sidecar.read_text().split()[0]:
        raise SystemExit(f"Archive checksum failed: {args.archive}")
    with tarfile.open(args.archive, "r:gz") as archive:
        unsafe = [m.name for m in archive.getmembers() if Path(m.name).is_absolute() or ".." in Path(m.name).parts]
        if unsafe:
            raise SystemExit(f"Archive contains unsafe paths: {unsafe[:5]}")
result = verify(args.release, args.hierarchy_aware)
print(json.dumps(result, indent=2))
