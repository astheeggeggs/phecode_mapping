#!/usr/bin/env python3
"""Optional PheTK parity harness; run only in the phetk extra environment.

It deliberately uses a frozen custom map and fixture inputs. The production
package neither imports nor redistributes PheTK (GPL-3.0).

--fixture-events must already be in PheTK's custom ICD-file shape, i.e. columns
person_id, date, vocabulary_id, ICD -- NOT this package's person_id/code/vocabulary/
event_date events file. PheTK also matches ICD and vocabulary_id as exact strings
(no punctuation/case normalization), so the fixture's ICD codes must be spelled
exactly as they appear in --custom-map's ICD column for a fair comparison; run
phecodex_mapper.normalize.normalize_code over your raw codes first if they aren't
already clean.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phetk-command", default="phetk")
    parser.add_argument("--fixture-events", required=True, type=Path,
                         help="CSV/TSV in PheTK's custom ICD-file shape: person_id, date, "
                              "vocabulary_id, ICD.")
    parser.add_argument("--custom-map", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    # PheTK's documented custom-platform interface is deliberately invoked as a
    # subprocess so licensing and dependency boundaries remain explicit.
    subprocess.run([args.phetk_command, "phecode", "count-phecode", "--platform", "custom",
                    "--icd_file_path", str(args.fixture_events), "--icd_version", "custom",
                    "--phecode_map_file_path", str(args.custom_map),
                    "--output_file_path", str(args.output)], check=True)


if __name__ == "__main__": main()
