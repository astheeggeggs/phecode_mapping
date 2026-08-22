from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import duckdb

from .io import checksum, connect, quote, relation_for

PHECODEX_RE = re.compile(r"^[A-Z]{2}_[0-9]+(?:\.[0-9]+)?$")
REQUIRED_EXTERNAL = {
    "phecode", "description", "sex", "ancestry", "case_count",
    "control_count", "sample_count", "source", "source_version",
}


def _columns(con: duckdb.DuckDBPyConnection, source: str) -> set[str]:
    return {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}


def _read_rows(path: Path, query: str) -> tuple[list[str], list[tuple]]:
    con = connect()
    source = relation_for(path)
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()], con.execute(query.format(source=source)).fetchall()


def _write_csv(path: Path, headers: list[str], rows: list[tuple]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


def _write_svg(path: Path, rows: list[dict]) -> None:
    points = [(float(r["local_case_proportion"]), float(r["external_case_proportion"]))
              for r in rows if r["local_case_proportion"] is not None and r["external_case_proportion"] is not None]
    width, height, margin = 760, 560, 60
    def point(x: float, y: float) -> tuple[float, float]:
        return margin + x * (width - 2 * margin), height - margin - y * (height - 2 * margin)
    circles = "\n".join(f'<circle cx="{point(x, y)[0]:.2f}" cy="{point(x, y)[1]:.2f}" r="2.5" fill="#2563eb" />' for x, y in points)
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="black"/><line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="black"/>
<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{margin}" stroke="#9ca3af" stroke-dasharray="4 4"/>{circles}
<text x="{width/2}" y="{height-15}" text-anchor="middle">Local UKB case proportion</text><text x="15" y="{height/2}" transform="rotate(-90 15 {height/2})" text-anchor="middle">All by All case proportion</text>
</svg>\n''')


def _validate_phecodex_ids(values: list[str], label: str) -> None:
    bad = sorted({str(v) for v in values if v and not PHECODEX_RE.fullmatch(str(v))})
    if bad:
        raise ValueError(f"{label} contains non-PhecodeX identifiers, e.g. {bad[:5]}")


def validate_phecodex_counts(run: Path, release: Path, external: Path, output: Path, hierarchy_aware: bool = False) -> None:
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}")
    suffix = "_hierarchy" if hierarchy_aware else ""
    required_local = [run / f"phecode_counts{suffix}.parquet", run / f"person_phecodes{suffix}.parquet", run / "audit.json", run / f"unmapped_events{suffix}.csv"]
    required_release = [release / "phecode_info.parquet", release / "manifest.json"]
    missing = [str(p) for p in required_local + required_release if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Validation inputs missing: {missing}")

    con = connect()
    local_src = relation_for(run / f"phecode_counts{suffix}.parquet")
    person_src = relation_for(run / f"person_phecodes{suffix}.parquet")
    info_src = relation_for(release / "phecode_info.parquet")
    external_src = relation_for(external)
    external_columns = _columns(con, external_src)
    if REQUIRED_EXTERNAL - external_columns:
        raise ValueError(f"All by All summary missing columns: {sorted(REQUIRED_EXTERNAL - external_columns)}")

    local_ids = [r[0] for r in con.execute(f"SELECT phecode FROM {local_src}").fetchall()]
    external_ids = [r[0] for r in con.execute(f"SELECT phecode FROM {external_src}").fetchall()]
    _validate_phecodex_ids(local_ids, "Local counts")
    _validate_phecodex_ids(external_ids, "All by All summary")

    audit = json.loads((run / "audit.json").read_text())
    manifest = json.loads((release / "manifest.json").read_text())
    if not audit.get("release_manifest_sha256"):
        raise ValueError("audit.json is missing release_manifest_sha256")
    if audit["release_manifest_sha256"] != checksum(release / "manifest.json"):
        raise ValueError("audit.json release_manifest_sha256 does not match the release manifest")

    info_columns = _columns(con, info_src)
    if not {"phecode", "sex"}.issubset(info_columns):
        raise ValueError("phecode_info.parquet requires phecode and sex columns")
    info = {r[0]: (r[1] or "Both") for r in con.execute(f"SELECT phecode, sex FROM {info_src}").fetchall()}

    duplicate_cases = con.execute(f"SELECT phecode, count(*) - count(DISTINCT person_id) FROM {person_src} GROUP BY phecode HAVING count(*) != count(DISTINCT person_id)").fetchall()
    if duplicate_cases:
        raise ValueError(f"person_phecodes contains duplicate person/phecode cases: {duplicate_cases[:5]}")
    local_description = "i.phecode_string" if "phecode_string" in info_columns else ("i.description" if "description" in info_columns else "''")
    local = con.execute(f"""
        SELECT c.phecode, {local_description} AS description_local, i.sex, c.case_count,
               c.control_count_before_exclusions, c.control_count_after_exclusions,
               c.excluded_control_count,
               c.case_count + c.control_count_before_exclusions AS sample_count,
               CAST(c.case_count AS DOUBLE) / NULLIF(c.case_count + c.control_count_before_exclusions, 0) AS case_proportion
        FROM {local_src} c LEFT JOIN {info_src} i USING (phecode)
    """).fetchall()
    local_by_id = {r[0]: r for r in local}
    recon = con.execute(f"""
        SELECT c.phecode, c.case_count, count(p.person_id)
        FROM {local_src} c LEFT JOIN {person_src} p USING (phecode)
        GROUP BY c.phecode, c.case_count HAVING c.case_count != count(p.person_id)
    """).fetchall()

    external = con.execute(f"""
        SELECT phecode, description, sex, ancestry,
               try_cast(case_count AS BIGINT) AS case_count,
               try_cast(control_count AS BIGINT) AS control_count,
               try_cast(sample_count AS BIGINT) AS sample_count,
               try_cast(case_count AS DOUBLE) / NULLIF(try_cast(sample_count AS DOUBLE), 0) AS case_proportion,
               source, source_version
        FROM {external_src}
    """).fetchall()
    headers = ["phecode", "description_local", "description_external", "sex_local", "sex_external", "ancestry", "local_case_count", "external_case_count", "local_control_count", "external_control_count", "local_sample_count", "external_sample_count", "local_case_proportion", "external_case_proportion", "absolute_proportion_difference", "relative_proportion_difference", "denominator_difference", "source", "source_version", "review_reason"]
    comparison: list[dict] = []
    for row in external:
        phecode, description, sex, ancestry, cases, controls, sample, proportion, source, version = row
        l = local_by_id.get(phecode)
        if l is None:
            reason = "missing_local_phecode"
            values = [phecode, "", description, "", sex, ancestry, None, cases, None, controls, None, sample, None, proportion, None, None, None, source, version, reason]
        else:
            local_description, local_sex, lc, lcb, lca, excluded, ls, lp = l[1:]
            reasons = []
            if str(local_sex or "Both").upper() != str(sex or "Both").upper(): reasons.append("sex_stratum_mismatch")
            if ancestry not in (None, "", "ALL", "META"): reasons.append("cross_biobank_ancestry_stratum")
            if ls != sample: reasons.append("denominator_mismatch")
            if local_sex and str(local_sex).upper() != "BOTH": reasons.append("local_sex_denominator_not_available")
            abs_diff = None if lp is None or proportion is None else abs(lp - proportion)
            rel_diff = None if abs_diff is None or proportion == 0 else abs_diff / proportion
            values = [phecode, local_description or "", description, local_sex, sex, ancestry, lc, cases, lcb, controls, ls, sample, lp, proportion, abs_diff, rel_diff, ls - sample if ls is not None and sample is not None else None, source, version, ";".join(reasons)]
        comparison.append(dict(zip(headers, values)))
    comparison.sort(key=lambda r: (r["absolute_proportion_difference"] is None, -(r["absolute_proportion_difference"] or 0)))
    output.mkdir(parents=True)
    _write_csv(output / "phecodex_comparison.csv", headers, [tuple(r[h] for h in headers) for r in comparison])
    review = [r for r in comparison if r["review_reason"] or (r["relative_proportion_difference"] is not None and r["relative_proportion_difference"] >= 0.5)]
    _write_csv(output / "phecodex_review.csv", headers, [tuple(r[h] for h in headers) for r in review])
    _write_svg(output / "prevalence_scatter.svg", comparison)

    unmapped_src = relation_for(run / f"unmapped_events{suffix}.csv")
    vocab_rates = con.execute(f"SELECT vocabulary, count(*) FROM {unmapped_src} GROUP BY vocabulary ORDER BY vocabulary").fetchall()
    local_ids_set, external_ids_set = set(local_ids), set(external_ids)
    qc = {
        "status": "warning" if review or recon else "pass",
        "comparison_rows": len(comparison),
        "matched_phecodes": len(local_ids_set & external_ids_set),
        "missing_local_phecodes": len(external_ids_set - local_ids_set),
        "missing_external_phecodes": len(local_ids_set - external_ids_set),
        "internal_case_reconstruction_errors": [list(r) for r in recon],
        "unmapped_events_by_vocabulary": {str(k): v for k, v in vocab_rates},
        "sex_specific_denominator_note": "The aggregate local outputs do not contain cohort sex totals; sex-specific local denominator comparisons are flagged rather than inferred.",
        "local_audit": audit,
        "local_mapping_variant": "hierarchy-aware" if hierarchy_aware else "exact",
        "release_manifest_sha256": checksum(release / "manifest.json"),
        "external_source_versions": sorted({str(r[9]) for r in external}),
        "review_rows": len(review),
    }
    (output / "validation.json").write_text(json.dumps(qc, indent=2, sort_keys=True) + "\n")
