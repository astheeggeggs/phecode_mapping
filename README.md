# PhecodeX consortium mapper

`phecodex-map` builds a reproducible, checksummed PhecodeX mapping release
once, then maps any number of biobank cohorts' ICD-9-CM, ICD-10-CM/ICD-10
(WHO), and (optionally) SNOMED events to phecodes against that release. It
uses the **official PhecodeX unrolled map** for all ICD mappings — it never
infers clinical mappings from code prefixes or regular expressions.

This tool is designed to be run independently at each participating
biobank/site: you build one release centrally (or each site builds its own
from the same official map, for a fully reproducible/auditable result), ship
the release directory, and each site runs `map-phecodes` locally against its
own cohort — no patient-level data needs to leave the site.

- [Who should read what](#who-should-read-what)
- [Install](#install)
- [Quick start](#quick-start)
- [Data governance](#data-governance)
- [Input file formats](#input-file-formats)
- [`map-phecodes` options](#map-phecodes-options)
- [Outputs](#outputs)
- [Multi-biobank rollout checklist](#multi-biobank-rollout-checklist)
- [PheTK comparison](#phetk-comparison)
- [Independent SNOMED validation](#independent-snomed-validation)
- [Development](#development)

## Who should read what

- **Running this at a new biobank/site?** Read [Data governance](#data-governance),
  [Input file formats](#input-file-formats), and the
  [Multi-biobank rollout checklist](#multi-biobank-rollout-checklist). You
  almost never need to touch `build-vocabulary` yourself — use the release
  directory the study coordinator gives you.
- **Building/maintaining the shared release for the consortium?** Read
  [Quick start](#quick-start) and the `--athena-dir`/SNOMED sections below.
- **Validating that this tool's mapping is trustworthy?** Read
  [PheTK comparison](#phetk-comparison) and
  [Independent SNOMED validation](#independent-snomed-validation).

## Install

Requires Python 3.11+.

```bash
pip install -e '.[test]'
```

## Quick start

Two commands, run in order:

1. **`build-vocabulary`** (run once per mapping release — typically by
   whoever coordinates the consortium/analysis) — turns the official
   PhecodeX map (and, optionally, an Athena/OMOP vocabulary extract) into a
   versioned, checksummed release directory.
2. **`map-phecodes`** (run once per biobank/cohort) — maps that biobank's
   own cohort's events against the shared release and writes per-phecode
   case/control counts. This is the command each site actually runs.

```bash
phecodex-map build-vocabulary \
  --phecodex-map phecodeX_unrolled_ICD_CM.csv \
  --phecodex-info phecodeX_info.csv \
  --output releases/phecodex-1.1

phecodex-map map-phecodes \
  --release releases/phecodex-1.1 \
  --cohort cohort.csv \
  --events events.csv \
  --output runs/site-a
```

Both commands accept CSV or Parquet for every input file, and refuse to
overwrite an existing `--output` directory (delete it first if you're
re-running).

## Data governance

- **The `--release` directory** (built by `build-vocabulary`) contains no
  patient-level data — only mapping tables derived from the public PhecodeX
  files and, if used, publicly-licensed vocabulary metadata from Athena.
  It is safe to share across sites.
- **`--athena-dir`** (used only by `build-vocabulary`, to enable SNOMED
  support) points at an authorized OMOP/Athena vocabulary extract. Athena
  data is licensed (UMLS/SNOMED International terms) — **do not commit it to
  version control or redistribute it outside your license terms.** The
  `.gitignore` in this repo excludes an `athena/` directory for this reason;
  keep that convention if you clone or fork this tool.
- **`--cohort`, `--events`, `--control-exclusions`** (used by
  `map-phecodes`) are your site's own patient-level data. They are never
  written back into the release directory, never leave your machine, and
  this tool makes no network calls. Still, treat the `--output` run
  directory as patient-level: `person_phecodes.parquet` and
  `unmapped_events.csv` both contain per-person rows.
- If you need a de-identified/synthetic fixture to test this pipeline
  before running it on real data, see `scripts/deidentify_ukb_for_testing.R`
  (built for UK Biobank's hospital ICD extract shape, but the
  scramble-and-jitter approach generalizes to other biobanks' extracts) —
  never commit its output either (the `.gitignore` pattern `*deid*.csv`
  covers this by convention, but rename thoughtfully if you deviate from it).

## Input file formats

### `--phecodex-map` (required, `build-vocabulary`)

The official PhecodeX unrolled ICD map, unmodified. Get it from
[PheWAS/PhecodeXVocabulary](https://github.com/PheWAS/PhecodeXVocabulary):

- `phecodeX_unrolled_ICD_CM.csv` (v1.1) — US ICD-9-CM + ICD-10-CM. Use this
  if your biobank's diagnosis codes are already US clinical-modification
  codes (e.g. most US EHR-derived cohorts, *All of Us*).
- `phecodeX_unrolled_ICD_WHO.csv` (v1.0) — WHO ICD-10 only (no ICD-9). Use
  this, or a hybrid built from both files (see note below), if your
  biobank's diagnosis codes are WHO ICD-10 rather than the US clinical
  modification — **this is the case for UK Biobank and most non-US
  biobanks.** WHO ICD-10 and US ICD-10-CM are different vocabularies with
  overlapping but non-identical codes; mapping WHO codes against the CM file
  directly will silently under-map. There's no WHO ICD-9 map published at
  all, so a WHO-coded biobank with pre-ICD-10-era records has no
  authoritative source for those codes — the closest available approximation
  is the genuine ICD-9-CM rows from the CM file.

| column | required | notes |
|---|---|---|
| `phecode` | yes | |
| `ICD` | yes | source ICD code, kept verbatim for traceability |
| `vocabulary_id` | yes | `ICD9CM` or `ICD10CM`; other values are dropped |

### `--phecodex-info` (optional, `build-vocabulary`)

Phecode metadata (description, category, sex restriction), also from
PheWAS/PhecodeXVocabulary. If omitted, all phecodes are written with
`sex="Both"` and blank description/category.

| column | required | notes |
|---|---|---|
| `phecode` | yes | join key; without it the file is copied through but not joined |
| `sex` | no | defaults to `"Both"` |
| `phecode_string` | no | human-readable description; defaults to `""` |
| `category` | no | defaults to `""` |

### `--athena-dir` (optional, `build-vocabulary`)

Path to an authorized OMOP/Athena vocabulary extract, used only to bridge
SNOMED codes to the ICD map above. Must contain `CONCEPT.csv` and
`CONCEPT_RELATIONSHIP.csv` in the standard OMOP CDM shape (tab-delimited
despite the `.csv` extension — this tool auto-detects the delimiter). At
minimum, download the **SNOMED**, **ICD9CM**, **ICD10CM**, and **ICD10
(WHO)** vocabularies from [Athena](https://athena.ohdsi.org/) — the WHO
ICD-10 vocabulary is required to bridge SNOMED codes that only cross-map
through WHO ICD-10 rather than the US clinical modification (common outside
the US). **Athena data and credentials must never be committed to this
repository.** Without `--athena-dir`, SNOMED events in `map-phecodes` are
left unmapped.

Note that PhecodeX itself publishes no SNOMED mapping at all — SNOMED
support in this tool exists entirely because of this Athena bridge
(SNOMED → Athena `Maps to` relationship → ICD9CM/ICD10CM/ICD10(WHO) → the
official PhecodeX ICD map). Its coverage is therefore capped by what Athena
and PhecodeX jointly cover, not by anything this tool adds — see
[Independent SNOMED validation](#independent-snomed-validation) for a
realistic sense of that coverage.

### `--cohort` (required, `map-phecodes`)

One row per person to include in the run.

| column | required | notes |
|---|---|---|
| `person_id` | yes | must be non-null and unique |
| `sex` | no | `"Male"` or `"Female"` (case-insensitive). Used only to NA out sex-restricted phecodes in `phenotype_matrix` (see [Outputs](#outputs)); everything else in the pipeline ignores it. Omit it and sex-restricted phecodes simply aren't NA'd (see the warning this produces, below) |

Any other columns are ignored.

### `--events` (required, `map-phecodes`)

One row per clinical event.

| column | required | notes |
|---|---|---|
| `person_id` | yes | events for people not in `--cohort` are ignored |
| `code` | yes | source code, as recorded (punctuation/whitespace is stripped automatically before matching) |
| `vocabulary` | yes | `ICD9CM`, `ICD10CM`, or `SNOMED`; anything else is left unmapped |
| `event_date` | only for `--case-rule two-dates` | any date DuckDB can parse, e.g. `YYYY-MM-DD` |

If your source data has UK Biobank's hospital-episode shape (one row per
person with wide `<field>-<instance>.<array>` or `f.<field>.<instance>.<array>`
columns rather than one row per event), see
`scripts/deidentify_ukb_for_testing.R`'s `extract_ID_and_ICD_UKB()` for a
worked example of reshaping that into this `person_id, code, vocabulary`
long format (it also de-identifies, if you need a test fixture — see
[Data governance](#data-governance)).

### `--exclude-phenotypes` (optional, `map-phecodes`)

Drops whole phecodes from **every** output (`phecode_counts`,
`person_phecodes`, `eligible_phecodes`, `phenotype_matrix`) — not just from
other phecodes' control pools like `--control-exclusions` below. Use this to
keep phenotypes with poor genetic construct validity for your analysis type
out of the results entirely, rather than filtering them post hoc from
thousands of output rows.

| column | required | notes |
|---|---|---|
| `match_type` | yes | `"category"` (matched against the release's `phecode_info` `category` column — requires the release to have been built with a `--phecodex-info` file that has one) or `"phecode"` (exact phecode) |
| `match_value` | yes | the category name or phecode to match |

`phecodex_mapper/data/recommended_exclusions.csv` (bundled with this
package) is a **starting point, not a complete answer** — for a rare-variant
association analysis, it drops `Symptoms` (non-specific sign/symptom codes)
and `Neonatal` (case status mostly reflects gestational age/obstetrics, not
the child's own germline burden) wholesale, plus three purely administrative
pregnancy-encounter phecodes (`PP_P001`–`PP_P003`). It deliberately does
**not** attempt to hand-pick which `Infections` or clinical `Pregnancy`
phecodes to drop — those need case-by-case clinical judgment (e.g. acute/
environmentally-driven infections have little heritable signal, but
monogenic-susceptibility infection phenotypes and genuinely heritable
pregnancy complications like pre-eclampsia do) that shouldn't be baked into
a shared default without review. Copy it and extend it for your own study
rather than passing it through unmodified.

`audit.json`'s `exclude_phenotypes.phecodes_excluded` records how many
phecodes this removed, so a run's phenotype list can always be traced back
to which filter produced it.

### `--control-exclusions` (optional, `map-phecodes`)

Removes phecode-inappropriate people from the control pool for a given
phecode (e.g. excluding people with a related diagnosis code or phecode from
counting as controls). **A case is never excluded** — case status always
takes precedence over an exclusion.

| column | required | notes |
|---|---|---|
| `phecode` | yes | the phecode this exclusion applies to |
| `exclusion_type` | yes | `"phecode"` (match another mapped phecode) or `"code"` (match a raw source code) |
| `exclusion_value` | yes | the phecode or code to match, per `exclusion_type` |
| `version` | no | recorded in `audit.json` for provenance; if omitted, `exclusion_version` is `null` |

## `map-phecodes` options

| flag | default | meaning |
|---|---|---|
| `--case-rule` | `any-event` | `any-event`: one mapped event makes a case. `two-dates`: needs mapped events on **≥2 distinct dates** for the same phecode. |
| `--min-cases` | `200` | phecodes are flagged `retained=false` below this case count |
| `--min-controls` | `200` | phecodes are flagged `retained=false` below this control count (after exclusions) |
| `--max-unmapped-rate` | `1.0` | run fails with an error if the fraction of events that couldn't be mapped exceeds this |

## Outputs

`build-vocabulary --output <release>/`:

- `icd_map.parquet` / `icd_map.csv` — the cleaned ICD→phecode map used by `map-phecodes`
- `snomed_map.parquet` / `snomed_map.csv` — SNOMED→phecode bridge (only if `--athena-dir` was given)
- `phecodex_reference_maps.xlsx` — human-readable copy of both maps
- `phetk_custom_map.csv` — see [PheTK comparison](#phetk-comparison) below
- `manifest.json` — tool version, input file paths/SHA-256 checksums, and row counts

`map-phecodes --output <run>/`:

- `phecode_counts.parquet` / `.csv` — one row per phecode: `case_count`, `control_count_before_exclusions`, `excluded_control_count`, `control_count_after_exclusions`, `retained`
- `person_phecodes.parquet` — one row per `(person_id, phecode)` case (long format)
- `eligible_phecodes.xlsx` — `phecode_counts` rows filtered to `retained=true`, for hand-off to analysts
- `phenotype_matrix.parquet` / `.csv.gz` — wide person × phecode matrix, gzip-compressed CSV / zstd-compressed Parquet (see below)
- `unmapped_events.csv` — events that didn't match the vocabulary map, for QC
- `audit.json` — timestamp, case rule, thresholds, exclusion version, unmapped rate, phenotype-matrix summary, and the release's manifest checksum, so a run can always be traced back to the exact release and inputs that produced it

### `phenotype_matrix`

One row per person in `--cohort`, one column per **retained** phecode (i.e.
the same set as `eligible_phecodes.xlsx`: `case_count >= --min-cases` *and*
`control_count_after_exclusions >= --min-controls`), for hand-off to
downstream analysis tools (e.g. SAIGE, PLINK, regression) that expect a
dense phenotype table rather than the long-format `person_phecodes.parquet`.
Each cell is one of:

- `1` — the person is a case for that phecode
- `0` — the person is an ordinary control for that phecode
- *(blank/NA)* — the person isn't evaluable as either for that phecode,
  because either:
  - the phecode is sex-restricted (per `--phecodex-info`'s `sex` column at
    build time) and this person's `--cohort` `sex` doesn't match (or is
    missing/unspecified), **or**
  - `--control-exclusions` removes this person from that phecode's control
    pool and they aren't already a case (cases are never excluded)

If any retained phecode is sex-restricted but `--cohort` has no `sex`
column, `map-phecodes` prints a warning and leaves that phecode's column
un-NA'd for everyone (opposite-sex people get `0` instead of blank) —
`audit.json`'s `phenotype_matrix.sex_restricted_phecodes_treated_as_unrestricted`
records how many columns this affected, so this can't silently pass
unnoticed. Add a `sex` column to `--cohort` to fix it. Note that the
official PhecodeX 1.1 `phecodeX_info.csv` does not itself include a `sex`
column — sex-restriction is only applied if your `--phecodex-info` source
provides one (e.g. a hand-curated addition for phecodes like
pregnancy/prostate-specific ones); without it, no phecode is treated as
sex-restricted and this feature is a no-op.

Because this is a dense matrix, its size scales with cohort size × number
of retained phecodes. Verified end-to-end against the real UK Biobank-scale
fixture used elsewhere in this README (336,304 people): building the
default-threshold matrix (1,112 retained phecodes at `--min-cases 200
--min-controls 200`) took under a minute and produced a 7.2MB
zstd-compressed Parquet file / 8.5MB gzip-compressed CSV; per-column sums
were spot-checked against `phecode_counts.parquet`'s `case_count` and
matched exactly. The initial implementation used a single dense
`cohort CROSS JOIN retained_phecodes` plus DuckDB's `PIVOT`, which was
**not** viable at this scale (unbounded — DuckDB's `PIVOT` re-scans its
source once per pivot column, and the cross join itself is the same
hundreds-of-millions-of-rows shape this tool's `phecode_counts` computation
also deliberately avoids); the shipped version instead builds a sparse
per-(person, phecode) table sized
to cases + exclusions, batches column construction (200 phecodes per
batch) to bound peak memory/temp-disk regardless of cohort size, and
stitches the batches back together with cheap `USING (person_id)` joins.

## Multi-biobank rollout checklist

If you're standing this up at a new site as part of a multi-biobank study:

1. Confirm your site's native diagnosis-code vocabulary before picking a
   `--phecodex-map` file — WHO ICD-10 (most non-US biobanks) vs. US
   ICD-10-CM (US EHR-derived cohorts) are different vocabularies; see the
   [`--phecodex-map`](#--phecodex-map-required-build-vocabulary) note above.
   Get this wrong and events will silently under-map rather than error.
2. Get the shared release directory from your study coordinator (or build
   your own from the same official PhecodeX source files + Athena export,
   if the study wants each site to build independently for auditability —
   `manifest.json`'s checksums let you confirm two sites' releases are
   built from byte-identical inputs).
3. Reshape your site's raw extract into the `person_id, code, vocabulary[,
   event_date]` long format described in
   [`--events`](#--events-required-map-phecodes). This is almost always the
   biggest real effort in onboarding a new site, since every biobank's raw
   export shape differs.
4. Run `map-phecodes` locally. No patient-level data needs to leave your
   site — the release directory has none, and this tool makes no network
   calls.
5. Check `audit.json`'s `unmapped_rate` and inspect `unmapped_events.csv`
   for anything unexpected (e.g. a vocabulary mismatch from step 1 usually
   shows up here as a much higher rate than other sites).
6. Send back `phecode_counts.parquet`/`.csv` (and `audit.json`, so the
   coordinator can verify everyone ran the same release) — not raw events.

## PheTK comparison

[PheTK](https://github.com/nhgritctran/PheTK) is the reference Python
toolkit for phecode mapping (used on *All of Us* and similar platforms). This
package does not depend on or bundle PheTK (which is GPL-3.0-licensed) — the
`phetk` extra and `scripts/compare_phetk.py` exist only to let you verify
parity yourself, in a separate environment.

|  | `phecodex-mapper` (this repo) | PheTK |
|---|---|---|
| ICD event columns | `person_id`, `code`, `vocabulary`, `event_date` | `person_id`, `ICD`, `vocabulary_id`, `date` (PheTK's custom-platform ICD file) |
| Phecode map columns | `phecode`, `ICD`, `vocabulary_id` (official unrolled map, used as-is) | `phecode`, `ICD` required; `flag` (9/10), `sex`, `exclude_range` optional, for its custom-map mode |
| Default map version | PhecodeX 1.1, built by you from the official release CSV | Bundles PhecodeX 1.0 |
| Case rule | `any-event` or `two-dates` (≥2 distinct dates) | count-based (`min_code_count`) by default |
| Control exclusion logic | phecode- or code-based exclusion list, cases always exempt | phecode-family exclusion ranges baked into its default map |
| Execution | DuckDB SQL over CSV/Parquet, no pandas dependency | pandas/BigQuery, platform-aware (All of Us, custom) |
| Output | counts, retained/eligible phecode list, unmapped-event QC file, audit trail with checksums | phecode counts / phecode table per person |

Because the two tools define "case" and "control exclusion" differently by
default, raw output won't match row-for-row unless you align both the
mapping version and the case rule. To check parity on the mapping step only:

1. Run `build-vocabulary` to produce `phetk_custom_map.csv` — an adapter
   written in PheTK's documented custom-map shape (`phecode`, `ICD`, `flag`,
   `sex`, `phecode_string`, `phecode_category`, `exclude_range`).
2. If your ICD source strips punctuation (UK Biobank extractions commonly
   do), run `scripts/redot_icd_for_phetk.py --input <fixture.csv> --output
   <fixture_dotted.csv>` first to reconstruct standard ICD9/10 decimal
   placement — PheTK does no code normalization, so undotted codes will
   silently under-match its map (see results below).
3. In a separate environment with `pip install -e '.[phetk]'`, run
   `scripts/compare_phetk.py --fixture-events <events.csv> --custom-map
   releases/<release>/phetk_custom_map.csv --output <phetk_out>` against a
   frozen fixture (invokes PheTK's `phecode count-phecode --platform custom`
   as a subprocess, keeping the GPL boundary explicit).
4. Compare PheTK's output to this tool's `person_phecodes.parquet`
   (`person_id`, `phecode`, event count) after applying the *any-event* case
   rule to both.
5. PheTK ships PhecodeX 1.0 by default; running against its bundled map
   instead of the frozen custom map will show real mapping drift between
   1.0 and 1.1, not a bug in either tool — treat it as a drift report, not
   a parity check.

### Verified parity result

The above procedure was run end-to-end against real PheTK 0.3.3 (`phetk phecode
count-phecode --platform custom --icd_version custom`), not just PheTK's
documentation.

**Small synthetic cohort (600 people, ~590 events):**

- **On cleanly-spelled ICD codes** (i.e. codes spelled exactly as they appear
  in the source map — see caveat below): the two tools produced **identical**
  `(person_id, phecode)` case sets — 269 cases each, 0 discrepancies either
  direction.
- **On the same cohort with realistic messy codes** (mixed case, stray
  whitespace, e.g. `" E11.9 "` or `e11.9`): this tool still produced 336
  correct cases (its normalization step strips presentation noise before
  matching), while PheTK produced only 269 — it **missed 67 real cases**
  because its custom-platform loader joins `ICD`/`vocabulary_id` as exact
  strings with no normalization.

**Real UK Biobank-scale cohort (336,305 people, 2.6M events, de-identified —
see `scripts/deidentify_ukb_for_testing.R`), built from the official
PhecodeX unrolled map (WHO ICD-10 rows + ICD-9-CM rows, matching UKB's native
ICD-10 coding):**

- **As UKB extractions naturally produce codes** (punctuation stripped, e.g.
  `extract_ID_and_ICD_UKB()`'s `gsub("\\.", "")`): this tool produced
  3,817,251 `(person_id, phecode)` case pairs; PheTK produced only 526,993 —
  **PheTK missed 86.2% of true cases**, because 91.6% of the PhecodeX map's
  entries (47,129 of 51,424 rows) are dotted subcodes (e.g. `A00.1`), and
  PheTK's exact-string join can only ever match the 8.4% that happen to be
  undotted top-level codes.
- **After re-inserting standard ICD9/ICD10 decimal placement** (see
  `scripts/redot_icd_for_phetk.py` — reconstructs the decimal from the
  standard category length, independent of the phecode map, so it's a fair
  reformatting rather than a comparison-defeating lookup): the two tools
  produced **exactly identical** case sets — 3,817,251 cases each, 0
  discrepancies either direction.

**Practical takeaway:** the two tools' underlying mapping logic is
equivalent once inputs are spelled consistently — every observed mismatch
traces to PheTK's custom-platform loader doing zero code normalization, not
to any difference in mapping semantics. If your ICD source strips
punctuation (UK Biobank extractions commonly do), either run
`scripts/redot_icd_for_phetk.py` first, or route codes through
`phecodex_mapper.normalize.normalize_code` before handing them to PheTK.
Otherwise, treat any PheTK/`phecodex-mapper` case-count mismatch as spelling
sensitivity in PheTK, not a bug in this package's mapping logic.

## Independent SNOMED validation

PhecodeX publishes no SNOMED mapping at all, so this tool's SNOMED support
(the `--athena-dir` bridge — see [`--athena-dir`](#--athena-dir-optional-build-vocabulary))
was validated three ways: known-code spot checks, an aggregate coverage
sanity check, and a hand-crafted end-to-end `map-phecodes` run — all
consistent with correct behaviour, including one genuine, traced,
non-bug map-vintage gap (a myocardial-infarction SNOMED code whose only
Athena cross-map target, `I21.B`, postdates the PhecodeX 1.1 map).

On top of those, the bridge was checked against an **independent, third-party
curated resource**: the
[Genes & Health](https://www.genesandhealth.org/) v10 custom binary trait
phenotype list, which independently curates ICD-10 *and* SNOMED codes for
287 clinical phenotypes (258 with both code types). For every phenotype, its
SNOMED codes were run through this tool's bridge and checked against the
phecode(s) implied by that same phenotype's own ICD-10 codes:

- Of 15,561 phenotype/SNOMED-code instances checked, where the bridge
  produced a phecode it **agreed with the sheet's own ICD-10 grouping 97.2%
  of the time** (5,074 agreements vs. 147 disagreements). Spot-checking the
  disagreements found they were phecode-taxonomy precision, not errors —
  e.g. a SNOMED code for "postpartum depression" correctly bridging to a
  postpartum-specific phecode rather than the sheet's generic depression
  bucket.
- The bridge did not fire at all for 12,813 of the sheet's 15,376 distinct
  SNOMED codes (83%). This is a real coverage limit, not a defect: ~1,800
  are Procedure/Observation/Measurement-domain codes that PhecodeX's
  diagnosis-only scope was never going to cover; ~1,600 are invalid/absent
  in the Athena extract used; and the largest remaining group (~8,500) are
  genuine clinical-finding concepts with no ICD diagnosis equivalent at all
  (e.g. "EKG: T wave abnormal", "Exercise tolerance test abnormal") — an
  abnormal test result isn't a diagnosis code, so no phecode should ever
  claim it.

**Takeaway:** where the SNOMED bridge does map a code, it agrees with an
independent curated resource in the vast majority of cases, and the
disagreements found were phecode precision rather than mapping errors. Its
lower overall hit rate on a broad SNOMED code list reflects real scope
(diagnosis-only, and only what Athena + PhecodeX jointly cover) rather than
a flaw in the join logic.

## Development

```bash
pip install -e '.[test]'
pytest
```
