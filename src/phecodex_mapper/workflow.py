from __future__ import annotations

import json
from pathlib import Path

from .io import checksum, connect, quote, relation_for
from .mapper import map_phecodes

SUPPORTED = {"ICD9CM", "ICD10", "ICD10CM", "SNOMED"}


def preflight(release: Path, cohort: Path, events: Path, hierarchy_aware: bool = True) -> dict:
    if not release.is_dir():
        raise ValueError(f"Release directory does not exist: {release}")
    required = [release / "manifest.json", release / "icd_map.parquet", release / "phecode_info.parquet"]
    if hierarchy_aware:
        required.append(release / "icd_hierarchy.parquet")
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise ValueError(f"Release is incomplete; missing: {missing}")
    manifest = json.loads((release / "manifest.json").read_text())
    if not manifest.get("tool_version") or not manifest.get("counts"):
        raise ValueError("Release manifest is missing tool_version or counts metadata")
    con = connect()
    cohort_src, event_src = relation_for(cohort), relation_for(events)
    cohort_cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_src}").fetchall()}
    event_cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {event_src}").fetchall()}
    for name, cols, required_cols in (("cohort", cohort_cols, {"person_id", "sex"}), ("events", event_cols, {"person_id", "code", "vocabulary"})):
        missing_cols = required_cols - cols
        if missing_cols:
            raise ValueError(f"{name} is missing required columns: {sorted(missing_cols)}")
    cohort_rows = con.execute(f"SELECT count(*) FROM {cohort_src}").fetchone()[0]
    duplicate_people = con.execute(f"SELECT count(*) - count(DISTINCT person_id) FROM {cohort_src}").fetchone()[0]
    null_people = con.execute(f"SELECT count(*) FROM {cohort_src} WHERE person_id IS NULL OR trim(CAST(person_id AS VARCHAR))='' ").fetchone()[0]
    if duplicate_people or null_people:
        raise ValueError(f"cohort person_id must be non-null and unique (duplicates={duplicate_people}, null_or_blank={null_people})")
    bad_sex = con.execute(f"SELECT DISTINCT sex FROM {cohort_src} WHERE sex IS NOT NULL AND upper(trim(CAST(sex AS VARCHAR))) NOT IN ('MALE','FEMALE')").fetchall()
    if bad_sex:
        raise ValueError(f"cohort sex must be Male, Female, or missing; found: {[r[0] for r in bad_sex[:10]]}")
    event_rows = con.execute(f"SELECT count(*) FROM {event_src}").fetchone()[0]
    missing_event_fields = con.execute(f"SELECT count(*) FROM {event_src} WHERE person_id IS NULL OR code IS NULL OR vocabulary IS NULL").fetchone()[0]
    bad_vocab = con.execute(f"SELECT upper(trim(CAST(vocabulary AS VARCHAR))), count(*) FROM {event_src} GROUP BY 1 HAVING upper(trim(CAST(vocabulary AS VARCHAR))) NOT IN ('ICD9CM','ICD10','ICD10CM','SNOMED')").fetchall()
    vocab_counts = {str(r[0]): r[1] for r in con.execute(f"SELECT upper(trim(CAST(vocabulary AS VARCHAR))), count(*) FROM {event_src} GROUP BY 1 ORDER BY 1").fetchall()}
    if missing_event_fields:
        raise ValueError(f"events contain {missing_event_fields} rows missing person_id, code, or vocabulary")
    if bad_vocab:
        raise ValueError(f"events contain unsupported vocabularies: {[r[0] for r in bad_vocab]}")
    unknown_people = con.execute(f"SELECT count(*) FROM {event_src} e LEFT JOIN {cohort_src} c USING (person_id) WHERE c.person_id IS NULL").fetchone()[0]
    release_phecodes = con.execute(f"SELECT count(DISTINCT phecode) FROM read_parquet('{quote(release / 'icd_map.parquet')}')").fetchone()[0]
    return {"release": str(release), "release_manifest_sha256": checksum(release / "manifest.json"),
            "inputs": {"cohort": {"path": str(cohort), "sha256": checksum(cohort)}, "events": {"path": str(events), "sha256": checksum(events)}},
            "cohort_rows": cohort_rows, "event_rows": event_rows, "event_rows_missing_required_fields": missing_event_fields,
            "vocabulary_counts": vocab_counts, "events_for_unknown_people": unknown_people, "hierarchy_aware": hierarchy_aware,
            "release_phecode_count": release_phecodes,
            "estimated_matrix_cells_upper_bound": cohort_rows * release_phecodes,
            "estimated_matrix_note": "Upper bound before event mapping, exclusions, sex restrictions, and retention thresholds."}


def run_workflow(*, release: Path, cohort: Path, events: Path, output: Path, case_rule: str = "any-event", exclusions: Path | None = None, exclude_phenotypes: Path | None = None, min_cases: int = 200, min_controls: int = 200, max_unmapped_rate: float = 1.0, exact_only: bool = False) -> dict:
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}. Remove it or choose a new --output path.")
    checks = preflight(release, cohort, events, hierarchy_aware=not exact_only)
    if exclusions:
        checks["inputs"]["control_exclusions"] = {"path": str(exclusions), "sha256": checksum(exclusions)}
    if exclude_phenotypes:
        checks["inputs"]["exclude_phenotypes"] = {"path": str(exclude_phenotypes), "sha256": checksum(exclude_phenotypes)}
    map_phecodes(release=release, cohort=cohort, events=events, output=output, case_rule=case_rule, exclusions=exclusions, min_cases=min_cases, min_controls=min_controls, max_unmapped_rate=max_unmapped_rate, exclude_phenotypes=exclude_phenotypes, hierarchy_aware=not exact_only)
    audit_path = output / "audit.json"
    audit = json.loads(audit_path.read_text())
    audit["preflight"] = checks
    audit["workflow"] = "analyst-run"
    audit["mapping_variant"] = "exact" if exact_only else "hierarchy-aware"
    audit["sensitive_outputs"] = [name for name in ("person_phecodes.parquet", "person_phecodes_hierarchy.parquet", "unmapped_events.csv", "unmapped_events_hierarchy.csv") if (output / name).exists()]
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    return audit
