from __future__ import annotations

import json
from pathlib import Path

from .io import checksum, connect, quote, relation_for
from .mapper import map_phecodes, validate_cohort_and_events

SUPPORTED = {"ICD9CM", "ICD10", "ICD10CM", "SNOMED"}
def preflight(release: Path, cohort: Path, events: Path) -> dict:
    if not release.is_dir():
        raise ValueError(f"Release directory does not exist: {release}")
    required = [release / "manifest.json", release / "icd_map.parquet", release / "phecode_info.parquet"]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise ValueError(f"Release is incomplete; missing: {missing}")
    manifest = json.loads((release / "manifest.json").read_text())
    if not manifest.get("tool_version") or not manifest.get("counts"):
        raise ValueError("Release manifest is missing tool_version or counts metadata")
    con = connect()
    cohort_src, event_src = relation_for(cohort), relation_for(events)
    stats = validate_cohort_and_events(con, cohort_src, event_src)
    release_phecodes = con.execute(f"SELECT count(DISTINCT phecode) FROM read_parquet('{quote(release / 'icd_map.parquet')}')").fetchone()[0]
    return {"release": str(release), "release_manifest_sha256": checksum(release / "manifest.json"),
            "inputs": {"cohort": {"path": str(cohort), "sha256": checksum(cohort)}, "events": {"path": str(events), "sha256": checksum(events)}},
            **stats,
            "release_phecode_count": release_phecodes,
            "estimated_matrix_cells_upper_bound": stats["cohort_rows"] * release_phecodes,
            "estimated_matrix_note": "Upper bound before event mapping, exclusions, sex restrictions, and retention thresholds."}


def run_workflow(*, release: Path, cohort: Path, events: Path, output: Path, case_rule: str = "any-event", exclusions: Path | None = None, exclude_phenotypes: Path | None = None, min_cases: int = 200, min_controls: int = 200, max_unmapped_rate: float = 1.0) -> dict:
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}. Remove it or choose a new --output path.")
    checks = preflight(release, cohort, events)
    if exclusions:
        checks["inputs"]["control_exclusions"] = {"path": str(exclusions), "sha256": checksum(exclusions)}
    if exclude_phenotypes:
        checks["inputs"]["exclude_phenotypes"] = {"path": str(exclude_phenotypes), "sha256": checksum(exclude_phenotypes)}
    map_phecodes(release=release, cohort=cohort, events=events, output=output, case_rule=case_rule, exclusions=exclusions, min_cases=min_cases, min_controls=min_controls, max_unmapped_rate=max_unmapped_rate, exclude_phenotypes=exclude_phenotypes)
    audit_path = output / "audit.json"
    audit = json.loads(audit_path.read_text())
    audit["preflight"] = checks
    audit["workflow"] = "analyst-run"
    audit["sensitive_outputs"] = [name for name in ("person_phecodes.parquet", "unmapped_events.csv") if (output / name).exists()]
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    return audit
