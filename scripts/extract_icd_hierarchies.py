#!/usr/bin/env python3
"""Extract mapper-ready ICD parent-child references from official archives."""
from __future__ import annotations

import argparse
import csv
import re
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET


def norm(code: str) -> str:
    return re.sub(r"[.\s-]", "", code.strip().upper())


def write_rows(path: Path, rows: set[tuple[str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["vocabulary", "parent_code", "child_code", "source_version"])
        writer.writerows(sorted(rows, key=lambda row: (row[1], row[2])))


def extract_who(zip_path: Path, output: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(archive.read(name))
    title = root.find("Title")
    version = title.attrib.get("version", "WHO ICD-10 2019") if title is not None else "WHO ICD-10 2019"
    rows = set()
    for cls in root.findall(".//Class"):
        child = norm(cls.attrib.get("code", ""))
        if not child:
            continue
        for parent in cls.findall("SuperClass"):
            parent_code = norm(parent.attrib.get("code", ""))
            if parent_code and len(parent_code) < len(child):
                rows.add(("ICD10", parent_code, child, version))
    write_rows(output, rows)


def extract_cm(zip_path: Path, output: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if "tabular" in n.lower() and n.lower().endswith(".xml"))
        root = ET.fromstring(archive.read(name))
    version = root.findtext("version") or "ICD-10-CM 2026"
    rows = set()

    def walk(node: ET.Element, parent: str | None = None) -> None:
        current = norm(node.findtext("name") or "") if node.tag == "diag" else parent
        if node.tag == "diag" and current and parent:
            rows.add(("ICD10CM", parent, current, f"ICD-10-CM {version}"))
        for child in node:
            if child.tag == "diag":
                walk(child, current or parent)
            elif child.tag not in {"name", "desc", "inclusionTerm", "codeFirst", "useAdditionalCode", "seeAlso", "excludes1", "excludes2", "sevenChrNote", "sevenChrDef", "note"}:
                walk(child, current or parent)

    walk(root)
    write_rows(output, rows)


def extract_icd9(zip_path: Path, output: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.upper().endswith("_DESC_LONG_DX.TXT"))
        text = archive.read(name).decode("latin-1")
    codes = {norm(line.split(maxsplit=1)[0]) for line in text.splitlines() if line.strip()}
    # ICD-9-CM diagnosis hierarchy: a four/five-character diagnosis is a
    # subdivision of the longest existing shorter code prefix. This is the
    # official tabular coding structure, applied only to codes in the CMS set.
    rows = set()
    categories = {code[:3] for code in codes if len(code) >= 3 and code[:3][0].isdigit()}
    for child in sorted(codes, key=lambda value: (len(value), value)):
        candidates = [parent for parent in codes if len(parent) < len(child) and child.startswith(parent)]
        if len(child) > 3 and child[:3] in categories:
            candidates.append(child[:3])
        if candidates:
            parent = max(candidates, key=len)
            rows.add(("ICD9CM", parent, child, "CMS ICD-9-CM Version 32; structural hierarchy"))
    write_rows(output, rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--icd10", type=Path, required=True)
    parser.add_argument("--icd10cm", type=Path, required=True)
    parser.add_argument("--icd9cm", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    extract_who(args.icd10, args.output_dir / "icd10_hierarchy.csv")
    extract_cm(args.icd10cm, args.output_dir / "icd10cm_hierarchy.csv")
    extract_icd9(args.icd9cm, args.output_dir / "icd9cm_hierarchy.csv")


if __name__ == "__main__":
    main()
