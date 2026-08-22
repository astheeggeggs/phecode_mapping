#!/usr/bin/env Rscript
#
# De-identify a UK Biobank ICD extract (as returned by extract_ID_and_ICD_UKB()
# in POI_rg_munging) and reshape it into phecodex-mapper's --cohort/--events
# input format, for use as a realistic test fixture.
#
# De-identification strategy
# ---------------------------
# Simply relabelling `eid` is not sufficient: a person's specific COMBINATION
# of codes can itself be identifying even under a fake ID (e.g. a rare
# diagnosis pattern). This script instead:
#   1. explodes every person's code string into one row per (eid, code)
#   2. drops eid entirely and assigns fresh synthetic person_id integers
#   3. block-shuffles codes across synthetic people, preserving each
#      person's *code count* (so per-phecode case/control counts stay
#      realistic) while destroying every real per-person code combination
#   4. synthesizes event_date uniformly at random (the UKB extraction this
#      script consumes has no per-code date; even if it did, reusing real
#      dates would itself be a re-identification risk, so don't). Until
#      explicit dated diagnosis fields (e.g. 41262/41263) are wired in, this
#      treats the same code appearing more than once for a synthetic person
#      as two separate events and guarantees them distinct dates, so
#      --case-rule two-dates can be exercised against this fixture.
#
# The real `eid` and any per-code dates from the source file are never
# written to disk by this script. Run it only on secure UKB-approved
# compute, and treat its *output* as safe to move to a normal dev/test
# environment -- do not skip a review of your own RAP/DNAnexus/RDSF export
# policy before moving the output off secure compute.
#
# Usage
# -----
#   source("deidentify_ukb_for_testing.R")
#   dt_icd <- extract_ID_and_ICD_UKB(phenotype_file = "...")  # from POI_rg_munging
#   deidentify_ukb_icd_for_testing(
#     dt_icd,
#     events_out = "events_deid.csv.gz",
#     cohort_out = "cohort_deid.csv.gz",
#     female_code = "0", male_code = "1"
#   )
#
# The resulting events_deid.csv.gz / cohort_deid.csv.gz can be passed straight to:
#   phecodex-map map-phecodes --release <release> \
#     --cohort cohort_deid.csv.gz --events events_deid.csv.gz --output <run>
#
# Command-line usage (reads the raw UKB extract itself, compressed or not --
# data.table::fread shells out to zcat/gunzip transparently for .gz/.bgz):
#
#   Rscript scripts/deidentify_ukb_for_testing.R \
#     --input /path/to/ukb_phenotype_file.tab.gz \
#     --events-out events_deid.csv.gz \
#     --cohort-out cohort_deid.csv.gz \
#     --seed 1 \
#     --female-code 0 \
#     --male-code 1
#
# --input is read with extract_ID_and_ICD_UKB() (defined below), which
# understands both UKB column-naming conventions ("41202-2.0" and
# "f.41202.0.0"/"f.eid") and extracts sex field 22001. The command-line
# sex encodings must be supplied explicitly with --female-code and --male-code.
# If you already have a data.table in the eid /
# ICD9_string / ICD10_string shape, source() this file instead and call
# deidentify_ukb_icd_for_testing() directly -- see the Usage block above.

library(data.table)

