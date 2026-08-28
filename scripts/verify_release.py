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


def verify(directory: Path) -> dict:
    required = {"manifest.json", "icd_map.parquet", "phecode_info.parquet"}
    missing = sorted(name for name in required if not (directory / name).is_file())
    if missing:
        raise SystemExit(f"Release is incomplete; missing: {missing}")
    manifest = json.loads((directory / "manifest.json").read_text())
    if not manifest.get("tool_version") or not manifest.get("counts"):
        raise SystemExit("manifest.json is missing required release metadata")

    # The point of this script. Checking that filenames exist told an analyst nothing:
    # a release whose icd_map.parquet had been replaced with arbitrary bytes still
    # reported "ok". Every shipped file is now checksummed at build time and re-checked
    # here against the digest recorded in the manifest.
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise SystemExit(
            "manifest.json records no artifact checksums, so this release's contents cannot be "
            "verified. It predates checksummed releases -- ask for a release rebuilt with a "
            "current build-vocabulary before using it.")

    problems = []
    for name, meta in sorted(artifacts.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: recorded in the manifest but missing from the release")
            continue
        actual_bytes = path.stat().st_size
        expected_bytes = meta.get("bytes")
        if expected_bytes is not None and actual_bytes != expected_bytes:
            problems.append(f"{name}: size is {actual_bytes} bytes, manifest records {expected_bytes}")
            continue
        actual = sha256(path)
        if actual != meta.get("sha256"):
            problems.append(f"{name}: sha256 {actual[:16]}... does not match the manifest")
    if problems:
        raise SystemExit("Release verification FAILED:\n  " + "\n  ".join(problems))

    for name in sorted(required):
        if name != "manifest.json" and name not in artifacts:
            raise SystemExit(f"{name} is present but carries no manifest checksum; the release is inconsistent")

    # An unrecorded file was reported and then passed. That is not cosmetic: map-phecodes
    # loads snomed_map.parquet if it merely EXISTS, so dropping one into an --icd-only
    # release silently restores SNOMED mappings the release deliberately withheld, and
    # verification blessed it. A release is what the manifest says it is; anything else
    # in the directory means the release is not the one that was built.
    # Files map-phecodes reads by NAME. An inert extra file (notes, .DS_Store) is
    # untidy; one of these is a behaviour change, because the mapper picks them up by
    # presence -- dropping snomed_map.parquet into an --icd-only release silently
    # restores the SNOMED mappings it was built to withhold.
    CONSUMED = {"icd_map.parquet", "phecode_info.parquet", "snomed_map.parquet"}
    smuggled = sorted(name for name in CONSUMED
                      if name not in artifacts and (directory / name).is_file())
    if smuggled:
        raise SystemExit(
            "Release verification FAILED: " + ", ".join(smuggled) + " present but not recorded "
            "in the manifest. map-phecodes reads these by name, so this release would produce "
            "phecodes it does not claim to contain. Remove them, or obtain a release built with "
            "them.")

    unexpected = sorted(p.name for p in directory.iterdir()
                        if p.is_file() and p.name != "manifest.json" and p.name not in artifacts)
    return {"release": str(directory), "manifest_sha256": sha256(directory / "manifest.json"),
            "artifacts_verified": len(artifacts),
            "unexpected_files": unexpected, "status": "ok"}


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--release", type=Path, required=True)
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
result = verify(args.release)
print(json.dumps(result, indent=2))
