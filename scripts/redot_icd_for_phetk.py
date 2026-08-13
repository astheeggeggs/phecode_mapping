#!/usr/bin/env python3
"""Re-insert standard ICD9/ICD10 decimal placement into a PheTK custom ICD
file (columns: person_id, date, vocabulary_id, ICD), for fair PheTK parity
testing against sources (e.g. UK Biobank extractions) that strip punctuation.

PheTK's custom-platform loader matches ICD/vocabulary_id as exact strings
with no normalization (see README's "PheTK comparison" section). If your
source codes have had decimals stripped -- as UK Biobank's raw hesin/hospital
diagnosis fields typically do once split into single strings -- PheTK will
silently fail to match most subcode-level phecode map entries. This script
reconstructs the decimal position from the standard ICD9CM/ICD10CM category
lengths (it does NOT look up the phecode map, so it's a fair, independent
reformatting rather than a comparison-defeating cheat):

  - ICD9CM: category is 3 digits, or 4 for E-codes (E + 3 digits).
    e.g. "25009" -> "250.09"; "V270" -> "V27.0"; "E8889" -> "E888.9"
  - ICD10CM: category is always 3 characters (letter + 2 alphanumerics).
    e.g. "A001" -> "A00.1"

Usage
-----
  python scripts/redot_icd_for_phetk.py \
    --input phetk_fixture.csv --output phetk_fixture_dotted.csv

Then run scripts/compare_phetk.py against phetk_fixture_dotted.csv for a
fair comparison; comparing against the un-redotted fixture will make PheTK
look far worse than its actual mapping logic, purely due to spelling.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def redot_icd9(code: str) -> str:
    code = code.strip().upper()
    if not code:
        return code
    cat_len = 4 if code.startswith("E") else 3
    return code if len(code) <= cat_len else f"{code[:cat_len]}.{code[cat_len:]}"


def redot_icd10(code: str) -> str:
    code = code.strip().upper()
    if not code:
        return code
    return code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"


def redot_file(input_path: Path, output_path: Path) -> int:
    n_changed = 0
    with input_path.open(newline="") as fin, output_path.open("w", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.writer(fout)
        writer.writerow(["person_id", "date", "vocabulary_id", "ICD"])
        for row in reader:
            code = row["ICD"]
            vocab = row["vocabulary_id"].strip().upper()
            if vocab == "ICD9CM":
                new_code = redot_icd9(code)
            elif vocab == "ICD10CM":
                new_code = redot_icd10(code)
            else:
                new_code = code
            if new_code != code:
                n_changed += 1
            writer.writerow([row["person_id"], row["date"], row["vocabulary_id"], new_code])
    return n_changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path,
                         help="PheTK custom ICD file (person_id, date, vocabulary_id, ICD) with punctuation stripped.")
    parser.add_argument("--output", required=True, type=Path,
                         help="Path to write the re-punctuated copy.")
    args = parser.parse_args()
    n_changed = redot_file(args.input, args.output)
    print(f"Re-inserted decimals into {n_changed} codes; wrote {args.output}")


if __name__ == "__main__":
    main()