# Reads a raw UKB phenotype extract and returns a data.table with columns
# eid, sex_code, ICD9_string, ICD10_string (one row per person, codes space-separated).
# Handles both UKB column-naming conventions seen in the wild:
#   - "<field>-<instance>.<array>", e.g. "41202-2.0" (including sex
#     as "22001-0.0")
#   - "f.<field>.<instance>.<array>", e.g. "f.41202.0.0", with the ID column
#     named "f.eid" instead of "eid".
extract_ID_and_ICD_UKB <- function(
	phenotype_file = "/well/lindgren-ukbb/projects/ukbb-11867/DATA/PHENOTYPE/PHENOTYPE_MAIN/ukb10844_ukb50009_updateddiagnoses_14012022.csv",
	sex_column = NULL
)
{
	get_cols <- function(codes, dt) {
		cols <- c()
		for (code in codes) {
			cols <- c(cols, grep(paste0("^", code, "\\-"), names(dt), value=TRUE))
			cols <- c(cols, grep(paste0("^f\\.", code, "\\."), names(dt), value=TRUE))
		}
		return(cols)
	}

	dt_header <- fread(phenotype_file, na.strings=NULL, nrow=1)
	id_col <- if ("eid" %in% names(dt_header)) "eid" else "f.eid"

	ICD10s <- if (length(get_cols("41270", dt_header))) c("41270", "40006", "40001", "40002") else c("41202", "41204", "40006", "40001", "40002")
	ICD9s <- if (length(get_cols("41271", dt_header))) c("41271", "40013") else c("41203", "41205", "40013")

	sex_candidates <- if (is.null(sex_column)) c("f.22001.0.0", "22001-0.0") else sex_column
	sex_cols <- intersect(sex_candidates, names(dt_header))
	if (!length(sex_cols)) {
		stop(sprintf("Could not find required UKB sex column; expected one of: %s",
			paste(sex_candidates, collapse = ", ")))
	}
	sex_cols <- sex_cols[1]
	cols <- c(sex_cols, get_cols(c(ICD9s, ICD10s), dt_header))
	select_cols <- rep("character", (length(cols) + 1))
	names(select_cols) <- c(id_col, cols)

	# Read in the entire file ensuring these columns are encoded as characters to avoid NA weirdness.
	dt <- fread(phenotype_file, na.strings=NULL, select=select_cols)
	if (id_col != "eid") setnames(dt, id_col, "eid")
	dt[, sex_code := apply(.SD, 1, function(x) {
		x <- x[!is.na(x) & nzchar(trimws(x))]
		if (length(x)) trimws(x[1]) else ""
	}), .SDcols=sex_cols]

	ICD10_cols <- get_cols(ICD10s, dt)
	dt[, ICD10_string := do.call(paste, .SD), .SDcols=ICD10_cols]
	dt[, ICD10_string := gsub("NA", "", ICD10_string)]
	dt[, ICD10_string := gsub("^(.*)$", " \\1 ", ICD10_string)]
	dt[, ICD10_string := gsub("( )+", " ", ICD10_string)]
	dt[, ICD10_string := gsub("\\.", "", ICD10_string)]

	ICD9_cols <- get_cols(ICD9s, dt)
	dt[, ICD9_string := do.call(paste, .SD), .SDcols=ICD9_cols]
	dt[, ICD9_string := gsub("NA", "", ICD9_string)]
	dt[, ICD9_string := gsub("^(.*)$", " \\1 ", ICD9_string)]
	dt[, ICD9_string := gsub("( )+", " ", ICD9_string)]
	dt[, ICD9_string := gsub("\\.", "", ICD9_string)]

	if (length(which(is.na(dt$ICD10_string))) > 0) dt$ICD10_string[which(is.na(dt$ICD10_string))] <- ""
	if (length(which(is.na(dt$ICD9_string))) > 0) dt$ICD9_string[which(is.na(dt$ICD9_string))] <- ""

	dt <- dt[, c("eid", "sex_code", "ICD9_string", "ICD10_string"), with=FALSE]
	return(dt)
}

