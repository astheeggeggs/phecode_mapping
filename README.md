# PhecodeX consortium mapper

`phecodex-map` applies a versioned PhecodeX mapping release to cohort-level
ICD events and produces a binary phenotype matrix plus aggregate QC outputs.
Mapping is performed locally at each biobank; participant-level data does not
need to leave the secure environment.

The recommended analyst path is the high-level `run` command. It validates the
inputs, uses the approved hierarchy-aware ICD policy, applies sex restrictions
and thresholds, and records checksums and configuration in `audit.json`.

For detailed release-building, UK Biobank preparation, validation, and
container instructions, see [ANALYST_GUIDE.md](ANALYST_GUIDE.md).

## Install

Python 3.11 or newer is required. For a pinned installation:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/pip install .
```

You can also use the supplied Docker or Apptainer/Singularity definitions;
see [ANALYST_GUIDE.md](ANALYST_GUIDE.md).

## Standard workflow

Verify the supplied release before use:

```bash
.venv/bin/python scripts/verify_release.py \
  --release releases/phecodex-1.1-hierarchy \
  --hierarchy-aware
```

Validate inputs without mapping:

```bash
.venv/bin/phecodex-map run \
  --release releases/phecodex-1.1-hierarchy \
  --cohort cohort.csv \
  --events events.csv \
  --output phecodex_run \
  --preflight-only
```

Run the mapper:

```bash
.venv/bin/phecodex-map run \
  --release releases/phecodex-1.1-hierarchy \
  --cohort cohort.csv \
  --events events.csv \
  --output phecodex_run
```

The command refuses to overwrite an existing output directory. Use
`--exact-only` when reproducing the exact-match compatibility baseline.
The standard workflow automatically applies the bundled recommended phenotype
exclusions for `Symptoms`, `Neonatal`, `Infections`, and three administrative
pregnancy-encounter phecodes. Supply `--exclude-phenotypes <file>` to use a
reviewed alternative policy.

To specify the bundled policy explicitly:

```bash
.venv/bin/phecodex-map run \
  --release releases/phecodex-1.1-hierarchy \
  --cohort cohort.csv \
  --events events.csv \
  --exclude-phenotypes src/phecodex_mapper/data/recommended_exclusions.csv \
  --output phecodex_run
```

## Input contract

The cohort must contain one row per person:

```text
person_id,sex
```

`person_id` must be non-null and unique. `sex` must be `Male`, `Female`, or
blank. Additional columns are ignored.

The events file must contain one row per event:

```text
person_id,code,vocabulary,event_date
```

`event_date` is optional for the default `any-event` rule, but required for
`--case-rule two-dates`. Supported vocabularies are `ICD9CM`, `ICD10`,
`ICD10CM`, and `SNOMED`. Codes are normalized before matching. Events for
people absent from the cohort are reported during preflight.

For UK Biobank's wide phenotype export, use
[`scripts/prepare_ukb_for_mapping.R`](scripts/prepare_ukb_for_mapping.R) in
the secure environment. It requires explicit female and male encodings and
writes compressed cohort and event files. Do not commit its outputs.

## Outputs

The standard hierarchy-aware run produces:

```text
phenotype_matrix_hierarchy.csv.gz
phenotype_matrix_hierarchy.parquet
phecode_counts_hierarchy.parquet
eligible_phecodes_hierarchy.xlsx
audit.json
unmapped_events_hierarchy.csv
hierarchy_fallbacks.csv
```

The matrix has one row per cohort person, a stable `person_id` column, and one
column per retained PhecodeX trait. Values are `1` for cases, `0` for ordinary
controls, and blank/NA when a person is not evaluable because of a sex
restriction or control exclusion.

Keep the matrix, unmapped events, and any person-level outputs in the secure
environment. Share aggregate counts and `audit.json` only where permitted.
The audit records release and input checksums, row and vocabulary counts,
mapping mode, thresholds, sex handling, and unmapped-event rates.

## Mapping policies

Exact matching is always retained as the compatibility baseline. In the
default hierarchy-aware policy, an event first attempts an exact match. If
that fails, it may inherit mappings from the most specific explicitly
validated parent in the same vocabulary. No string-prefix inference or
cross-vocabulary fallback is used. SNOMED is outside the ICD hierarchy logic.

Hierarchy-aware releases contain `icd_hierarchy.parquet` and record reference
checksums and source versions in `manifest.json`. Review
`hierarchy_fallbacks.csv` and compare exact versus hierarchy-aware counts:

```bash
python scripts/compare_exact_hierarchy.py \
  --run phecodex_run \
  --output phecodex_run_hierarchy_comparison
