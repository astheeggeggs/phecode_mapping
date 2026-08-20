#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
from phecodex_mapper.sex_metadata import enrich_sex_metadata

parser = argparse.ArgumentParser(description="Add validated sex metadata to PhecodeX 1.1 info.")
parser.add_argument("--v1-info", type=Path, required=True); parser.add_argument("--v11-info", type=Path, required=True)
parser.add_argument("--changes", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--review-output", type=Path, required=True)
args = parser.parse_args()
print(enrich_sex_metadata(args.v1_info, args.v11_info, args.changes, args.output, args.review_output))
