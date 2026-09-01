# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Environment (Python >= 3.11)
python -m venv .venv && .venv/bin/pip install -r requirements-lock.txt && .venv/bin/pip install -e '.[test]'

# Tests: 268 tests, ~155s. Tests that shell out to Rscript skip when it is absent.
.venv/bin/pytest -q
.venv/bin/pytest tests/test_case_rule.py -q                    # one module
.venv/bin/pytest tests/test_case_rule.py::test_name -q          # one test
.venv/bin/pytest -k "sex and not metadata" -q

# Maintainer: build a release directory from published PhecodeX files
.venv/bin/phecodex-map build-vocabulary \
  --phecodex-map phecodeX_unrolled_ICD_CM.csv --phecodex-map phecodeX_unrolled_ICD_WHO.csv \
  --phecodex-info phecodeX_info_1.1_with_sex.csv \
  --athena-dir athena --recover-unmapped \
  --recovery-adjudication data/icd_recovery_adjudication.csv \
  --icd-only --output releases/phecodex-1.1-analyst

# Maintainer: bundle a release for other sites (refuses any release carrying SNOMED tables)
.venv/bin/python scripts/package_distribution.py --release releases/... --output distributions/....tar.gz

# Analyst: verify, preflight, map
.venv/bin/python scripts/verify_release.py --release release
.venv/bin/phecodex-map run --release release --cohort cohort.csv --events events.csv \
  --output phecodex_run [--preflight-only]

