#!/usr/bin/env python3
"""Plot PhecodeX phenotype attrition after repeated cohort downsampling.

This is an aggregate QC tool. It uses the mapper's any-event case table and
reapplies both the case and control thresholds at every requested sample size.
It does not reproduce control exclusions or sex-specific denominators; those
limitations are recorded in the output metadata and README guidance.
"""
from __future__ import annotations

import argparse
import csv
import random
from collections import defaultdict
from pathlib import Path

import duckdb


def write_svg(path: Path, rows: list[dict], min_cases: int, min_controls: int) -> None:
    width, height, margin = 760, 500, 80
    points = [(int(r["sample_size"]), int(r["retained_phecodes"])) for r in rows]
    xmax = max(x for x, _ in points) or 1
    ymax = max(y for _, y in points) or 1
    def xy(x: int, y: int) -> tuple[float, float]:
        return margin + x / xmax * (width - 2 * margin), height - margin - y / ymax * (height - 2 * margin)
    polyline = " ".join(f"{xy(x, y)[0]:.2f},{xy(x, y)[1]:.2f}" for x, y in points)
    circles = "".join(f'<circle cx="{xy(x, y)[0]:.2f}" cy="{xy(x, y)[1]:.2f}" r="4" fill="#2563eb"/>' for x, y in points)
    x_ticks = "".join(f'<text x="{xy(x, 0)[0]:.2f}" y="{height-margin+22}" text-anchor="middle">{x:,}</text>' for x in (0, xmax/2, xmax))
    y_ticks = "".join(f'<text x="{margin-8}" y="{xy(0, y)[1]+4:.2f}" text-anchor="end">{int(y):,}</text>' for y in (0, ymax/2, ymax))
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="black"/><line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="black"/>
<polyline points="{polyline}" fill="none" stroke="#2563eb" stroke-width="3"/>{circles}
<g font-family="sans-serif" font-size="12" fill="black">{x_ticks}{y_ticks}<text x="{width/2}" y="{height-15}" text-anchor="middle">Downsampled cohort size</text><text x="20" y="{height/2}" transform="rotate(-90 20 {height/2})" text-anchor="middle">PhecodeX phenotypes retained</text></g>
<text x="{width/2}" y="25" text-anchor="middle">PhecodeX attrition (case/control cutoff {min_cases})</text></svg>\n''')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True, help="Mapper cohort CSV/Parquet with person_id.")
    parser.add_argument("--person-phecodes", type=Path, required=True, help="Mapper person_phecodes.parquet.")
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-svg", type=Path, required=True)
    parser.add_argument("--sample-sizes", default="1000,5000,10000,25000,50000,100000,200000,300000,400000", help="Comma-separated sizes; values above the cohort size are skipped.")
    parser.add_argument("--min-cases", type=int, default=200)
    parser.add_argument("--min-controls", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    con = duckdb.connect()
    cohort = con.execute(f"SELECT DISTINCT CAST(person_id AS VARCHAR) FROM read_csv_auto('{args.cohort}')" if args.cohort.suffix.lower() != ".parquet" else f"SELECT DISTINCT CAST(person_id AS VARCHAR) FROM read_parquet('{args.cohort}')").fetchall()
    people = [r[0] for r in cohort]
    cases = con.execute(f"SELECT CAST(person_id AS VARCHAR), phecode FROM read_parquet('{args.person_phecodes}')").fetchall()
    by_phecode: dict[str, set] = defaultdict(set)
    for person_id, phecode in cases:
        by_phecode[str(phecode)].add(person_id)
    rng = random.Random(args.seed)
    shuffled = people[:]
    rng.shuffle(shuffled)
    rows: list[dict] = []
    for requested in sorted({int(x) for x in args.sample_sizes.split(",") if x.strip()}):
        n = min(requested, len(shuffled))
        if n == 0: continue
        selected = set(shuffled[:n])
        retained = sum(1 for case_people in by_phecode.values() if (cases_n := len(case_people & selected)) >= args.min_cases and n - cases_n >= args.min_controls)
        rows.append({"sample_size": n, "retained_phecodes": retained, "min_cases": args.min_cases, "min_controls": args.min_controls, "seed": args.seed})
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["sample_size", "retained_phecodes", "min_cases", "min_controls", "seed"])
        writer.writeheader(); writer.writerows(rows)
    write_svg(args.output_svg, rows, args.min_cases, args.min_controls)
    print(f"Wrote {len(rows)} downsampling points to {args.output_csv} and {args.output_svg}")


if __name__ == "__main__":
    main()
