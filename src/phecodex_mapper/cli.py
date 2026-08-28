from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .mapper import map_phecodes
from .validation import validate_phecodex_counts
from .vocabulary import build_vocabulary
from .workflow import preflight, run_workflow

DEFAULT_EXCLUSIONS = Path(__file__).with_name("data") / "recommended_exclusions.csv"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="phecodex-map",
        description="Build reproducible PhecodeX mapping releases and map cohort events to phecodes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    workflow = commands.add_parser("run", help="Validate canonical inputs and map them against the release.")
    workflow.add_argument("--release", required=True, type=Path)
    workflow.add_argument("--cohort", required=True, type=Path)
    workflow.add_argument("--events", required=True, type=Path)
    workflow.add_argument("--output", required=True, type=Path)
    workflow.add_argument("--case-rule", choices=["any-event", "two-dates"], default="any-event")
    workflow.add_argument("--control-exclusions", type=Path)
    workflow.add_argument("--exclude-phenotypes", type=Path,
                          help="Phenotype exclusions. Defaults to the bundled recommended exclusions "
                               "(Symptoms, Neonatal, Infections, and administrative pregnancy codes).")
    workflow.add_argument("--min-cases", type=int, default=200)
    workflow.add_argument("--min-controls", type=int, default=200)
    workflow.add_argument("--max-unmapped-rate", type=float, default=1.0)
    workflow.add_argument("--preflight-only", action="store_true", help="Validate inputs and print the preflight report without mapping.")

    build = commands.add_parser(
        "build-vocabulary",
        help="Turn an official PhecodeX map into a versioned, checksummed release directory.",
        description="Turn an official PhecodeX map (and, optionally, an Athena vocabulary "
                     "extract) into a versioned, checksummed release directory for map-phecodes.",
    )
    build.add_argument("--phecodex-map", required=True, type=Path, action="append",
                        help="Official PhecodeX unrolled ICD map CSV/Parquet. Repeat this "
                             "option to combine CM and WHO maps (requires phecode, ICD/icd, "
                             "vocabulary_id columns).")
    build.add_argument("--phecodex-info", type=Path,
                        help="Optional phecode metadata CSV/Parquet (phecode, sex, "
                             "phecode_string, category). Omit to default sex=Both and blank text.")
    build.add_argument("--athena-dir", type=Path,
                        help="Optional directory with an authorized Athena/OMOP vocabulary "
                             "extract (CONCEPT.csv, CONCEPT_RELATIONSHIP.csv) used to bridge "
                             "SNOMED codes to the ICD map. Never commit Athena data or credentials.")
    build.add_argument("--recover-unmapped", action="store_true",
                        help="Add ICD codes the published PhecodeX map omits but this release can "
                             "justify -- either the same code carries phecodes under another "
                             "vocabulary of the same ICD generation (the ICD-9/ICD-10 boundary "
                             "is never crossed), or it maps to a SNOMED concept the bridge accepted. "
                             "Recovers codes PhecodeX's sparse WHO map lacks and codes WHO has "
                             "retired (e.g. I84.x haemorrhoids, reclassified to K64.x). Requires "
                             "--athena-dir. Codes whose two routes disagree are skipped unless "
                             "--recovery-adjudication resolves them; every added row is listed in "
                             "recovered_codes.csv and summarised in the manifest.")
    build.add_argument("--recovery-adjudication", type=Path,
                        help="CSV resolving codes whose recovery routes disagree. Requires columns "
                             "icd_code and adjudication_A_or_B, where A selects the cross-vocabulary "
                             "assignment and B the SNOMED route.")
    build.add_argument("--icd-only", action="store_true",
                        help="Build an ICD-only release: the SNOMED bridge is still built and used "
                             "as recovery evidence, but snomed_map.csv/parquet are not written and "
                             "the reference workbook omits the bridge sheet. This is what the analyst "
                             "distribution requires -- package_distribution.py refuses any release "
                             "carrying SNOMED-derived tables. The manifest records icd_only=true so "
                             "their absence is visibly a decision rather than a failed build.")
    build.add_argument("--output", required=True, type=Path,
                        help="Release directory to create. Must not already exist.")

    run = commands.add_parser(
        "map-phecodes",
        help="Map one cohort's events against a release and write per-phecode case/control counts.",
        description="Map one cohort's ICD/SNOMED events against a release built by "
                     "build-vocabulary and write per-phecode case/control counts.",
    )
    run.add_argument("--release", required=True, type=Path,
                      help="Release directory produced by build-vocabulary.")
    run.add_argument("--cohort", required=True, type=Path,
                      help="CSV/Parquet with one row per person (requires column: person_id, "
                           "non-null and unique; required column: sex, values 'Male'/'Female', "
                           "used to NA out sex-restricted phecodes in phenotype_matrix).")
    run.add_argument("--events", required=True, type=Path,
                      help="CSV/Parquet with one row per clinical event (requires columns: "
                           "person_id, code, vocabulary; vocabulary is ICD9CM, ICD10CM, ICD10, "
                           "or SNOMED; event_date required for --case-rule "
                           "two-dates).")
    run.add_argument("--output", required=True, type=Path,
                      help="Run directory to create. Must not already exist.")
    run.add_argument("--case-rule", choices=["any-event", "two-dates"], default="any-event",
                      help="any-event: one mapped event makes a case (default). two-dates: "
                           "requires mapped events on >=2 distinct dates for the same phecode.")
    run.add_argument("--control-exclusions", type=Path,
                      help="Optional CSV/Parquet (phecode, exclusion_type, exclusion_value, "
                           "vocabulary, [version]) removing people from a phecode's control pool. Cases are "
                           "never excluded.")
    run.add_argument("--exclude-phenotypes", type=Path,
                      help="Optional CSV/Parquet (match_type, match_value) dropping whole "
                           "phecodes from every output (unlike --control-exclusions, which only "
                           "adjusts other phecodes' control pools). match_type is 'category' "
                           "(matched against the release's phecode_info 'category' column) or "
                           "'phecode' (exact phecode). See "
                           "phecodex_mapper/data/recommended_exclusions.csv for a starting point "
                           "(phenotypes with poor genetic construct validity, e.g. non-specific "
                           "symptom codes) -- review and extend it for your own analysis rather "
                           "than using it unmodified.")
    run.add_argument("--min-cases", type=int, default=200,
                      help="Minimum case count for a phecode to be marked retained (default: 200).")
    run.add_argument("--min-controls", type=int, default=200,
                      help="Minimum control count (after exclusions) for a phecode to be marked "
                           "retained (default: 200).")
    run.add_argument("--max-unmapped-rate", type=float, default=1.0,
                      help="Raise an error if the fraction of events that fail to map exceeds "
                           "this (default: 1.0, i.e. never fail).")

    validate = commands.add_parser(
        "validate-phecodex",
        help="Compare aggregate UK Biobank PhecodeX counts with an All by All summary export.",
    )
    validate.add_argument("--run", required=True, type=Path,
                          help="map-phecodes output directory containing aggregate outputs and audit.json.")
    validate.add_argument("--release", required=True, type=Path,
                          help="PhecodeX release directory used for the mapping run.")
    validate.add_argument("--external", required=True, type=Path,
                          help="Aggregate All by All PhecodeX CSV/Parquet export with the documented columns.")
    validate.add_argument("--output", required=True, type=Path,
                          help="New directory for comparison tables, review rows, plot, and validation.json.")
    args = parser.parse_args()
    try:
        if args.command == "run":
            exclude_phenotypes = args.exclude_phenotypes or DEFAULT_EXCLUSIONS
            if args.preflight_only:
                print(json.dumps(preflight(args.release, args.cohort, args.events), indent=2, sort_keys=True))
            else:
                audit = run_workflow(release=args.release, cohort=args.cohort, events=args.events, output=args.output, case_rule=args.case_rule, exclusions=args.control_exclusions, exclude_phenotypes=exclude_phenotypes, min_cases=args.min_cases, min_controls=args.min_controls, max_unmapped_rate=args.max_unmapped_rate)
                print(json.dumps({"output": str(args.output), "mapping_policy": audit["mapping_policy"], "matrix_columns": audit.get("phenotype_matrix", {}).get("n_columns")}, indent=2))
        elif args.command == "build-vocabulary":
            build_vocabulary(args.phecodex_map, args.phecodex_info, args.output, args.athena_dir,
                             args.recover_unmapped, args.recovery_adjudication, args.icd_only)
        elif args.command == "map-phecodes":
            map_phecodes(args.release, args.cohort, args.events, args.output, args.case_rule, args.control_exclusions, args.min_cases, args.min_controls, args.max_unmapped_rate, args.exclude_phenotypes)
        else:
            validate_phecodex_counts(args.run, args.release, args.external, args.output)
    except (ValueError, FileExistsError, FileNotFoundError, RuntimeError) as exc:
        # Known, user-actionable failures (bad input, existing output dir, unmapped-rate
        # threshold, ...) are reported as a single line; anything else surfaces as a full
        # traceback so unexpected bugs stay debuggable.
        print(f"phecodex-map: error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__": main()
