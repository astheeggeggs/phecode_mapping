# PhecodeX analyst guide

This bundle converts canonical cohort and clinical-event files into a binary
PhecodeX phenotype matrix for downstream RVAS analysis. It runs locally or in
your secure compute environment; individual-level inputs and outputs must not
leave that environment.

## Install and verify

Use Python 3.11 or newer:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/pip install .
```

Verify the shared release:

```bash
.venv/bin/python scripts/verify_release.py \
  --release release
```

## Required inputs

`cohort.csv` must contain one row per person:

```text
person_id,sex
```

`events.csv` must contain one row per diagnosis/clinical event:

```text
person_id,code,vocabulary,event_date
```

`event_date` may be omitted for the default one-event case rule. If you extract with
`scripts/prepare_ukb_for_mapping.R` it is populated from UKB's parallel date arrays
(41280/41281) and the script reports how many events carry a date; see the README's
input-contract section for what `--case-rule two-dates` then measures on that source,
which is not "two visits". Supported
vocabularies are `ICD9CM`, `ICD10`, `ICD10CM`, and `SNOMED`. Sex must be
`Male`, `Female`, or blank.

**Be explicit about which ICD-10 you are using.** `ICD10` (WHO) and `ICD10CM`
(US clinical modification) are different vocabularies and the `vocabulary` column
is trusted as ground truth — the wrong label does not fail, it silently drops the
events into `unmapped_events.csv`, or for the 163 codes that exist in both maps
with different phecodes, assigns the wrong phenotype. **UK Biobank codes WHO
ICD-10, so use `ICD10`.** Check your run's `audit.json` for
`unmapped_by_vocabulary`. **Do not judge this by the unmapped rate alone** — a
correctly labelled UK Biobank extract sits near 20% simply because PhecodeX's WHO
map is coarse, so the rate cannot tell a mislabel from an honest gap. The field that
can is `share_of_unmapped_rescued_by_sibling`: the fraction of your failing events
that *would* map under the other ICD-10 label. Correctly labelled data sits near 1%;
genuinely mislabelled data sits near 20%. `map-phecodes` warns on stderr above 5%. Check
also that the release you were sent expects the same label — `manifest.json`
records under `vocabularies` which source file each label came from, and PhecodeX
publishes the same WHO map under both names. See the README's "You must state
which ICD-10 you are mapping" for the detail.

## Preflight and run

Validate inputs without processing them:

```bash
.venv/bin/phecodex-map run \
  --release release --cohort cohort.csv --events events.csv \
  --output phecodex_run --preflight-only
```

Run the mapper:

```bash
.venv/bin/phecodex-map run \
  --release release --cohort cohort.csv --events events.csv \
  --output phecodex_run
```

The bundled `examples/` files are a two-person smoke test. Running them exercises
the whole path but retains **no phenotypes** — the default `--min-cases 200` cannot
be met by two people — so the run warns and reports `matrix_columns: 0`. That is the
expected result and confirms the install works; use your own cohort for real output.

The primary RVAS input is:

```text
phenotype_matrix.csv.gz
```

It contains `person_id` plus one column per retained PhecodeX trait. Values
are `1` for cases, `0` for ordinary controls, and blank for non-evaluable
people. Keep all person-level files secure. Use `audit.json`,
`phecode_counts.parquet`, and `unmapped_events.csv` for QC.

The standard `run` command automatically applies the bundled recommended
exclusions for Symptoms, Neonatal, Infections, and three administrative
pregnancy-encounter phecodes. Supply `--exclude-phenotypes <file>` to replace
them with a reviewed study-specific policy.

To specify the bundled policy explicitly:

```bash
.venv/bin/phecodex-map run \
  --release release \
  --cohort cohort.csv \
  --events events.csv \
  --exclude-phenotypes src/phecodex_mapper/data/recommended_exclusions.csv \
  --output phecodex_run
