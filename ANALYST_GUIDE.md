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

`event_date` may be omitted for the default one-event case rule. Supported
vocabularies are `ICD9CM`, `ICD10`, `ICD10CM`, and `SNOMED`. Sex must be
`Male`, `Female`, or blank.

**Be explicit about which ICD-10 you are using.** `ICD10` (WHO) and `ICD10CM`
(US clinical modification) are different vocabularies and the `vocabulary` column
is trusted as ground truth — the wrong label does not fail, it silently drops the
events into `unmapped_events.csv`, or for the 163 codes that exist in both maps
with different phecodes, assigns the wrong phenotype. **UK Biobank codes WHO
ICD-10, so use `ICD10`.** Check your run's `audit.json` for
`unmapped_by_vocabulary`; a rate above about 20% for one vocabulary usually means
it is mislabelled, and `map-phecodes` warns on stderr when that happens. Check
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

## Containers

The repository includes `containers/Dockerfile` and
`containers/Singularity.def`. They install the pinned dependencies and mapper;
they do not contain the release or any cohort data.

The standard analyst distribution is ICD-only and does not include
SNOMED/Athena-derived mapping tables. SNOMED support remains available in the
advanced build workflow for sites with the appropriate licensing, but those
outputs must not be added to the shared analyst bundle.

Build and test Docker locally:

```bash
docker build -f containers/Dockerfile -t phecodex-mapper:1.1 .
docker run --rm phecodex-mapper:1.1 --help
```

Run against local files by mounting only the secure input/output directories:

```bash
docker run --rm \
  -v "$PWD/release:/data/release:ro" \
  -v "$PWD/input:/data/input:ro" \
  -v "$PWD/output:/data/output" \
  phecodex-mapper:1.1 run \
    --release /data/release \
    --cohort /data/input/cohort.csv \
    --events /data/input/events.csv \
    --output /data/output/phecodex_run
```

Build an Apptainer/Singularity image on a build host:

```bash
apptainer build phecodex-mapper-1.1.sif containers/Singularity.def
```

On clusters where unprivileged builds are required, build the image on an
approved host or use an administrator-provided `--fakeroot`/remote-builder
workflow. Run it without copying cohort data into the image:

```bash
apptainer exec \
  --bind /secure/release:/data/release:ro \
  --bind /secure/input:/data/input:ro \
  --bind /secure/output:/data/output \
  phecodex-mapper-1.1.sif \
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
