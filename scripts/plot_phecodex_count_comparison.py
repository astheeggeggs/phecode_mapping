#!/usr/bin/env python3
"""Create a dependency-free SVG scatter plot of local and All by All counts."""
from __future__ import annotations

import argparse
import csv
import html
import math
import re
from pathlib import Path

PHECODEX = re.compile(r"^[A-Z]{2}_[0-9]+(?:\.[0-9]+)?$")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--comparison", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--label-count", type=int, default=15)
    args = p.parse_args()
    rows = []
    with args.comparison.open(newline="") as f:
        for row in csv.DictReader(f):
            code = row.get("phecode", "")
            if not PHECODEX.fullmatch(code):
                continue
            try:
                local = float(row["local_case_count"])
                external = float(row["external_case_count"])
            except (KeyError, TypeError, ValueError):
                continue
            if local < 0 or external < 0:
                continue
            rows.append((code, local, external, row.get("description_external", "")))
    if not rows:
        raise SystemExit("No valid PhecodeX count rows found")

    # Small positive floor permits zero counts on logarithmic axes.
    floor = 1.0
    xmax = max([floor] + [r[1] for r in rows])
    ymax = max([floor] + [r[2] for r in rows])
    max_value = 10 ** math.ceil(math.log10(max(xmax, ymax)))
    width, height, left, right, top, bottom = 1100, 800, 105, 35, 55, 95
    plot_w, plot_h = width-left-right, height-top-bottom
    log_max = math.log10(max_value)
    def xy(x: float, y: float) -> tuple[float, float]:
        return (left + math.log10(max(x, floor))/log_max*plot_w,
                height-bottom - math.log10(max(y, floor))/log_max*plot_h)
    def esc(s: str) -> str: return html.escape(str(s), quote=True)
    labels = sorted(rows, key=lambda r: abs(math.log10(max(r[1], floor))-math.log10(max(r[2], floor))), reverse=True)[:args.label_count]
    label_codes = {r[0] for r in labels}
    ticks = [10 ** i for i in range(int(log_max)+1)]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="20" font-weight="bold">Local vs All by All PhecodeX case counts</text>']
    for t in ticks:
        x, _ = xy(t, floor); _, y = xy(floor, t)
        parts += [f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height-bottom}" stroke="#e5e7eb"/>',
                  f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>',
                  f'<text x="{x:.1f}" y="{height-bottom+24}" text-anchor="middle" font-family="sans-serif" font-size="12">{t:g}</text>',
                  f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12">{t:g}</text>']
    x0,y0=xy(floor,floor); x1,y1=xy(max_value,max_value)
    parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="#6b7280" stroke-dasharray="6 5"/>')
    for code, local, external, desc in rows:
        x,y=xy(local,external); color="#dc2626" if code in label_codes else "#2563eb"
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}" fill-opacity="0.65"/>')
    for code, local, external, desc in labels:
        x,y=xy(local,external)
        parts.append(f'<text x="{x+6:.1f}" y="{y-6:.1f}" font-family="sans-serif" font-size="11" fill="#111827">{esc(code)}</text>')
    parts += [f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#111827"/>',
              f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#111827"/>',
              f'<text x="{width/2}" y="{height-25}" text-anchor="middle" font-family="sans-serif" font-size="15">Local case count (log scale)</text>',
              f'<text x="20" y="{height/2}" transform="rotate(-90 20 {height/2})" text-anchor="middle" font-family="sans-serif" font-size="15">All by All case count (log scale)</text>',
              '</svg>']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(parts) + "\n")
    print(f"Wrote {len(rows)} points to {args.output}")


if __name__ == "__main__": main()
