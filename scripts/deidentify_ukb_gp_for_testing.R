#!/usr/bin/env Rscript
# De-identify a UK Biobank primary-care GP clinical extract for testing.
#
# SKETCH -- not yet verified against a real extract (unlike
# deidentify_ukb_for_testing.R, which was run against real data). Column
# names/sentinel dates below are UKB's documented gp_clinical.txt shape;
# confirm against your actual extract header before trusting this as-is.
#
# UKB's primary-care data ships Read v2 / CTV3 codes, not SNOMED CT concept
# IDs directly. If you want SNOMED-coded events for testing, you must first
# cross-map read_2/read_3 -> SNOMED CT (e.g. via an NHS TRUD Read-SNOMED
# crosswalk, or Athena's own "Read" vocabulary if you download it -- it
# wasn't in the Athena vocabulary set you selected). This script assumes
# that cross-mapping has already produced a `snomed_code` column; adjust
# extract_gp_clinical_UKB() below if you're working from raw read_2/read_3
# instead.
#
# Usage:
#   Rscript scripts/deidentify_ukb_gp_for_testing.R \
#     --input gp_clinical.txt.gz \
#     --events-out events_gp_deid.csv --cohort-out cohort_gp_deid.csv \
#     --seed 1 [--max-people 5000]

suppressPackageStartupMessages(library(data.table))

# UKB placeholder/sentinel dates used in gp_clinical for missing/invalid dates.
GP_SENTINEL_DATES <- c("01/01/1901", "02/02/1902", "03/03/1903", "07/07/2037")

extract_gp_clinical_UKB <- function(path) {
  dt <- fread(path, showProgress = FALSE)
  stopifnot(all(c("eid", "event_dt") %in% names(dt)))
  # Prefer an already-cross-mapped SNOMED column if present; otherwise fall
  # back to whichever Read code column is populated (v2 then v3).
  if ("snomed_code" %in% names(dt)) {
    dt[, code := as.character(snomed_code)]
    dt[, vocabulary := "SNOMED"]
  } else if ("read_2" %in% names(dt) || "read_3" %in% names(dt)) {
    dt[, code := fifelse(!is.na(read_2) & read_2 != "", as.character(read_2), as.character(read_3))]
    dt[, vocabulary := fifelse(!is.na(read_2) & read_2 != "", "READ2", "READ3")]
  } else {
    stop("No snomed_code/read_2/read_3 column found -- check the extract header.")
  }
  dt <- dt[!is.na(code) & code != ""]
  dt <- dt[!(event_dt %in% GP_SENTINEL_DATES)]
  dt[, event_date := as.Date(event_dt, format = "%d/%m/%Y")]
  dt <- dt[!is.na(event_date)]
  dt[, .(person_id = as.character(eid), code, vocabulary, event_date)]
}

deidentify_gp_events <- function(events, seed = 1, max_people = NULL) {
  set.seed(seed)
  people <- unique(events$person_id)
  if (!is.null(max_people)) people <- sample(people, min(max_people, length(people)))
  events <- events[person_id %in% people]

  # Scramble across individuals: shuffle the person_id <-> event-history
  # assignment itself (not just relabeling IDs), so no real person's code
  # sequence survives intact.
  id_map <- data.table(person_id = people, new_id = sprintf("SIM%06d", sample(seq_along(people))))
  events <- merge(events, id_map, by = "person_id")

  # Scramble within individuals: independently jitter each event's date by
  # a random +/- offset (does not preserve real inter-event date deltas).
  n <- nrow(events)
  events[, event_date := event_date + sample(-365:365, n, replace = TRUE)]

  cohort <- data.table(person_id = id_map$new_id)
  events_out <- events[, .(person_id = new_id, code, vocabulary, event_date)]
  list(cohort = cohort, events = events_out)
}

.get_flag <- function(args, name, default = NULL) {
  i <- which(args == name)
  if (length(i) == 0) return(default)
  args[i + 1]
}

.is_rscript_main <- any(grepl("--file=", commandArgs(trailingOnly = FALSE)))
if (.is_rscript_main) {
  args <- commandArgs(trailingOnly = TRUE)
  input <- .get_flag(args, "--input")
  events_out <- .get_flag(args, "--events-out", "events_gp_deid.csv")
  cohort_out <- .get_flag(args, "--cohort-out", "cohort_gp_deid.csv")
  seed <- as.integer(.get_flag(args, "--seed", "1"))
  max_people <- .get_flag(args, "--max-people")
  if (!is.null(max_people)) max_people <- as.integer(max_people)
  if (is.null(input)) stop("--input is required")

  events <- extract_gp_clinical_UKB(input)
  result <- deidentify_gp_events(events, seed = seed, max_people = max_people)
  fwrite(result$cohort, cohort_out)
  fwrite(result$events, events_out)
  cat(sprintf("Wrote %d people, %d events -> %s, %s\n",
              nrow(result$cohort), nrow(result$events), cohort_out, events_out))
}
