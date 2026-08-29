#!/usr/bin/env Rscript

# Prepare a real UK Biobank wide phenotype extract for phecodex-map.
# This script preserves stable within-environment IDs and event assignments.
# Run only inside the approved secure environment; never commit its outputs.
#
# EVENT DATES. UKB pairs each diagnosis array with a parallel date array of the same
# length: 41270 "Diagnoses - ICD10" with 41280 "Date of first in-patient diagnosis -
# ICD10" (both 259 arrays), and 41271 with 41281 for ICD-9. Codes and dates are matched
# by ARRAY INDEX, and the pairing is checked at run time rather than assumed -- if the
# two sets of columns do not line up, this stops instead of silently mis-dating events.
#
# Read what --case-rule two-dates means on this source before using it. 41280 gives the
# date a code was FIRST recorded, so each (person, code) pair carries exactly one date.
# A person is therefore a two-dates case when they have TWO DIFFERENT codes mapping to
# the same phecode, first recorded on different days -- not when they have the same code
# at two separate visits, which this extract cannot express. That is a stricter rule than
# any-event and a looser one than "two encounters"; if you need genuine per-episode dates
# you need the HES episode tables (hesin/hesin_diag), not the wide extract.
#
# When no date columns are present the event_date column is OMITTED rather than written
# empty. An all-blank column would satisfy map-phecodes' "two-dates requires event_date"
# check and then yield zero cases in silence; omitting it makes that check fire.

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
# Dates exist only for the 41270/41271 hospital arrays. The cancer-registry and
# death-cause fields have their own date fields with different shapes, and the
# 41202/41204 fallbacks a different one again; rather than guess, those events are
# emitted undated. count(DISTINCT event_date) ignores NULLs, so an undated event simply
# does not contribute a date to the two-dates rule, which is the correct behaviour.
icd10_date_cols <- if (length(icd10_hospital)) get_cols("41280", names(header)) else character()
icd9_date_cols <- if (length(icd9_hospital)) get_cols("41281", names(header)) else character()
selected <- c(id_col, sex_col, icd9_cols, icd10_cols, icd9_date_cols, icd10_date_cols)
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

# "41270-0.5" and "f.41270.0.5" both reduce to "0.5", which is the array index the
# code column and its date column must share.
array_suffix <- function(cols, code) sub(paste0("^(f\\.)?", code, "[.-]"), "", cols)

empty_events <- function() data.table(person_id = character(), code = character(),
                                      vocabulary = character(), event_date = character())

make_events <- function(cols, vocabulary, date_cols = character(), code_field = NULL,
                        date_field = NULL) {
  if (!length(cols)) return(empty_events())
  date_for <- character()
  if (length(date_cols)) {
    date_for <- setNames(date_cols, array_suffix(date_cols, date_field))
    # Verified, not assumed: every code array index must have a matching date array
    # index. If UKB ever changes the pairing, stop rather than mis-date every event.
    wanted <- array_suffix(cols[grepl(paste0("^(f\\.)?", code_field, "[.-]"), cols)], code_field)
    orphans <- setdiff(wanted, names(date_for))
    if (length(orphans)) {
      stop(sprintf("%s has array indices with no matching %s column: %s. Codes and dates could not be paired by array index.",
                   code_field, date_field, paste(head(orphans, 5), collapse = ", ")))
    }
  }
  pieces <- lapply(cols, function(col) {
    values <- trimws(as.character(dt[[col]]))
    values[is.na(values) | !nzchar(values) | toupper(values) == "NA"] <- NA_character_
    dates <- rep(NA_character_, length(values))
    if (length(date_for) && grepl(paste0("^(f\\.)?", code_field, "[.-]"), col)) {
      dcol <- date_for[[array_suffix(col, code_field)]]
      dates <- trimws(as.character(dt[[dcol]]))
      dates[is.na(dates) | !nzchar(dates) | toupper(dates) == "NA"] <- NA_character_
    }
    rows <- data.table(person_id = dt$person_id, code_string = values, event_date = dates)[!is.na(code_string)]
    if (!nrow(rows)) return(empty_events())
    rows[, code := strsplit(code_string, "\\s+")]
    rows <- rows[, .(code = unlist(code)), by = .(person_id, event_date)][nzchar(code)]
    rows[, vocabulary := vocabulary]
    rows[, .(person_id, code, vocabulary, event_date)]
  })
  rbindlist(pieces, use.names = TRUE)
}
events <- rbind(
  make_events(icd9_cols, "ICD9CM", icd9_date_cols, "41271", "41281"),
  make_events(icd10_cols, "ICD10", icd10_date_cols, "41270", "41280"))
dated <- sum(!is.na(events$event_date))
if (!dated) {
  # Omit rather than write an all-blank column: map-phecodes refuses --case-rule
  # two-dates when event_date is absent, but an empty column passes that check and
  # then produces zero cases silently.
  events[, event_date := NULL]
}
fwrite(cohort, cohort_out)
fwrite(events, events_out)
cat(sprintf("Wrote %d people and %d events without ID or event shuffling\n", nrow(cohort), nrow(events)))
if (dated) {
  cat(sprintf("%d of %d events carry an event_date (%.1f%%); --case-rule two-dates is usable\n",
              dated, nrow(events), 100 * dated / max(nrow(events), 1)))
} else {
  cat("No date columns found (41280/41281); event_date omitted, so --case-rule two-dates will be refused\n")
}