```

The official unrolled PhecodeX map expands phecodes to mapped descendant ICD
codes; it does not authorize inferring every unmapped descendant from a
parent. Hierarchy-aware mapping therefore requires a separate, versioned
parent-child reference for `ICD9CM`, `ICD10`, and `ICD10CM`.

## Quality control

At minimum, check:

1. the release checksum and mapper version in `audit.json`;
2. cohort/event row counts and vocabulary counts from preflight;
3. unmapped rates separately for each vocabulary;
4. exact versus hierarchy-aware fallback counts;
5. high-frequency rows in `hierarchy_fallbacks.csv`;
6. retained phenotype counts and sex-specific phenotype handling;
7. that excluded categories are absent from the retained phenotype list.

For aggregate cross-biobank plausibility checks, export only aggregate
PhecodeX counts from an authorized source such as the All by All public
summary. The validator accepts CSV or Parquet with:

```text
phecode,description,sex,ancestry,case_count,control_count,sample_count,source,source_version
```

Run it with:

```bash
.venv/bin/phecodex-map validate-phecodex \
  --run phecodex_run \
  --release releases/phecodex-1.1-hierarchy \
  --external all_by_all_phecodex_summary.csv \
  --output validation_all_by_all \
  --hierarchy-aware
```

This is a plausibility comparison, not an exact expected-count test. The
report flags denominator, ancestry, sex-stratum, version, and missing-trait
differences for manual review. Never export participant-level records from
an external resource for this comparison.

## Privacy and data governance

Do not commit or redistribute cohort/event files, phenotype matrices,
person-level case files, generated runs, participant-level reports, Athena or
UMLS/SNOMED source files, raw download archives, or credentials.

The shared analyst distribution is intended to contain the mapper, an ICD-only
release, its manifest/checksum, documentation, and synthetic fixtures. Keep
licensed vocabulary sources at the site that built the release. The repository
is configured to ignore local releases, hierarchy sources, Athena files, and
generated outputs.

## Advanced release building

Consortium maintainers can build a release from official PhecodeX files with
`build-vocabulary`. The command accepts repeated `--phecodex-map` arguments,
preserving separate `ICD10` and `ICD10CM` aliases. Hierarchy references are
provided as `VOCABULARY:path`:

```bash
phecodex-map build-vocabulary \
  --phecodex-map phecodeX_unrolled_ICD_CM.csv \
  --phecodex-map phecodeX_unrolled_ICD_WHO.csv \
  --phecodex-info phecodeX_info_1.1_with_sex.csv \
  --icd-hierarchy ICD9CM:/secure/refs/icd9cm_hierarchy.csv \
  --icd-hierarchy ICD10:/secure/refs/icd10_hierarchy.csv \
  --icd-hierarchy ICD10CM:/secure/refs/icd10cm_hierarchy.csv \
  --output releases/phecodex-1.1-hierarchy
```

Use the official [PhecodeX vocabulary repository](https://github.com/PheWAS/PhecodeXVocabulary)
for source maps and record their checksums. The release builder records source
paths, versions, row counts, and checksums in `manifest.json`. See
[ANALYST_GUIDE.md](ANALYST_GUIDE.md) for packaging, UK Biobank extraction,
container execution, and downsampling examples. The lower-level
`map-phecodes` and `validate-phecodex` commands remain available for advanced
users.

## Development

```bash
.venv/bin/pytest -q
```

The repository contains only synthetic fixtures and aggregate/public metadata;
participant-level data must remain outside version control.
