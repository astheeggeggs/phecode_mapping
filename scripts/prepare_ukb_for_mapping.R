#!/usr/bin/env Rscript

# Prepare a real UK Biobank wide phenotype extract for phecodex-map.
# This script preserves stable within-environment IDs and event assignments.
# Run only inside the approved secure environment; never commit its outputs.

suppressPackageStartupMessages(library(data.table))

get_flag <- function(args, flag, default = NULL) {
  hit <- which(args == flag)
  if (!length(hit)) return(default)
  if (hit[1] == length(args)) stop(sprintf("Missing value for %s", flag))
  args[hit[1] + 1]
}

args <- commandArgs(trailingOnly = TRUE)
input <- get_flag(args, "--input")
cohort_out <- get_flag(args, "--cohort-out")
events_out <- get_flag(args, "--events-out")
female_code <- get_flag(args, "--female-code")
male_code <- get_flag(args, "--male-code")
if (is.null(input) || is.null(cohort_out) || is.null(events_out) || is.null(female_code) || is.null(male_code)) {
  stop("Required: --input, --cohort-out, --events-out, --female-code, --male-code")
}
if (!grepl("\\.gz$", cohort_out, ignore.case = TRUE) || !grepl("\\.gz$", events_out, ignore.case = TRUE)) {
  stop("--cohort-out and --events-out must end in .gz")
}

get_cols <- function(codes, names_in) {
  unique(unlist(lapply(codes, function(code) c(
    grep(paste0("^", code, "\\-"), names_in, value = TRUE),
    grep(paste0("^f\\.", code, "\\."), names_in, value = TRUE)
  ))))
}

header <- fread(input, nrow = 1, na.strings = NULL)
id_col <- if ("eid" %in% names(header)) "eid" else if ("f.eid" %in% names(header)) "f.eid" else stop("Could not find eid or f.eid")
sex_candidates <- c("f.22001.0.0", "22001-0.0")
sex_col <- intersect(sex_candidates, names(header))[1]
if (is.na(sex_col)) stop("Could not find f.22001.0.0 or 22001-0.0")
icd10_hospital <- get_cols("41270", names(header))
icd9_hospital <- get_cols("41271", names(header))
icd10_cols <- c(if (length(icd10_hospital)) icd10_hospital else get_cols(c("41202", "41204"), names(header)),
                get_cols(c("40006", "40001", "40002"), names(header)))
icd9_cols <- c(if (length(icd9_hospital)) icd9_hospital else get_cols(c("41203", "41205"), names(header)),
               get_cols("40013", names(header)))
selected <- c(id_col, sex_col, icd9_cols, icd10_cols)
types <- rep("character", length(selected)); names(types) <- selected
dt <- fread(input, na.strings = NULL, select = types)
setnames(dt, id_col, "person_id")
dt[, person_id := trimws(as.character(person_id))]
if (anyNA(dt$person_id) || any(!nzchar(dt$person_id))) stop("person_id contains missing or blank values")
if (anyDuplicated(dt$person_id)) stop("person_id must be unique")

sex <- trimws(as.character(dt[[sex_col]]))
sex[is.na(dt[[sex_col]]) | !nzchar(sex) | toupper(sex) == "NA"] <- NA_character_
bad <- unique(sex[!is.na(sex) & sex != as.character(female_code) & sex != as.character(male_code)])
if (length(bad)) stop(sprintf("Unexpected sex code(s): %s", paste(bad, collapse = ", ")))
cohort <- data.table(person_id = dt$person_id, sex = fifelse(sex == as.character(female_code), "Female", fifelse(sex == as.character(male_code), "Male", NA_character_)))

make_events <- function(cols, vocabulary) {
  if (!length(cols)) return(data.table(person_id = character(), code = character(), vocabulary = character()))
  pieces <- lapply(cols, function(col) {
    values <- trimws(as.character(dt[[col]]))
    values[is.na(values) | !nzchar(values) | toupper(values) == "NA"] <- NA_character_
    rows <- data.table(person_id = dt$person_id, code_string = values)[!is.na(code_string)]
    if (!nrow(rows)) return(data.table(person_id = character(), code = character(), vocabulary = character()))
    rows[, code := strsplit(code_string, "\\s+")]
    rows <- rows[, .(code = unlist(code)), by = person_id][nzchar(code)]
    rows[, vocabulary := vocabulary]
    rows[, .(person_id, code, vocabulary)]
  })
  rbindlist(pieces, use.names = TRUE)
}
events <- rbind(make_events(icd9_cols, "ICD9CM"), make_events(icd10_cols, "ICD10"))
fwrite(cohort, cohort_out)
fwrite(events, events_out)
cat(sprintf("Wrote %d people and %d events without ID or event shuffling\n", nrow(cohort), nrow(events)))