# Post-run checks (aggregate output only)
.venv/bin/python scripts/check_prevalence.py --run phecodex_run --release release --cohort cohort.csv --out prevalence.csv
.venv/bin/python scripts/reconcile_attrition.py --run phecodex_run --release release --cohort cohort.csv
```

`build-vocabulary`, `run`, and `map-phecodes` all refuse to write into an existing output
directory. `run` = preflight + `map-phecodes` + an enriched `audit.json`, and is the documented
analyst path; `map-phecodes` is the same mapping without the preflight block.

## Architecture

Two phases with a checksummed artefact between them, because a federated consortium needs every
site to make the same mapping decisions once, at build time.

1. **`vocabulary.build_vocabulary`** (maintainer, once) turns published PhecodeX CSVs — plus an
   optional Athena/OMOP extract — into a *release directory*: `icd_map.{parquet,csv}`,
   `phecode_info.*`, optional `snomed_map.*`, `phecodex_reference_maps.xlsx`,
   `phetk_custom_map_icd10{,cm}.csv`, `recovered_codes.csv`, and `manifest.json`.
2. **`mapper.map_phecodes`** (each site, per cohort) joins cohort events against that release and
   writes a *run directory*: `phenotype_matrix.{csv.gz,parquet}`, `phecode_counts.*`,
   `person_phecodes.parquet`, `unmapped_events.csv`, `eligible_phecodes.xlsx`, `audit.json`.
3. **`validation.validate_phecodex_counts`** compares a run's aggregate counts against an external
   aggregate summary (e.g. All by All) — a plausibility review, not an exact test.

`cli.py` is a thin argparse layer; `workflow.py` wires preflight + mapping; `io.py` holds the
DuckDB connection and checksumming; `normalize.py` is the single definition of code
normalization (punctuation-stripping only — it never derives parent codes); `retention.py`
is the single definition of sex-evaluability, the evaluable denominator and the
case/control retention rule; `diagnostics.py` holds post-mapping reporting that decides
nothing (the per-vocabulary unmapped tallies and the mislabelling heuristic).

Everything computational is DuckDB SQL over file relations, not dataframes. `io.relation_for`
turns a path into a `read_csv_auto(...)`/`read_parquet(...)` FROM clause, so CSV and Parquet
inputs go down the same path. There is no pandas dependency.

### Invariants that constrain how you change this code

- **Mapping is exact-match only.** No parent-code inference, prefix matching, or cross-vocabulary
  fallback at run time. Gaps are fixed at build time via `--recover-unmapped` (evidence-based,
  recorded in `recovered_codes.csv`), never by a run-time heuristic. A hierarchy fallback was
  removed deliberately; do not reintroduce one.
- **`ICD10` (WHO) and `ICD10CM` are distinct vocabularies** and the events file's `vocabulary`
  column is ground truth. UK Biobank is `ICD10`. A release's `ICD10CM` map may be genuine
  ICD-10-CM or a WHO map someone relabelled before the build, and the label alone cannot tell
  the two apart, so releases are not interchangeable — the manifest's `vocabularies` block
  records which source file each label came from.
- **Byte-reproducibility.** Two builds from identical inputs must produce identical files. Every
  `COPY` is written in a fixed order and `io.pin_workbook_timestamps` strips wall-clock time out of
  `.xlsx` output. `manifest.json`'s `created_at_utc` is the only permitted non-determinism. Adding
  an unordered write breaks cross-site digest comparison.
- **`io.connect()` pins `TimeZone = 'UTC'`.** Casting a tz-aware timestamp to DATE otherwise goes
  through the machine's local zone and gives two sites different case sets under
  `--case-rule two-dates`. `audit.json` records `analysis_timezone`.
- **The manifest, not the filesystem, decides what a release contains.** `map_phecodes` reads
  `snomed_map.parquet` only when `manifest.json["artifacts"]` records it, and errors if the file is
  present but unrecorded.
- **Guards refuse rather than degrade.** The recurring defect class here is a run that completes
  "successfully" with silently wrong numbers: an entirely-empty `event_date` column, a non-ISO
  date, a numeric `code` in Parquet, a `person_id` type mismatch that joins nothing, a blank
  `match_value` that makes `NOT IN` UNKNOWN for every row, an exclusion rule whose casing matches
  nothing. Each has a targeted guard with a long comment explaining the failure it saw. Those
  comments are the specification — preserve them, and when adding a check, use the same semantics
  as the code it guards (e.g. join on `CAST(... AS VARCHAR)` because the pipeline does).
- **`two-dates` means two distinct dates among events mapping to that phecode** — not two visits,
  not two different codes. A single dated occurrence is neither case nor control: it is NA in the
  matrix and `subthreshold_control_count` in the counts, following PheTK.
- **Sex handling is derived, never asserted.** `release_has_sex_metadata` false means *no* phecode
  is restricted and every sex-specific trait is scored against the whole cohort.
- **`scripts/` uses the package, it does not re-implement it.** Every script that touches
  DuckDB goes through `io.connect()` (which pins `TimeZone='UTC'`) and `io.relation_for()`
  / `io.quote()`, and scores phecodes with `retention.py` rather than its own copy of the
  rule. All four had drifted into private reimplementations: a hardcoded `read_csv_auto`
  crashed `check_prevalence.py` on a Parquet cohort *after* printing its bands and
  *before* its sharpest check, and three spellings of the evaluable denominator needed a
  whole script to police them. `tests/test_distribution.py` runs every bundled script's
  `--help` from an extracted bundle so an import that resolves only in the repo fails.
- Phenotype exclusions are **applied by default** by `run` from
  `src/phecodex_mapper/data/recommended_exclusions.csv`; matching is case-sensitive.

## Tests

`tests/conftest.py` provides two release fixtures: `release` (minimal, no `phecode_info`) and
`full_release` (sex + category, the only one that reaches the documented analyst path).
`test_mutation_guards.py` and `test_mutation_survivors.py` exist solely to kill named mutations
that survived earlier suites — their docstrings list the mutation each test targets. When adding a
guard test, break the behaviour and confirm the test fails; a guard that passes under the mutation
is vacuous.

## Data governance

`.gitignore` excludes `athena/`, `scratchpad_ukb/`, `*deid*.csv`, `releases/`, `runs/`, `reports/`,
`distributions/`, `hierarchy_sources/`, and `phecodeX_*.csv` — several of these exist in the working
tree but must never be committed. Participant-level data, Athena/SNOMED sources, and generated
releases stay out of version control; analyst bundles are published as GitHub Releases. Athena-derived
content is separately licensed and must not be redistributed, which is why shared releases are built
`--icd-only`. Scripts that touch real data (`scripts/*.R`, `check_deidentification.py`,
`check_prevalence.py`) are designed to emit aggregate counts only.

Prose documentation is split deliberately: `README.md` above "What the map contains" is the analyst
path, below it is maintainer/background material; `ANALYST_GUIDE.md` covers UK Biobank extraction,
containers, and the one-off checks. Keep changes on the correct side of that split.
