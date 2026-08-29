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
import math
import random
from collections import defaultdict
from pathlib import Path

import duckdb


def _x_tick_values(xmin: int, xmax: int) -> list[int]:
    """Decade and half-decade ticks (1 and 5 x 10^k) inside the plotted range.

    The endpoints are always labelled. A log axis cannot be read by interpolating
    between two labels the way a linear one can, so an unlabelled first or last
    point has no value a reader can recover from the picture.
    """
    ticks, magnitude = set(), 1
    while magnitude <= xmax:
        ticks.update(value for value in (magnitude, 5 * magnitude) if xmin <= value <= xmax)
        magnitude *= 10
    return sorted(ticks | {xmin, xmax})


def write_svg(path: Path, rows: list[dict], min_cases: int, min_controls: int) -> None:
    width, height, margin = 760, 500, 80
    points = [(int(r["sample_size"]), int(r["retained_phecodes"])) for r in rows]
    if not points:
        # Every requested size was skipped. max() over an empty sequence would raise
        # here, turning "nothing to plot" into a traceback.
        path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
                        f'<rect width="100%" height="100%" fill="white"/>'
                        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
                        f'font-family="sans-serif">No sample size was smaller than the cohort</text></svg>\n')
        return
    # Log x. Sample sizes span two to three orders of magnitude, and on a linear axis
    # everything below ~50,000 collapses into the leftmost few percent of the plot --
    # which is precisely the range a site decides a planned subset in, and where the
    # curve is steepest. main() already guarantees every sample_size is >= 1, so the
    # logarithm is always defined.
    xmin, xmax = min(x for x, _ in points), max(x for x, _ in points)
    log_min, log_span = math.log10(xmin), math.log10(xmax) - math.log10(xmin)
    ymax = max(y for _, y in points) or 1
    plot_width, plot_height = width - 2 * margin, height - 2 * margin

    def x_at(x: int) -> float:
        # A single requested size (or several identical ones) leaves nothing to spread
        # across the axis; centre it rather than dividing by a zero span.
        if log_span <= 0:
            return margin + plot_width / 2
        return margin + (math.log10(x) - log_min) / log_span * plot_width

    def y_at(y: float) -> float:
        return height - margin - y / ymax * plot_height

    polyline = " ".join(f"{x_at(x):.2f},{y_at(y):.2f}" for x, y in points)
    circles = "".join(f'<circle cx="{x_at(x):.2f}" cy="{y_at(y):.2f}" r="4" fill="#2563eb"/>' for x, y in points)
    x_ticks = "".join(
        f'<line x1="{x_at(x):.2f}" y1="{height-margin}" x2="{x_at(x):.2f}" y2="{height-margin+5}" stroke="black"/>'
        f'<text x="{x_at(x):.2f}" y="{height-margin+22}" text-anchor="middle">{x:,}</text>'
        for x in _x_tick_values(xmin, xmax))
    y_ticks = "".join(
        f'<line x1="{margin-5}" y1="{y_at(y):.2f}" x2="{margin}" y2="{y_at(y):.2f}" stroke="black"/>'
        f'<text x="{margin-8}" y="{y_at(y)+4:.2f}" text-anchor="end">{int(y):,}</text>'
        for y in (0, ymax / 2, ymax))
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="black"/><line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="black"/>
<polyline points="{polyline}" fill="none" stroke="#2563eb" stroke-width="3"/>{circles}
<g font-family="sans-serif" font-size="12" fill="black">{x_ticks}{y_ticks}<text x="{width/2}" y="{height-15}" text-anchor="middle">Downsampled cohort size (log scale)</text><text x="20" y="{height/2}" transform="rotate(-90 20 {height/2})" text-anchor="middle">PhecodeX phenotypes retained</text></g>
<text x="{width/2}" y="25" text-anchor="middle">PhecodeX attrition (case/control cutoff {min_cases})</text></svg>\n''')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True,
                         help="Mapper cohort CSV/Parquet with person_id and sex.")
    parser.add_argument("--release", type=Path, default=None,
                         help="Release directory. Supplying it makes sex-restricted phecodes "
                              "score against the matching sex only, as a real run does. Without "
                              "it every phecode is treated as unrestricted, which OVERSTATES "
                              "retention for the ~325 restricted phecodes.")
    parser.add_argument("--person-phecodes", type=Path, required=True, help="Mapper person_phecodes.parquet.")
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-svg", type=Path, required=True)
    parser.add_argument("--sample-sizes", default="1000,5000,10000,25000,50000,100000,200000,300000,400000", help="Comma-separated sizes; values above the cohort size are skipped.")
    parser.add_argument("--min-cases", type=int, default=200)
    parser.add_argument("--min-controls", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    con = duckdb.connect()
    reader = "read_parquet" if args.cohort.suffix.lower() == ".parquet" else "read_csv_auto"
    cohort_columns = {r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM {reader}('{args.cohort}')").fetchall()}
    has_sex = "sex" in cohort_columns
    sex_expression = "upper(trim(CAST(sex AS VARCHAR)))" if has_sex else "''"
    rows_in = con.execute(
        f"SELECT DISTINCT CAST(person_id AS VARCHAR), {sex_expression} "
        f"FROM {reader}('{args.cohort}')").fetchall()
    people = [r[0] for r in rows_in]
    sex_of = {r[0]: r[1] for r in rows_in}

    # Which phecodes are sex-restricted, from the release rather than assumed. A
    # restricted phecode is evaluable only in the matching sex, so both its case count
    # and its control denominator come from that half of the sample -- treating the
    # whole sample as the denominator lets it clear --min-controls at a sample size
    # where a real run would drop it.
    restrict: dict[str, str] = {}
    if args.release:
        info = args.release / "phecode_info.parquet"
        if info.is_file():
            columns = {r[0] for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{info}')").fetchall()}
            if {"phecode", "sex"} <= columns:
                restrict = {r[0]: r[1] for r in con.execute(
                    f"SELECT phecode, upper(trim(sex)) FROM read_parquet('{info}') "
                    f"WHERE upper(trim(sex)) IN ('MALE','FEMALE')").fetchall()}
    if restrict and not has_sex:
        raise SystemExit("--release names sex-restricted phecodes but --cohort has no sex "
                         "column; the curve would overstate retention. Supply a cohort with sex.")
    if args.release and not restrict:
        print("note: the release restricts no phecode by sex, so every phecode is scored "
              "against the whole sample")
    cases = con.execute(f"SELECT CAST(person_id AS VARCHAR), phecode FROM read_parquet('{args.person_phecodes}')").fetchall()
    by_phecode: dict[str, set] = defaultdict(set)
    for person_id, phecode in cases:
        by_phecode[str(phecode)].add(person_id)
    rng = random.Random(args.seed)
    shuffled = people[:]
    rng.shuffle(shuffled)
    rows: list[dict] = []
    for requested in sorted({int(x) for x in args.sample_sizes.split(",") if x.strip()}):
        # Skip, as --sample-sizes documents. Clamping to the cohort size instead emitted
        # a SECOND row for the full cohort whenever an oversized value was passed --
        # a duplicate point on the curve that reads as an extra measurement.
        if requested > len(shuffled):
            print(f"skipping --sample-sizes {requested}: larger than the cohort ({len(shuffled)})")
            continue
        n = requested
        if n <= 0: continue
        selected = set(shuffled[:n])
        n_male = sum(1 for p in selected if sex_of.get(p) == "MALE")
        n_female = sum(1 for p in selected if sex_of.get(p) == "FEMALE")
        retained = 0
        for phecode, case_people in by_phecode.items():
            cases_n = len(case_people & selected)
            evaluable = {"MALE": n_male, "FEMALE": n_female}.get(restrict.get(phecode), n)
            if cases_n >= args.min_cases and evaluable - cases_n >= args.min_controls:
                retained += 1
        rows.append({"sample_size": n, "retained_phecodes": retained,
                     "min_cases": args.min_cases, "min_controls": args.min_controls,
                     "sex_aware": bool(restrict), "seed": args.seed})
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["sample_size", "retained_phecodes", "min_cases", "min_controls", "seed"])
        writer.writeheader(); writer.writerows(rows)
    write_svg(args.output_svg, rows, args.min_cases, args.min_controls)
    print(f"Wrote {len(rows)} downsampling points to {args.output_csv} and {args.output_svg}")


if __name__ == "__main__":
    main()