```

## How long it takes, and how much memory

Ask for the memory before you queue the job. Mapping is done in DuckDB, which is
memory-hungry by design, and the run is the only step that needs a substantial machine.

Measured end to end on a 500,000-person cohort with 2,600,000 events against a real
two-vocabulary release, on 8 cores at 2.0 GHz:

| step | wall time | peak memory | output |
|---|---:|---:|---|
| `build-vocabulary` (maintainers only) | 45 s | 0.2 GB | — |
| `run` (500k people, 2.6M events) | 3.7 min | 5.0 GB | 41 MB |

Practical guidance:

- **Ask for 16 GB.** Peak resident memory was 5 GB, and the OS reported a 9.5 GB peak
  footprint once mapped and compressed pages are counted. DuckDB will spill to disk on
  a smaller machine rather than fail, so a 8 GB node usually still finishes — slower.
- **Time scales with events, memory with cohort size × retained phecodes**, because the
  phenotype matrix is people × traits. Halving the cohort roughly halves both.
- **Budget disk for about 50 MB of outputs** at this scale, dominated by
  `phenotype_matrix` in its two formats and `person_phecodes.parquet`.
- `verify_release.py`, preflight, and the QC scripts are all seconds and need no
  particular resources.

The cohort here is synthetic — real identifiers never left their environment — with
codes drawn from the release's own WHO ICD-10 vocabulary on a skewed distribution, so
a few codes are common and most are rare. Runtime and memory are driven by row counts
and column cardinality rather than by which specific codes appear, so these figures
should transfer; the retained-phenotype count from a synthetic draw should not be read
as a prediction of yours (see the README's attrition curve for that).

## UK Biobank extraction

Run this inside the approved secure environment. Its outputs are individual-level and
must never leave that environment or be committed.

```bash
Rscript scripts/prepare_ukb_for_mapping.R \
  --input ukb_wide_extract.csv \
  --cohort-out cohort.csv.gz \
  --events-out events.csv.gz \
  --female-code 0 --male-code 1
```

`--female-code` and `--male-code` are required rather than assumed: the script reads
genetic sex (field 22001), and a guessed encoding is silently wrong rather than loudly
wrong. Both output paths must end in `.gz`.

It reads hospital diagnoses from fields 41270 (ICD-10) and 41271 (ICD-9), falling back
to 41202/41204 and 41203/41205 when those are absent, plus cancer registry (40006,
40013) and death causes (40001, 40002). Event dates come from the parallel arrays 41280
and 41281, matched by array index; the pairing is checked at run time and the script
stops rather than mis-dating events if it does not hold. Cancer-registry and
death-cause events are emitted undated.

Events are labelled `ICD10` — **WHO ICD-10, which is what UK Biobank codes.** Pair them
with a release built from `phecodeX_unrolled_ICD_WHO.csv`. See "You must state which
ICD-10 you are mapping" above; mislabelling these as `ICD10CM` does not fail, it
silently produces the wrong phenotypes.

The script reports how many events carry a date. If none do, it omits the `event_date`
column entirely, so `--case-rule two-dates` is refused rather than silently returning
zero cases.

> **Version.** UK Biobank is WHO ICD-10, and PhecodeX has published no WHO map for
> version 1.1 — so a UKB run is phenotyped from **PhecodeX 1.0**, whatever the release
> directory is called. Check `phecodex_upstream_versions` in the release's
> `manifest.json` and describe it accurately in any methods section.

## Cohort-size attrition

How many phenotypes survive the retention thresholds at a given cohort size. Useful for
deciding whether a planned subset is large enough to be worth running.

```bash
.venv/bin/python scripts/plot_phecode_attrition.py \
  --cohort cohort.csv \
  --release release \
  --person-phecodes phecodex_run/person_phecodes.parquet \
  --output-csv attrition.csv --output-svg attrition.svg \
  --sample-sizes 1000,5000,10000,50000,100000,250000,500000
```

Before trusting the curve, confirm its last point reproduces your run:

```bash
.venv/bin/python scripts/reconcile_attrition.py \
  --run phecodex_run --release release --cohort cohort.csv
```

It compares three numbers that must agree — the run's own `retained` flag, the
thresholds reapplied to the run's counts, and the curve's model — and if they do not,
names the phecodes responsible. A curve that disagrees at full cohort size is wrong at
every smaller size too.

**Pass `--release`.** Without it every phecode is scored against the whole sample, and
the ~325 sex-restricted phecodes get a denominator twice their real one — so the curve
promises phenotypes a real run will not retain. With it, the curve reproduces the
mapper's own retained count exactly at full cohort size, which is the check worth
running first: compare the last row against `phenotype_matrix.n_columns` in your
`audit.json`.

One approximation remains: it uses the any-event case table and does not reproduce
control exclusions, so with `--control-exclusions` in play the counts are a slight
upper bound. `sex_aware` in the output CSV records whether sex restrictions were
applied. The CSV and SVG are aggregate and safe to share where your data
agreement permits; the inputs are not.

## Confirming a fixture leaks nothing

If you build a de-identified fixture with `scripts/deidentify_ukb_for_testing.R`, check
it before it leaves secure compute. This prints **counts only** — never an identifier,
never a code, never a row — so its output is safe to read out or paste anywhere, while
its inputs are not.

```bash
.venv/bin/python scripts/check_deidentification.py \
  --input ukb_phenotype_file.tab.gz \
  --cohort cohort_deid.csv.gz \
  --events events_deid.csv.gz
