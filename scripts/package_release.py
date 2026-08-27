#!/usr/bin/env python3
"""Package an already-built release for controlled consortium distribution."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--release", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True, help="Output .tar.gz path")
args = parser.parse_args()

if not (args.release / "manifest.json").is_file():
    raise SystemExit(f"Release is missing manifest.json: {args.release}")
if args.output.exists():
    raise SystemExit(f"Output already exists: {args.output}")

manifest = json.loads((args.release / "manifest.json").read_text())

# Packaging must NOT modify the release. This script used to add a `bundle_contents`
# key and rewrite manifest.json, which changed the manifest's own hash -- retroactively
# invalidating the release_manifest_sha256 recorded in every audit.json produced against
# that release, so validate-phecodex then rejected already-completed runs. The key was
# also redundant: manifest["artifacts"] already lists every shipped file, with a digest
# for each. Package the release exactly as it was built.
artifacts = manifest.get("artifacts")
if not isinstance(artifacts, dict) or not artifacts:
    raise SystemExit(
        "manifest.json records no artifact checksums; rebuild the release with a current "
        "build-vocabulary before packaging it for distribution")

problems = []
for name, meta in sorted(artifacts.items()):
    path = args.release / name
    if not path.is_file():
        problems.append(f"{name}: recorded in the manifest but missing")
    elif sha256(path) != meta.get("sha256"):
        problems.append(f"{name}: sha256 does not match the manifest")
untracked = sorted(p.name for p in args.release.iterdir()
                   if p.is_file() and p.name != "manifest.json" and p.name not in artifacts)
if problems:
    raise SystemExit("Refusing to package a release that does not verify:\n  " + "\n  ".join(problems))

args.output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(args.output, "w:gz") as archive:
    archive.add(args.release, arcname=args.release.name)
checksum_path = args.output.with_name(args.output.name + ".sha256")
checksum_path.write_text(f"{sha256(args.output)}  {args.output.name}\n")
print(f"bundle: {args.output}")
print(f"sha256: {checksum_path}")
print(f"verified: {len(artifacts)} artifacts against manifest.json")
if untracked:
    print(f"warning: packaged {len(untracked)} file(s) with no manifest checksum: {untracked}",
          file=sys.stderr)
