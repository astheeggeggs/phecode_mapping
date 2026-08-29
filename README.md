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
unparseable dates as absent. A column present but entirely empty is refused too — the
presence of a column is not the presence of dates, and it would otherwise yield zero
cases from a rule that had no dates to apply.

The rule is **two distinct dates among the events mapping to that phecode**. The codes
need not be the same one twice, and need not be different — what counts is that the
phecode is evidenced on two separate days:

| the person's history | verdict |
|---|---|
| two codes mapping to the phecode, on different dates | **case** |
| the same code twice, on different dates | **case** |
| two codes mapping to the phecode, both on one date | non-evaluable |
| one code on one date | non-evaluable |
| no code for the phecode | control |

A single dated occurrence is deliberately neither case nor control — it is ambiguous
evidence, not evidence of absence — and `phecode_counts` reports those people under
`subthreshold_control_count`.

**On UK Biobank.** `prepare_ukb_for_mapping.R` takes dates from UKB's parallel date
arrays (41280 for the 41270 ICD-10 diagnoses, 41281 for 41271), matched by array index.
41280 records when a code was *first* recorded, so each (person, code) pair carries one
date — which means on this source the rule resolves to the first row of the table above:
two different codes for the phecode, first recorded on different days. The same code at
two separate admissions is not distinguishable in the wide extract; if you need
per-episode granularity, that lives in the HES episode tables (`hesin`/`hesin_diag`).
Cancer-registry and death-cause events are emitted undated and contribute no date.

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
mapping mode, thresholds, sex handling, and unmapped-event rates. Four fields are
worth checking on every run, because each records something that would otherwise be
invisible:

| field | why it matters |
|---|---|
| `events_in_file` vs `events` | the second is post-join. If they differ, events were dropped because their `person_id` is not in the cohort, and `unmapped_rate` describes only what survived |
| `control_exclusions.unmatched_rules` | rules that removed no one. Matching is case-sensitive, so part of a policy can silently do nothing |
| `sex.release_has_sex_metadata` | false means **no** phecode is sex-restricted and every sex-specific phenotype is scored against the whole cohort |
| `analysis_timezone` | pinned to UTC. Two sites must resolve a timestamp to the same calendar date or `--case-rule two-dates` gives them different case sets |

## Mapping policy

An event maps to a phecode only when its normalized code appears in the release's
PhecodeX map for that vocabulary. There is no inference from parent codes, no
string-prefix matching, and no cross-vocabulary fallback.

### You must state which ICD-10 you are mapping

`ICD10` (WHO) and `ICD10CM` (US clinical modification) are different vocabularies,
and the `vocabulary` column is taken as ground truth: a code declared `ICD10CM` is
matched only against the release's ICD-10-CM rows, never against WHO. Declaring the
wrong one does not fail — the codes simply do not match, and the events are dropped
into `unmapped_events.csv`.

This matters more than it sounds, because the two maps differ in both directions:

| | distinct codes in the shipped map |
|---|---|
| `ICD10` (WHO) | 8,560 |
| `ICD10CM` | 55,338 |
| in both | 7,730 — of which **163 carry different phecodes** |

So there is no safe default. Declaring WHO events as `ICD10CM` loses every WHO-only
code; declaring CM events as `ICD10` loses CM's finer subdivisions. And for the 163
codes present in both, the wrong label yields a *different phenotype* rather than no
phenotype — `J33.x` (nasal polyp) takes only RE_471.5 under CM but also CA_135
(benign neoplasm) under WHO.

**UK Biobank codes WHO ICD-10, so its events belong under `ICD10`.**
[`scripts/prepare_ukb_for_mapping.R`](scripts/prepare_ukb_for_mapping.R) emits that
label. Measured on a 2.6M-event UK Biobank extract, mislabelling it `ICD10CM` moved
168,769 events between mapped and unmapped.

Two things make a mismatch visible rather than silent. `map-phecodes` reports the
unmapped rate per vocabulary in `audit.json` and warns on stderr when a vocabulary
with at least 1,000 events exceeds 20% unmapped. And `manifest.json` records, under
`vocabularies`, which source file each label came from — worth checking, because
PhecodeX ships the WHO map twice: `phecodeX_unrolled_ICD_WHO.csv` labels it `ICD10`
while `phecodeX_unrolled_ICD_UKB.csv` labels byte-identical content `ICD10CM`. A
release built from the UKB file therefore expects `ICD10CM` events, and pairing it
with correctly-labelled `ICD10` events leaves 98% of them unmapped.

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

### Recovering codes the published map omits

`build-vocabulary --recover-unmapped` adds ICD codes PhecodeX does not map but
which this release can justify from evidence it already contains. It is opt-in;
without it the map is exactly what the published files hold.

Two things motivate it. PhecodeX's WHO ICD-10 map is roughly six times coarser
than its ICD-10-CM map (8,560 distinct codes against 55,338), and WHO retires
codes the map never catches up with — `I84.x` haemorrhoids was reclassified to
`K64.x`, so a cohort spanning 2000–2022 loses every older haemorrhoid episode with
no signal at all.

