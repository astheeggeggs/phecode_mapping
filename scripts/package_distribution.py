#!/usr/bin/env python3
"""Create a clean analyst distribution bundle around a built release."""
from __future__ import annotations

import argparse
import hashlib
import tarfile
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--release", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    raise SystemExit(f"Output already exists: {args.output}")
if not (args.release / "manifest.json").is_file():
    raise SystemExit(f"Release is missing manifest.json: {args.release}")
if any((args.release / name).exists() for name in ("snomed_map.csv", "snomed_map.parquet")):
    raise SystemExit("This analyst distribution excludes SNOMED-derived outputs; provide an ICD-only release")

root = Path(__file__).resolve().parents[1]
# Every script README.md or ANALYST_GUIDE.md tells an analyst to run must be here.
# tests/test_distribution.py derives that list from the docs themselves, so adding a
# documented command without bundling its script fails the suite.
files = [root / "LICENSE", root / "README.md", root / "ANALYST_GUIDE.md", root / "attrition.svg", root / "pyproject.toml", root / "requirements-lock.txt", root / "examples/cohort.csv", root / "examples/events.csv", root / "scripts/verify_release.py", root / "scripts/package_distribution.py",
         root / "scripts/prepare_ukb_for_mapping.R", root / "scripts/plot_phecode_attrition.py", root / "scripts/check_prevalence.py", root / "scripts/check_deidentification.py", root / "scripts/reconcile_attrition.py", root / "scripts/deidentify_ukb_for_testing.R", root / "containers/Dockerfile", root / ".dockerignore", root / "containers/Singularity.def"]
files += list((root / "src").rglob("*.py"))
files += list((root / "src/phecodex_mapper/data").glob("*.csv"))
args.output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(args.output, "w:gz") as archive:
    for path in files:
        archive.add(path, arcname=Path("phecodex-distribution") / path.relative_to(root))
    archive.add(args.release, arcname=Path("phecodex-distribution") / "release")
checksum_path = args.output.with_name(args.output.name + ".sha256")
checksum_path.write_text(f"{sha256(args.output)}  {args.output.name}\n")
print(f"bundle: {args.output}")
print(f"sha256: {checksum_path}")
