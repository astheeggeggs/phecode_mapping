#!/usr/bin/env python3
"""Package an already-built release for controlled consortium distribution."""
from __future__ import annotations

import argparse
import hashlib
import json
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
manifest["bundle_contents"] = sorted(p.name for p in args.release.iterdir() if p.is_file())
(args.release / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

args.output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(args.output, "w:gz") as archive:
    archive.add(args.release, arcname=args.release.name)
checksum_path = args.output.with_name(args.output.name + ".sha256")
checksum_path.write_text(f"{sha256(args.output)}  {args.output.name}\n")
print(f"bundle: {args.output}")
print(f"sha256: {checksum_path}")