Two routes supply evidence. Nothing is inferred from code structure:

| route | evidence |
|---|---|
| `cross_vocabulary` | the same code carries phecodes under another vocabulary **of the same ICD generation** in this release |
| `snomed_bridge` | the code maps to a SNOMED concept the bridge accepted, which means every source ICD code for that concept agreed |

The `cross_vocabulary` route never crosses the ICD-9/ICD-10 boundary. The two
generations reuse the same code *strings* for unrelated diseases, so a bare string
match across them is not "the same code": ICD-9-CM `V09.0` is a penicillin-resistant
infection while WHO ICD-10 `V09.0` is a pedestrian struck in a nontraffic accident,
and ICD-9-CM `E888.9` is an unspecified fall while ICD-10-CM `E88.89` is a metabolic
disorder. The collision is systematic rather than incidental — ICD-9's `E` chapter
is external causes against ICD-10's endocrine/metabolic, and ICD-9's `V` chapter is
health status against ICD-10's transport accidents. Recovery is therefore confined
to `ICD10` ↔ `ICD10CM`, which is the route's purpose; `ICD9CM` has no sibling here
and takes no cross-vocabulary evidence.

The two routes are **not fully independent**, and `both_routes_agree` should not be
read as two witnesses. The SNOMED bridge retains a phecode only where every source
ICD code Athena collapses onto the concept implies it, and it counts those sources
by code *string* — so a code mapped under ICD-10-CM but absent from the sparser WHO
map is one voice, not a dissenting second one. That is the right call (absence is
not disagreement), but it means the bridge can be corroborating a recovery with the
very ICD-10-CM row the `cross_vocabulary` route already used. Measured on this
release, that is the case for 181 of the 863 `both_routes_agree` codes, and 42% of
bridged SNOMED concepts rest on a single source ICD code — "unanimous" over one
voter. Agreement here raises confidence; it does not double it.

Where both routes fire and agree, or only one fires, the row is added. Where they
**disagree the code is skipped** and named on stderr, unless
`--recovery-adjudication` resolves it — guessing between two contradicting sources
is the inference this tool refuses to make elsewhere. That file needs `icd_code`
and `adjudication_A_or_B`, where `A` selects the cross-vocabulary assignment and
`B` the SNOMED route.

Everything added is listed in `recovered_codes.csv` with its route, and summarised
under `recovery` in `manifest.json` along with the adjudication file's checksum.
Recovery runs *after* the SNOMED bridge and never feeds back into it, so it cannot
bootstrap itself, and it is purely additive — no published assignment is rewritten.

[`data/icd_recovery_adjudication.csv`](data/icd_recovery_adjudication.csv) holds
the reviewed verdicts for this release: 32 conflicts, 31 resolved to the
cross-vocabulary assignment and one (`M79.66`) to the SNOMED route, because the
cross-vocabulary option there is the upstream defect described below.

Measured on the full Athena extract with those verdicts: **1,938 codes (4,219 rows)
added, 0 conflicts left unresolved**, taking the WHO ICD-10 side of the map from
8,560 distinct codes to 10,405.

The effect on phenotypes is not uniform — it concentrates on traits the retired
codes had quietly gutted. On a 336,304-person cohort, 246 phecodes gained cases and
none lost any (measured before the ICD-9/ICD-10 guard above was added; of the traits
listed here only `MS_745` is touched by it, losing 2 of its map rows):

| phecode | before | after | |
|---|---|---|---|
| `CV_439` Hemorrhoids | 507 | 22,726 | 45× |
| `MS_745` Fractures | 5,934 | 19,167 | |
| `GI_524.1` Irritable bowel syndrome | 64 | 4,585 | 72× |

Haemorrhoids at 507 of 336,304 is 0.15%, which is not a plausible hospital-coded
rate for an older cohort; the codes had been lost to the `I84`→`K64`
reclassification. Before recovery that phenotype was not under-ascertained, it was
unusable — and nothing in the outputs said so.

Doing this at build time rather than at mapping time is deliberate. A federated
analysis needs every site to make the same decision once; a run-time fallback
would let two sites disagree silently and would break the exact-match policy above.

### Known upstream defect: limb-pain codes in the ICD-10-CM map

PhecodeX's ICD-10-CM map assigns the **thigh and lower-leg** pain codes to the
**arm** phecode. This is upstream, in `phecodeX_unrolled_ICD_CM.csv`, and affects
anyone using that map — the tool reproduces it faithfully because it maps exactly
what the release contains.

```
M79.65, M79.651, M79.652   Pain in thigh       -> SS_809.31 [Pain in arm*]
M79.66, M79.661, M79.662   Pain in lower leg   -> SS_809.31 [Pain in arm*]
```

