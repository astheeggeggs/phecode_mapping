#!/usr/bin/env bash
# Rebuild a phecodeX matrix from WHO ICD-10 events, then export the aggregate phecodeX x ICD-10
# agreement table and the per-pair diagnostic.  Runs INSIDE the secure environment that holds the
# cohort and events files (README, Input contract); nothing person-level leaves.
#
# Usage:  PY=.venv/bin/python3.11 bash scripts/rebuild_and_agreement.sh <release_dir> <cohort.csv.gz> <events.csv.gz> <out_dir>
# (<out_dir> must not exist yet: the mapper refuses to overwrite phecodex_run)
# Needs: the bundle's .venv (phecodex-map) with pandas, numpy, scipy installed in it
#        (pip install pandas numpy scipy), or set PY=/path/to/python that has them.
set -euo pipefail
REL=${1:?release dir}; COHORT=${2:?cohort.csv.gz}; EVENTS=${3:?events.csv.gz}; OUT=${4:?out dir}
PY=${PY:-.venv/bin/python}; MAP=${MAP:-.venv/bin/phecodex-map}
mkdir -p "$OUT"

echo "== 1. release integrity and which ICD-10 the release carries"
"$PY" scripts/verify_release.py --release "$REL"
"$PY" - "$REL" <<'PYEOF'
import json, sys
m = json.load(open(f"{sys.argv[1]}/manifest.json"))
for v, d in m["vocabularies"].items(): print(f"   {v}: {d['distinct_codes']} codes from {d['source_files']}")
assert "ICD10" in m["vocabularies"], "release has no WHO ICD10 rows: WHO-coded events would not map at all"
PYEOF

echo "== 2. events must be labelled ICD10 (WHO), never ICD10CM"
"$PY" - "$EVENTS" <<'PYEOF'
import pandas as pd, sys
ev = pd.read_csv(sys.argv[1], usecols=["vocabulary"], dtype=str)
print(ev.vocabulary.value_counts().to_string())
assert "ICD10CM" not in set(ev.vocabulary), "events carry ICD10CM: this pipeline is for WHO ICD-10 events (see README)"
PYEOF

echo "== 3. keep Infections / Symptoms / Neonatal for the agreement run (only the administrative pregnancy codes excluded)"
grep -v '^category,' src/phecodex_mapper/data/recommended_exclusions.csv > "$OUT/exclude_phenotypes_admin_only.csv"
cat "$OUT/exclude_phenotypes_admin_only.csv"

echo "== 4. preflight, then map (any-event; WHO fifth-character parent fallback is the default since 0.2; phecodeX with < 200 cases or controls are dropped)"
"$MAP" run --release "$REL" --cohort "$COHORT" --events "$EVENTS" --output "$OUT/phecodex_run" \
  --exclude-phenotypes "$OUT/exclude_phenotypes_admin_only.csv" --preflight-only
"$MAP" run --release "$REL" --cohort "$COHORT" --events "$EVENTS" --output "$OUT/phecodex_run" \
  --exclude-phenotypes "$OUT/exclude_phenotypes_admin_only.csv"

echo "== 5. audit fields that would otherwise be invisible"
"$PY" - "$OUT/phecodex_run/audit.json" <<'PYEOF'
import json, sys
a = json.load(open(sys.argv[1]))
def get(*ks):
    d = a
    for k in ks:
        d = d.get(k, {}) if isinstance(d, dict) else {}
    return d if d != {} else "n/a"
print("   events_in_file / events (post-join):", get("events_in_file"), "/", get("events"))
print("   mapping policy:", get("mapping_policy"), "| case rule:", get("case_rule"), "| min_cases:", get("min_cases"))
fc = get("icd10_fifth_character")
print("   ICD10 fifth character:", fc if fc == "n/a" else f"{fc['policy']}: {fc['events_matched_via_parent']} events ({fc['distinct_codes_matched_via_parent']} codes) matched via their 4-char parent, by chapter {fc['events_matched_via_parent_by_chapter']}")
print("   sex.release_has_sex_metadata:", get("sex", "release_has_sex_metadata"))
for v, d in (a.get("unmapped_by_vocabulary") or {}).items():
    if isinstance(d, dict): print(f"   {v}: unmapped_rate={d.get('unmapped_rate')}  share_of_unmapped_rescued_by_sibling={d.get('share_of_unmapped_rescued_by_sibling')}  (<~1% correct label, ~20% mislabelled)")
print("   exclude_phenotypes.unmatched rules:", get("exclude_phenotypes", "unmatched_phecode_rules"))
PYEOF

echo "== 6. agreement table (aggregate, can leave) -- self-test first"
"$PY" scripts/phecodex_icd_agreement.py --selftest
"$PY" scripts/phecodex_icd_agreement.py --matrix "$OUT/phecodex_run/phenotype_matrix.parquet" --events "$EVENTS" --cohort "$COHORT" \
  --out "$OUT/phecodex_icd_agreement.tsv.gz"

echo "== 7. diagnostic for pairs that fail containment under exact fifth-character matching (aggregate, can leave)"
"$PY" scripts/phecodex_icd_agreement.py --matrix "$OUT/phecodex_run/phenotype_matrix.parquet" --events "$EVENTS" --cohort "$COHORT" \
  --diagnose MS_703.1=M10,MS_718=M54,MS_715.1=M47,MS_722=M72,SO_376=H43,GI_510=K22,EM_236.1=E66,GI_511=K21 \
  --diagnose-out "$OUT/phecodex_icd_diagnose.tsv"

echo "== 8. quick read of the result: do the failed pairs now contain?"
"$PY" - "$OUT/phecodex_icd_agreement.tsv.gz" <<'PYEOF'
import pandas as pd, sys
d = pd.read_csv(sys.argv[1], sep="\t")
assert d.r.abs().max() <= 1 + 1e-9
pairs = [("MS_703.1","M10"),("MS_718","M54"),("MS_715.1","M47"),("MS_722","M72"),("SO_376","H43"),("EM_236.1","E66"),("GI_511","K21"),("ID_052.32","B02")]
print(d.set_index(["phecodex","code"]).reindex(pairs)[["n_cases","n_code","n_both","frac_cases_with_code","frac_code_in_cases"]].to_string())
print("n_eval distinct values (sex strata only if no NA from exclusions):", sorted(d.n_eval.unique()))
PYEOF

echo "== files that may leave the environment: $OUT/phecodex_icd_agreement.tsv.gz  $OUT/phecodex_icd_diagnose.tsv  $OUT/phecodex_run/audit.json"
