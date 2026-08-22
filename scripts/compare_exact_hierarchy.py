#!/usr/bin/env python3
"""Compare exact and hierarchy-aware mapper outputs without participant-level export."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True, help="map-phecodes output directory")
    parser.add_argument("--output", type=Path, required=True, help="directory for aggregate comparison outputs")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    run = args.run.resolve()
    out = args.output.resolve()
    c = duckdb.connect()

    exact_unmapped = c.execute(f"SELECT count(*) FROM read_csv_auto('{run / 'unmapped_events.csv'}')").fetchone()[0]
    hierarchy_unmapped = c.execute(f"SELECT count(*) FROM read_csv_auto('{run / 'unmapped_events_hierarchy.csv'}')").fetchone()[0]
    audit = json.loads((run / "audit.json").read_text())
    total_events = audit["events"]
    fallback_events = audit.get("hierarchy_aware", {}).get("fallback_events")

    c.execute(f"""COPY (
      SELECT vocabulary, sum(event_count) AS fallback_event_count,
             count(DISTINCT parent_code) AS mapped_parent_count,
             count(DISTINCT normalized_code) AS child_code_count
      FROM read_csv_auto('{run / 'hierarchy_fallbacks.csv'}')
      GROUP BY vocabulary ORDER BY vocabulary
    ) TO '{out / 'fallback_by_vocabulary.csv'}' (HEADER, DELIMITER ',')""")
    c.execute(f"""COPY (
      SELECT vocabulary, parent_code, phecode,
             sum(event_count) AS fallback_event_count,
             count(DISTINCT normalized_code) AS child_code_count
      FROM read_csv_auto('{run / 'hierarchy_fallbacks.csv'}')
      GROUP BY vocabulary, parent_code, phecode
      ORDER BY fallback_event_count DESC, vocabulary, parent_code, phecode
    ) TO '{out / 'fallback_by_parent_and_phecode.csv'}' (HEADER, DELIMITER ',')""")
    c.execute(f"""COPY (
      SELECT coalesce(e.phecode, h.phecode) AS phecode,
             coalesce(e.case_count, 0) AS exact_case_count,
             coalesce(h.case_count, 0) AS hierarchy_case_count,
             coalesce(h.case_count, 0) - coalesce(e.case_count, 0) AS case_count_difference,
             coalesce(e.retained, false) AS exact_retained,
             coalesce(h.retained, false) AS hierarchy_retained
      FROM read_parquet('{run / 'phecode_counts.parquet'}') e
      FULL OUTER JOIN read_parquet('{run / 'phecode_counts_hierarchy.parquet'}') h USING (phecode)
      WHERE coalesce(e.case_count, 0) <> coalesce(h.case_count, 0)
      ORDER BY case_count_difference DESC, phecode
    ) TO '{out / 'changed_phecodes.csv'}' (HEADER, DELIMITER ',')""")

    report = {
        "total_events": total_events,
        "exact_unmapped_events": exact_unmapped,
        "hierarchy_unmapped_events": hierarchy_unmapped,
        "events_removed_from_unmapped": exact_unmapped - hierarchy_unmapped,
        "fallback_events_reported": fallback_events,
        "fallback_events_plus_hierarchy_unmapped": (fallback_events or 0) + hierarchy_unmapped,
        "consistency_check_passed": exact_unmapped == (fallback_events or 0) + hierarchy_unmapped,
    }
    (out / "comparison_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