`SS_809.32 [Pain in leg*]` exists and receives only the three generic `M79.60x`
codes. The sibling assignments are correct — `M79.64x` (hand and fingers) goes to
`SS_809.33` and `M79.67x` (foot and toes) to `SS_809.34` — so this looks like an
isolated slip rather than a systematic convention.

The practical consequence is that a cohort's "Pain in arm" phenotype silently
includes people whose only qualifying code was leg pain. On a 2.6M-event UK
Biobank extract, `M79.66` alone accounted for 2,365 events. If you use
`SS_809.31`, `SS_809.32`, or their parent `SS_809.3`, treat them as unreliable
until this is fixed upstream, and consider excluding them with
`--exclude-phenotypes`.

This was found by comparing unmapped WHO ICD-10 codes against their SNOMED-bridged
phecodes and adjudicating the disagreements; the same review confirmed the other
nineteen disagreements were cases where the CM map correctly adds specificity.

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

## Licence

The code in this repository is released under the [MIT licence](LICENSE) — use,
modify and redistribute it freely; the only condition is that the copyright notice
travels with it.

That licence covers **this tool**, not the data it maps. Two separate things ride
along with a built release, and neither is ours to relicense:

- **PhecodeX.** The mapping definitions come from the upstream
  [PhecodeX vocabulary repository](https://github.com/PheWAS/PhecodeXVocabulary),
  which states no licence of its own. Use follows academic norms and the citation
  below rather than an explicit grant. A release directory contains PhecodeX-derived
  content, so redistributing one is not covered by the MIT licence above.
- **OMOP/Athena.** Releases built with `--athena-dir` may embed Athena-derived
  knowledge, which is separately licensed and must not be redistributed. Build shared
  releases with `--icd-only`, and check
  `recovery.assignments_resting_solely_on_athena_evidence` in the manifest.

If you are redistributing a release rather than the code, take those two points to
your data-access team first.

## Provenance and citation

This tool does not define phenotypes. It applies the published **PhecodeX v1.1**
mapping, and any analysis using it should cite the source:

> Shuey MM, Stead WW, Aka I, et al. Next-generation phenotyping: introducing
> phecodeX for enhanced discovery research in medical phenomics.
> *Bioinformatics*. 2023;39(11):btad655. doi:10.1093/bioinformatics/btad655
> (PMID 37930895)

Source maps come from the [PhecodeX vocabulary repository](https://github.com/PheWAS/PhecodeXVocabulary);
`manifest.json` records the path and sha256 of every input file used, so a result
can be traced back to the exact map that produced it. Where this tool departs from
the published map — the opt-in recovery of omitted codes, and the one adjudicated
limb-pain verdict — it is recorded in `recovered_codes.csv` and the manifest rather
than folded in silently.

Releases built with `--athena-dir` use an OMOP/Athena vocabulary extract. Athena
content is separately licensed and **must not be redistributed**; `--icd-only` exists
so a shared release carries none of its tables. See the note above on
`assignments_resting_solely_on_athena_evidence` before sharing a recovered map.

Cross-checks against [PheTK](https://github.com/nhgritctran/PheTK) use the adapter in
`phetk_custom_map.csv`.

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

To build the release you actually hand to analysts, add recovery and `--icd-only`:

```bash
phecodex-map build-vocabulary \
  --phecodex-map phecodeX_unrolled_ICD_CM.csv \
  --phecodex-map phecodeX_unrolled_ICD_WHO.csv \
  --phecodex-info phecodeX_info_1.1_with_sex.csv \
  --athena-dir athena \
  --recover-unmapped \
  --recovery-adjudication data/icd_recovery_adjudication.csv \
  --icd-only \
  --output releases/phecodex-1.1-analyst
```

`--icd-only` is not optional for a shared release: `package_distribution.py` refuses
any release carrying SNOMED-derived tables, so without it there is no bundle. The
SNOMED bridge is still built and still supplies recovery evidence — only its tables
are withheld, and `manifest.json` records `icd_only: true` so their absence reads as
a decision rather than a failed build.

Check `recovery.assignments_resting_solely_on_athena_evidence` in the manifest before
redistributing. On the 1.1 release that is 120 of 1,939 assignments (6.2%); the other
1,819 are justified by PhecodeX's own published map and carry no Athena-derived
content. If your site is not licensed to redistribute SNOMED-derived knowledge, that
number is the part to discuss with your data-access team.

`--phecodex-info` is optional; omitting it ships a phecode-only info table so the
release still verifies, but the map then carries **no sex restrictions at all** and
every sex-specific phecode is scored against the whole cohort. `audit.json` reports
`release_has_sex_metadata: false` when that happens. For any real analysis, supply it.

Builds are byte-reproducible: two builds from identical inputs produce identical
artefacts and therefore identical checksums, so federated sites can compare
`manifest.json` digests and establish that they hold the same map. Every table is
written in a fixed order and the workbook's embedded timestamps are pinned.
`manifest.json` itself is the one exception — it records `created_at_utc`.

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