```

It answers three questions and exits non-zero on any failure:

1. **Does a real `eid` appear as an output `person_id`?** Must be zero.
2. **Does a real `eid` appear anywhere in any column?** Must be zero — this catches a
   stray crosswalk column or an id pasted into a field nobody thought to look at.
3. **Were code combinations actually broken up?** Reported as the share of output people
   whose exact code set matches some real person's. A few percent is chance, since
   people carrying one common code collide often. Near 100% means only the identifier
   was replaced — which is not de-identification, because a rare diagnosis pattern
   identifies someone whatever the label says.

Question 3 is the one worth understanding. It is the check that would have caught the
primary-care script before it was fixed: that script assigned fresh ids while keeping
each person's whole history together, and on a simulated extract 60 of 60 original code
pairs survived intact.

## Prevalence sanity check

Run this once, on your real cohort, after your first full run. It is the only check
that can catch a whole class of error the test suite cannot reach: every automated
check in this repo is internal consistency, and none of them can tell you whether
hypertension comes out at 25% or at 2.5%.

```bash
.venv/bin/python scripts/check_prevalence.py \
  --run phecodex_run \
  --release release \
  --cohort cohort.csv \
  --out prevalence.csv
```

It reports three things: named common phecodes against wide order-of-magnitude bands,
sex-restricted phecodes (where the expected number of wrong-sex people scored is
**exactly zero**, which makes it the sharpest check available), and the commonest
phecodes in your data for eyeballing.

Treat the bands as a smoke alarm, not a validation: they are set wide enough to catch
something broken by a factor of ten, and a single near-miss is more likely the band
being wrong for your cohort than a mapping error. Several at once, or anything wrong
by an order of magnitude, is worth investigating.

The output is aggregate — counts and rates, no `person_id`, no per-person rows — but
check it against your own data-sharing agreement before moving it anywhere.

## Containers

The repository includes `containers/Dockerfile` and
`containers/Singularity.def`. They install the pinned dependencies and mapper;
they do not contain the release or any cohort data.

The image tag below is the **mapper's** version, not a PhecodeX version. Do not tag
an image `1.1`: the container carries no map at all, and the release you later mount
into it is normally a hybrid of PhecodeX 1.1 (ICD-10-CM) and 1.0 (WHO ICD-10). Which
PhecodeX versions you actually used is recorded in the release's `manifest.json` under
`phecodex_upstream_versions`, and nowhere else.

The standard analyst distribution is ICD-only and does not include
SNOMED/Athena-derived mapping tables. SNOMED support remains available in the
advanced build workflow for sites with the appropriate licensing, but those
outputs must not be added to the shared analyst bundle.

Build and test Docker locally:

```bash
docker build -f containers/Dockerfile -t phecodex-mapper:0.1.0 .
docker run --rm phecodex-mapper:0.1.0 --help
```

Run against local files by mounting only the secure input/output directories:

```bash
docker run --rm \
  -v "$PWD/release:/data/release:ro" \
  -v "$PWD/input:/data/input:ro" \
  -v "$PWD/output:/data/output" \
  phecodex-mapper:0.1.0 run \
    --release /data/release \
    --cohort /data/input/cohort.csv \
    --events /data/input/events.csv \
    --output /data/output/phecodex_run
```

Build an Apptainer/Singularity image on a build host:

```bash
apptainer build phecodex-mapper-0.1.0.sif containers/Singularity.def
```

On clusters where unprivileged builds are required, build the image on an
approved host or use an administrator-provided `--fakeroot`/remote-builder
workflow. Run it without copying cohort data into the image:

```bash
apptainer exec \
  --bind /secure/release:/data/release:ro \
  --bind /secure/input:/data/input:ro \
  --bind /secure/output:/data/output \
  phecodex-mapper-0.1.0.sif \
  phecodex-map run \
    --release /data/release \
    --cohort /data/input/cohort.csv \
    --events /data/input/events.csv \
    --output /data/output/phecodex_run
```

Verify the release separately before running the container. Never use Docker
build context or container bind mounts that include Athena source files,
individual-level data, or generated cohort outputs unless they are explicitly
needed at runtime and remain on secure storage.
