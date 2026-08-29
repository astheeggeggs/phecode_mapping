"""Post-mapping diagnostics: what did NOT map, and does that look like a mistake.

Split out of mapper.py, which had absorbed diagnostics alongside the mapping itself
and was heading for 1,000 lines. Nothing here changes a mapping decision -- these
functions read the mapper's tables and return blocks for audit.json, plus the
warnings an analyst needs in order to read them.
"""
from __future__ import annotations

import sys


def unmapped_by_vocabulary(con) -> dict:
    """Per-vocabulary unmapped tallies, plus the mislabelling heuristic.

    Extracted from map_phecodes, which had grown to ~280 lines by absorbing diagnostics
    alongside the mapping itself. This reads normalized_events/mapped_events/icd_map and
    returns a block for audit.json; it decides nothing about the mapping.

    Per-vocabulary, not just overall. A whole vocabulary mapping badly is a
    different problem from a long tail of odd codes, and the aggregate hides it:
    two real runs sat at 23.7-23.8% unmapped and nothing remarked on it. The
    specific failure this surfaces is a mislabelled vocabulary -- UK Biobank codes
    WHO ICD-10, and events labelled ICD10CM are matched against the CM map, so
    every WHO-only code is silently discarded. `vocabulary` is taken as ground
    truth and nothing else can detect that.
    """
    by_vocabulary = {
        v: {"events": n, "unmapped": u, "unmapped_rate": (u / n if n else 0)}
        for v, n, u in con.execute("""
          SELECT e.vocabulary, count(*), count(*) FILTER (
            WHERE NOT EXISTS (SELECT 1 FROM mapped_events m WHERE m.event_id = e.event_id))
          FROM normalized_events e GROUP BY e.vocabulary ORDER BY e.vocabulary
        """).fetchall()}
    # Advisory only: --max-unmapped-rate defaults to 1.0 so the hard check below can
    # never fire, which is a deliberate default (a site cannot know its rate before
    # the first run) but leaves nothing to notice a bad one. This warns instead.
    #
    # A high unmapped rate is NOT evidence of mislabelling by itself. PhecodeX's WHO
    # map is genuinely coarse, so a correctly-labelled UK Biobank extract sits at
    # 20.3% -- warning on the rate alone fires on the right answer and points the
    # analyst at the wrong one, and following it corrupts the run. What discriminates
    # is the counterfactual: of the events that FAILED, how many would map under the
    # sibling ICD-10 label? Measured on 2.5M UK Biobank events, correctly labelled
    # ICD10 rescues 0.8% of its failures under ICD10CM, while the same events
    # mislabelled ICD10CM rescue 19.8% under ICD10 -- a 24x separation, against only
    # 4.8 points between the two overall rates. The 5% threshold sits ~6x above the
    # false-alarm case and ~4x below the true one.
    sibling_of = {"ICD10": "ICD10CM", "ICD10CM": "ICD10"}
    for vocabulary, stats in by_vocabulary.items():
        sibling = sibling_of.get(vocabulary)
        if not sibling or stats["events"] < 1000 or not stats["unmapped"]:
            continue
        rescued = con.execute("""
          SELECT count(*) FROM normalized_events e
          WHERE e.vocabulary = ?
            AND NOT EXISTS (SELECT 1 FROM mapped_events m WHERE m.event_id = e.event_id)
            AND EXISTS (SELECT 1 FROM icd_map m
                        WHERE m.normalized_code = e.normalized_code AND m.vocabulary = ?)
        """, [vocabulary, sibling]).fetchone()[0]
        share = rescued / stats["unmapped"]
        stats["sibling_vocabulary"] = sibling
        stats["unmapped_events_that_would_map_as_sibling"] = rescued
        stats["share_of_unmapped_rescued_by_sibling"] = share
        if share > 0.05:
            print(f"phecodex-map: warning: {vocabulary} looks mislabelled. "
                  f"{stats['unmapped']:,} of {stats['events']:,} {vocabulary} events "
                  f"({stats['unmapped_rate']:.1%}) did not map, and {share:.1%} of those "
                  f"({rescued:,} events) WOULD map if they were labelled {sibling}. A correctly "
                  f"labelled extract sits near 1%. Check which ICD-10 your source actually uses "
                  f"-- UK Biobank is WHO ICD10, not ICD10CM -- and re-run; do not relabel on the "
                  f"unmapped rate alone.", file=sys.stderr)
    return by_vocabulary