deidentify_ukb_icd_for_testing <- function(
	dt_icd,
	events_out = "events_deid.csv.gz",
	cohort_out = "cohort_deid.csv.gz",
	date_min = "2000-01-01",
	date_max = "2022-12-31",
	seed = NULL,
	max_people = NULL,
	female_code = NULL,
	male_code = NULL
) {
	stopifnot(all(c("eid", "sex_code", "ICD9_string", "ICD10_string") %in% names(dt_icd)))
	if (is.null(female_code) || is.null(male_code)) stop("female_code and male_code must be supplied explicitly")
	if (!grepl("\\.gz$", events_out, ignore.case=TRUE) || !grepl("\\.gz$", cohort_out, ignore.case=TRUE)) {
		stop("events_out and cohort_out must be gzip-compressed paths ending in .gz")
	}
	dt_icd <- data.table(dt_icd)
	if (!is.null(seed)) set.seed(seed)

	n_people <- nrow(dt_icd)
	if (!is.null(max_people) && max_people < n_people) {
		# Subsample people (not codes) before de-identifying, if you only want
		# a smaller fixture. Subsampling after de-identification would be fine
		# too, but doing it first keeps runtime down on very large extracts.
		dt_icd <- dt_icd[sample(.N, max_people)]
		n_people <- max_people
	}
	sex_values <- trimws(as.character(dt_icd$sex_code))
	sex_values[is.na(dt_icd$sex_code) | !nzchar(sex_values) | toupper(sex_values) == "NA"] <- ""
	bad_sex <- unique(sex_values[nzchar(sex_values) & sex_values != as.character(female_code) & sex_values != as.character(male_code)])
	if (length(bad_sex)) stop(sprintf("Unexpected sex code(s): %s", paste(bad_sex, collapse=", ")))
	dt_icd[, sex_normalized := fifelse(sex_values == as.character(female_code), "Female", fifelse(sex_values == as.character(male_code), "Male", NA_character_))]

	# 1) explode each person's code strings into one row per (row_index, code, vocabulary).
	# row_index is a purely positional handle for the block shuffle below -- it is
	# discarded before anything is written and never derived from eid.
	dt_icd[, row_index := .I]
	split_codes <- function(code_string) {
		codes <- strsplit(trimws(code_string), "\\s+")[[1]]
		codes[nzchar(codes)]
	}
	build_long <- function(col, vocabulary) {
		out <- dt_icd[, .(codes = list(split_codes(get(col)))), by = row_index]
		out <- out[lengths(codes) > 0]
		out <- out[, .(code = unlist(codes)), by = row_index]
		out[, vocabulary := vocabulary]
		out
	}
	long <- rbind(build_long("ICD9_string", "ICD9CM"), build_long("ICD10_string", "ICD10"))

	# 2) drop eid entirely; capture how many codes each row (real person) had.
	# Every original row must be kept here, including people with zero codes --
	# they define the control pool, and silently dropping them would shrink
	# controls and bias case/control counts downstream.
	codes_per_person <- merge(
		data.table(row_index = dt_icd$row_index),
		long[, .(n_codes = .N), by = row_index],
		by = "row_index", all.x = TRUE)
	codes_per_person[is.na(n_codes), n_codes := 0]
	# Every real person keeps their own eid out of scope from here on -- only
	# row_index (a meaningless positional integer) and a code count survive.

	# 3) fresh synthetic person_id, one per original row, assigned in random order
	# so synthetic_id order carries no information about original row order either.
	synthetic_ids <- sample(seq_len(nrow(codes_per_person)))
	codes_per_person[, person_id := synthetic_ids]

	# Block shuffle: build the target person_id for each *code slot* by repeating
	# each synthetic person_id according to their code count, then shuffle the
	# whole vector. This preserves the marginal distribution of "codes per person"
	# without preserving which codes originally belonged together.
	slots <- rep(codes_per_person$person_id, codes_per_person$n_codes)
	slots <- sample(slots)
	stopifnot(length(slots) == nrow(long))
	long[, person_id := slots]
	long[, row_index := NULL]

	# 4) synthesize event dates uniformly at random; never reuse real dates
	# (this extraction doesn't have per-code dates anyway). Per the same-code
	# convention agreed for this fixture -- until explicit dated diagnosis
	# fields are wired in, treat the same code appearing more than once for a
	# person as two separate events -- so within each (person_id, code) group
	# dates are sampled WITHOUT replacement, guaranteeing duplicates never
	# collide onto the same day and silently look like a single event to
	# --case-rule two-dates.
	date_range <- as.integer(as.Date(c(date_min, date_max)))
	long[, event_date := as.character(as.Date(
		sample(date_range[1]:date_range[2], .N, replace = FALSE), origin = "1970-01-01")),
		by = .(person_id, code)]

	# Rename to phecodex-mapper's --events column names and drop internal id.
	events <- long[, .(person_id, code, vocabulary, event_date)]
	fwrite(events, events_out)

	cohort <- data.table(
		person_id = codes_per_person$person_id,
		sex = dt_icd$sex_normalized[match(codes_per_person$row_index, dt_icd$row_index)])
	fwrite(cohort, cohort_out)

	cat(sprintf(
		"Wrote %d de-identified events for %d synthetic people to %s and %s\n",
		nrow(events), nrow(cohort), events_out, cohort_out))
	invisible(list(events = events, cohort = cohort))
}

# --- Command-line entry point -------------------------------------------
# Only runs when this file is executed directly (Rscript ...), not when
# it's source()'d from another script/session.
.args <- commandArgs(trailingOnly = FALSE)
.is_rscript_main <- any(grepl("--file=", .args))
if (.is_rscript_main) {
	.argv <- commandArgs(trailingOnly = TRUE)
	.get_flag <- function(flag, default = NULL) {
		hit <- which(.argv == flag)
		if (length(hit) == 0) return(default)
		.argv[hit[1] + 1]
	}
	.input <- .get_flag("--input")
	if (!is.null(.input)) {
		.events_out <- .get_flag("--events-out", "events_deid.csv.gz")
		.cohort_out <- .get_flag("--cohort-out", "cohort_deid.csv.gz")
			.seed <- .get_flag("--seed", NULL)
			.max_people <- .get_flag("--max-people", NULL)
			.female_code <- .get_flag("--female-code")
			.male_code <- .get_flag("--male-code")
			if (is.null(.female_code) || is.null(.male_code)) stop("--female-code and --male-code are required")
			cat(sprintf("Reading %s ...\n", .input))
			dt_icd <- extract_ID_and_ICD_UKB(phenotype_file = .input)
		deidentify_ukb_icd_for_testing(
			dt_icd,
			events_out = .events_out,
			cohort_out = .cohort_out,
				seed = if (!is.null(.seed)) as.integer(.seed) else NULL,
				max_people = if (!is.null(.max_people)) as.integer(.max_people) else NULL,
				female_code = .female_code, male_code = .male_code)
	}
}
