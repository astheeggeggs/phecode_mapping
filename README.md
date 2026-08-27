# PhecodeX consortium mapper

`phecodex-map` applies a versioned PhecodeX mapping release to cohort-level
ICD events and produces a binary phenotype matrix plus aggregate QC outputs.
Mapping is performed locally at each biobank; participant-level data does not
need to leave the secure environment.

The recommended analyst path is the high-level `run` command. It validates the
inputs, maps events by exact match against the published PhecodeX map, applies
sex restrictions and thresholds, and records checksums and configuration in
`audit.json`.

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
  --release releases/phecodex-1.1
```

Validate inputs without mapping:

```bash
.venv/bin/phecodex-map run \
  --release releases/phecodex-1.1 \
  --cohort cohort.csv \
  --events events.csv \
  --output phecodex_run \
  --preflight-only
```

Run the mapper:

```bash
.venv/bin/phecodex-map run \
  --release releases/phecodex-1.1 \
  --cohort cohort.csv \
  --events events.csv \
  --output phecodex_run
```

The command refuses to overwrite an existing output directory.
The standard workflow automatically applies the bundled recommended phenotype
exclusions for `Symptoms`, `Neonatal`, `Infections`, and three administrative
pregnancy-encounter phecodes. Supply `--exclude-phenotypes <file>` to use a
reviewed alternative policy.

To specify the bundled policy explicitly:

```bash
.venv/bin/phecodex-map run \
  --release releases/phecodex-1.1 \
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
`--case-rule two-dates`, which additionally requires every date to be ISO
`YYYY-MM-DD`; a run refuses to start otherwise rather than silently treating
unparseable dates as absent.

Under `two-dates`, a person carrying a phecode on only one date is neither a case
nor a control: they are non-evaluable (blank in the matrix), because a single code
is ambiguous evidence rather than evidence of absence. `phecode_counts` reports
them as `subthreshold_control_count`. This follows PheTK, whose control set
excludes everyone with any occurrence of the phecode regardless of count. The
default `any-event` rule is unaffected, since one event already makes a case. Supported vocabularies are `ICD9CM`, `ICD10`,
`ICD10CM`, and `SNOMED`. Codes are normalized before matching. Events for
people absent from the cohort are reported during preflight.

For UK Biobank's wide phenotype export, use
[`scripts/prepare_ukb_for_mapping.R`](scripts/prepare_ukb_for_mapping.R) in
the secure environment. It requires explicit female and male encodings and
writes compressed cohort and event files. Do not commit its outputs.

## Outputs

A run produces:

```text
phenotype_matrix.csv.gz
phenotype_matrix.parquet
phecode_counts.parquet
eligible_phecodes.xlsx
audit.json
unmapped_events.csv
```

The matrix has one row per cohort person, a stable `person_id` column, and one
column per retained PhecodeX trait. Values are `1` for cases, `0` for ordinary
controls, and blank/NA when a person is not evaluable because of a sex
restriction or control exclusion.

Keep the matrix, unmapped events, and any person-level outputs in the secure
environment. Share aggregate counts and `audit.json` only where permitted.
The audit records release and input checksums, row and vocabulary counts,
mapping mode, thresholds, sex handling, and unmapped-event rates.

## Mapping policy

An event maps to a phecode only when its normalized code appears in the release's
PhecodeX map for that vocabulary. There is no inference from parent codes, no
string-prefix matching, and no cross-vocabulary fallback.

This is deliberate. The published PhecodeX map is already unrolled to leaf level
wherever a phecode is assigned, so a code's absence from it is a curation decision
rather than a gap to be filled. The unmapped branches are dominated by trauma
sequelae, iatrogenic complications and status codes whose mapped ancestors are
disease phenotypes — inferring them from a parent would assign, for example,
"retained intraocular foreign body" to "Disorders of globe". Mapping exactly means
every site reproduces the same result from the same published vocabulary, and a
reviewer can check the mapping against
[the PhecodeX vocabulary repository](https://github.com/PheWAS/PhecodeXVocabulary).

Where the published map genuinely lags the current ICD release, the remedy is a
curated, versioned supplement to the map — explicit and auditable — rather than
run-time inference. Codes with no entry are reported in `unmapped_events.csv`;
review that file rather than assuming a low unmapped rate means good coverage.

## Quality control

At minimum, check:

1. the release checksum and mapper version in `audit.json`;
2. cohort/event row counts and vocabulary counts from preflight;
3. unmapped rates separately for each vocabulary;
4. high-frequency codes in `unmapped_events.csv`, which indicate either a stale
   map or a vocabulary the release does not cover;
5. retained phenotype counts and sex-specific phenotype handling;
6. that excluded categories are absent from the retained phenotype list.

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
  --release releases/phecodex-1.1 \
  --external all_by_all_phecodex_summary.csv \
  --output validation_all_by_all
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
is configured to ignore local releases, Athena files, and generated outputs.

## Advanced release building

Consortium maintainers can build a release from official PhecodeX files with
`build-vocabulary`. The command accepts repeated `--phecodex-map` arguments,
preserving separate `ICD10` and `ICD10CM` aliases:

```bash
phecodex-map build-vocabulary \
  --phecodex-map phecodeX_unrolled_ICD_CM.csv \
  --phecodex-map phecodeX_unrolled_ICD_WHO.csv \
  --phecodex-info phecodeX_info_1.1_with_sex.csv \
  --output releases/phecodex-1.1
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
