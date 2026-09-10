#!/usr/bin/env python3
"""Create a clean analyst distribution bundle around a built release.

By default this refuses a release carrying SNOMED tables, because the bundle it
produces is the one published as a GitHub Release and Athena-derived content is
separately licensed. --allow-snomed lifts that for a bundle you hand to one named,
Athena-licensed site directly. It is deliberately not the default and deliberately
not silent: the bundle it writes must not be published.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
from pathlib import Path

NOTICE_NAME = "SNOMED_REDISTRIBUTION_NOTICE.txt"
NOTICE = """This bundle contains SNOMED/Athena-derived mapping tables (release/snomed_map.csv
and release/snomed_map.parquet), built from an OMOP/Athena vocabulary extract.

That content is separately licensed and is NOT covered by the MIT licence in
LICENSE, which applies to the code only. This bundle was produced with
--allow-snomed for a direct transfer to a site holding its own Athena licence.

Do not publish it and do not pass it on. If another site needs SNOMED mapping,
they should build the release themselves from their own Athena extract -- builds
are byte-reproducible, so an independently built release is identical and carries
no redistribution question at all.

The ICD-only bundle, which excludes these tables, is the one published for
general use.
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--release", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--allow-snomed", action="store_true",
                    help="Bundle a release carrying SNOMED tables. For a direct transfer to an "
                         "Athena-licensed site only -- the result must not be published. A "
                         "redistribution notice is added to the bundle.")
args = parser.parse_args()
if args.output.exists():
    raise SystemExit(f"Output already exists: {args.output}")
if not (args.release / "manifest.json").is_file():
    raise SystemExit(f"Release is missing manifest.json: {args.release}")
carries_snomed = any((args.release / name).exists() for name in ("snomed_map.csv", "snomed_map.parquet"))
if carries_snomed and not args.allow_snomed:
    raise SystemExit("This analyst distribution excludes SNOMED-derived outputs; provide an ICD-only release "
                     "(or pass --allow-snomed for a direct transfer to an Athena-licensed site)")

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
    # The constraint has to travel with the archive. A recipient who is told once, in
    # an email, that a bundle may not be onward-shared has nothing to consult later --
    # and this bundle is byte-indistinguishable from the publishable one apart from
    # two files buried in release/. Never written into the release itself: rewriting
    # anything under release/ changes manifest.json's hash and retroactively
    # invalidates the release_manifest_sha256 in every audit.json built against it.
    if carries_snomed:
        notice = NOTICE.encode()
        info = tarfile.TarInfo(str(Path("phecodex-distribution") / NOTICE_NAME))
        info.size = len(notice)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(notice))
checksum_path = args.output.with_name(args.output.name + ".sha256")
checksum_path.write_text(f"{sha256(args.output)}  {args.output.name}\n")
if carries_snomed:
    print(f"WARNING: this bundle carries SNOMED-derived tables and must not be published; "
          f"{NOTICE_NAME} has been added to it", file=sys.stderr)
print(f"bundle: {args.output}")
print(f"sha256: {checksum_path}")
