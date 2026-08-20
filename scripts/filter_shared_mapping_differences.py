#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv
from pathlib import Path

parser=argparse.ArgumentParser(description="Filter shared ICD mapping differences by retained phecode categories.")
parser.add_argument("--differences",type=Path,required=True); parser.add_argument("--info",type=Path,required=True); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--exclude-category",action="append",required=True); args=parser.parse_args()
with args.info.open(newline="",encoding="latin1") as f:
    excluded={r["phecode"] for r in csv.DictReader(f) if r.get("category") in set(args.exclude_category)}
with args.differences.open(newline="",encoding="utf-8") as f: source=list(csv.DictReader(f))
out=[]
for row in source:
    cm=set(filter(None,row["icd10cm_phecodes"].split("|"))); who=set(filter(None,row["who_icd10_phecodes"].split("|")))
    cm_keep=sorted(cm-excluded); who_keep=sorted(who-excluded)
    if cm_keep != who_keep:
        out.append({**row,"icd10cm_retained_phecodes":"|".join(cm_keep),"who_icd10_retained_phecodes":"|".join(who_keep),"excluded_icd10cm_phecodes":"|".join(sorted(cm&excluded)),"excluded_who_icd10_phecodes":"|".join(sorted(who&excluded))})
args.output.parent.mkdir(parents=True,exist_ok=True)
with args.output.open("w",newline="",encoding="utf-8") as f:
    writer=csv.DictWriter(f,fieldnames=list(out[0])); writer.writeheader(); writer.writerows(out)
print(f"retained differences: {len(out)}")
