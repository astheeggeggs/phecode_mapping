#!/usr/bin/env bash
# Build phecodeX_info_1.1_with_sex.csv, the --phecodex-info input of every release, from
# pinned upstream files. Upstream's 1.1 info has no sex column; 1.0's does. Carry it across
# with scripts/enrich_phecodex_sex.py (direct matches, the changes workbook's moved and new
# phecodes, and the few semantic assignments in sex_metadata.py), then check the result is
# byte for byte the file the releases were built from.
#
# Upstream's sex label is empirical: a phecode is Male or Female when more than 90% of its
# codes' use in its source EHR is in that recorded sex (PheWAS/PhecodeX README). We keep it
# as published; README.md, "Which phenotypes are sex-restricted", lists the known exceptions.
#
# usage: bash scripts/build_phecodex_sex.sh [--python PY] [--out DIR]   (from the repo root)
set -euo pipefail

PY=".venv/bin/python"
OUT="."
while [[ $# -gt 0 ]]; do
  case $1 in
    --python) PY="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

COMMIT="237f7c58daedd0188e4f5ca0201e4fc83083e53a"   # PheWAS/PhecodeXVocabulary, 2026-04-27
BASE="https://raw.githubusercontent.com/PheWAS/PhecodeXVocabulary/${COMMIT}"
INPUTS=(
  "v10_info.csv|PhecodeX%20(version%201.0)/phecodeX_info.csv|e9c21325b89645190f851412eff5ace961e9b73db85da2e2cdc27272232432f6"
  "v11_info.csv|PhecodeX%20(version%201.1)/phecodeX_info.csv|70198d06b2e87ae0204336de5eca54deced2630308231614e1820e72d82103c3"
  "changes.xlsx|PhecodeX%20(version%201.1)/PhecodeX%20changes.xlsx|03536c82028893a8140f54c925092ba4ce698062eb8a034fd61c9f117a7a0e5f"
)
OUT_INFO_SHA="0498e20d53d7dd123b8794a15902377e3e4ddde6441713fd4e37faaceb616f3a"
OUT_REVIEW_SHA="3a5b7f6008fd5ee3e737773330c324b785c75ffa0aac522d0f7fbb5085be7d2a"

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT
for entry in "${INPUTS[@]}"; do
  IFS='|' read -r name path want <<< "${entry}"
  curl -fsSL -o "${WORK}/${name}" "${BASE}/${path}"
  got=$(sha256 "${WORK}/${name}")
  if [[ "${got}" != "${want}" ]]; then
    echo "ERROR: ${path} has sha256 ${got}, expected ${want}" >&2; exit 1
  fi
done

mkdir -p "${OUT}"
"${PY}" scripts/enrich_phecodex_sex.py --v1-info "${WORK}/v10_info.csv" --v11-info "${WORK}/v11_info.csv" \
  --changes "${WORK}/changes.xlsx" --output "${OUT}/phecodeX_info_1.1_with_sex.csv" \
  --review-output "${OUT}/phecodeX_sex_assignments_review.csv"

status=0
for pair in "phecodeX_info_1.1_with_sex.csv|${OUT_INFO_SHA}" "phecodeX_sex_assignments_review.csv|${OUT_REVIEW_SHA}"; do
  IFS='|' read -r name want <<< "${pair}"
  got=$(sha256 "${OUT}/${name}")
  if [[ "${got}" == "${want}" ]]; then
    echo "${OUT}/${name}: identical to the file the releases were built from"
  else
    echo "WARNING: ${OUT}/${name} differs from the released build (sha256 ${got}, expected ${want})" >&2
    status=1
  fi
done
exit ${status}
