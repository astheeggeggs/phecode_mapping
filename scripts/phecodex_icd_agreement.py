#!/usr/bin/env python3
"""Aggregate (phecodeX x 3-character ICD-10 code) agreement from individual-level cohort data.

Runs INSIDE the secure environment that holds the phecodeX mapper outputs; exports only aggregate
counts (no person-level data).  Replicates the All by All (Lu et al.) idea -- link phecodeX to
ICD-10 codes by phenotypic correlation on the same participants -- so that a summary-statistics
resource keyed by ICD-10 code can be labelled with phecodeX without a dictionary-only proxy.

Inputs (phecode_mapping repo formats):
  --matrix   phenotype_matrix.parquet or phenotype_matrix.csv.gz: person_id + one column per phecodeX,
             1 case / 0 control / NA not evaluable (sex restriction or control exclusion)
  --events   events.csv[.gz]: person_id,code,vocabulary[,event_date]; ICD10 (WHO) rows are used,
             a person carries code C if any ICD10 event starts with C (dots removed)
  --cohort   cohort.csv[.gz]: person_id,sex (defines the denominator)
Output: one row per (phecodeX, code) with n_both > 0:
  n_eval      people evaluable for the phecodeX (non-NA)
  n_cases     phecodeX cases among them
  n_code      code carriers among them
  n_both      cases carrying the code
  r           Pearson (phi) correlation of the two 0/1 indicators over evaluable people
  frac_cases_with_code = n_both / n_cases ;  frac_code_in_cases = n_both / n_code

  python3 phecodex_icd_agreement.py --matrix ... --events ... --cohort ... --out phecodex_icd_agreement.tsv.gz
  python3 phecodex_icd_agreement.py --selftest

Diagnostic (--diagnose MS_703.1=M10,MS_718=M54 --diagnose-out diag.tsv): for each pair, the carriers of the
3-char code split into phecodeX case / non-case, tabulated by 4-char code, by number of distinct event dates
(if event_date exists) and by source column (if one exists).  Aggregate counts only; cells below --min-both
are blanked.  Written for the case where every phecodeX case carries the code but a large share of the code's
carriers are not cases although the dictionary maps every child: the table says whether the non-cases
differ by code, by date count, or by source -- i.e. whether the matrix was built from fewer events, with a
min-count rule, with a thinner dictionary than the events file, or from codes the map does not spell the
same way (see icd10_fifth_character.py).
"""
import argparse, gzip, io, sys
import numpy as np, pandas as pd
import scipy.sparse as sp


def load_matrix(path):
    if path.endswith(".parquet"):
        m = pd.read_parquet(path)
    else:
        m = pd.read_csv(path)
    m = m.set_index("person_id")
    return m


def code_indicators(events, persons):
    ev = events[events.vocabulary.astype(str).str.upper() == "ICD10"].copy()
    ev["c3"] = ev.code.astype(str).str.replace(".", "", regex=False).str.upper().str[:3]
    ev = ev[ev.c3.str.fullmatch(r"[A-Z][0-9]{2}") & ev.person_id.isin(persons)]
    ev = ev.drop_duplicates(["person_id", "c3"])
    codes = np.array(sorted(ev.c3.unique()))
    pidx = pd.Index(persons); cidx = pd.Index(codes)
    C = sp.csr_matrix((np.ones(len(ev), dtype=np.int32), (pidx.get_indexer(ev.person_id), cidx.get_indexer(ev.c3))),
                      shape=(len(persons), len(codes)))
    C.data[:] = 1
    return C, codes


def agreement(mat, C, codes, min_both=1):
    phen = mat.columns.to_numpy()
    vals = mat.to_numpy(dtype=float)
    case = sp.csr_matrix(np.nan_to_num(vals, nan=0.0) == 1.0).astype(np.int64)
    evalm = sp.csr_matrix(~np.isnan(vals)).astype(np.int64)
    # float64 BEFORE the products: n_eval * n_cases * n_code * ... reaches 1e22 on a cohort of several hundred thousand and
    # overflows int64 silently (found on real data: r far above 1 for a common code)
    n_both = (case.T @ C).toarray().astype(np.float64)    # phecodeX x code
    n_code = (evalm.T @ C).toarray().astype(np.float64)   # carriers among evaluable
    n_cases = np.asarray(case.sum(axis=0)).ravel().astype(np.float64)[:, None]
    n_eval = np.asarray(evalm.sum(axis=0)).ravel().astype(np.float64)[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        num = n_both * n_eval - n_cases * n_code
        den = np.sqrt(n_cases * (n_eval - n_cases) * n_code * (n_eval - n_code))
        r = np.where(den > 0, num / den, np.nan)
    i, j = np.nonzero(n_both >= min_both)
    assert np.nanmax(np.abs(r)) <= 1 + 1e-9, "|r| > 1: integer overflow in the phi products"
    out = pd.DataFrame(dict(phecodex=phen[i], code=codes[j], n_eval=n_eval[i, 0].astype(int), n_cases=n_cases[i, 0].astype(int),
                            n_code=n_code[i, j].astype(int), n_both=n_both[i, j].astype(int), r=r[i, j]))
    out["frac_cases_with_code"] = out.n_both / out.n_cases
    out["frac_code_in_cases"] = out.n_both / out.n_code
    return out


def diagnose(mat, events, persons, pairs, min_cell):
    """Aggregate tables explaining why carriers of CODE are not cases of PHECODEX; see module docstring."""
    ev = events[events.vocabulary.astype(str).str.upper() == "ICD10"].copy()
    ev["c"] = ev.code.astype(str).str.replace(".", "", regex=False).str.upper()
    ev["c3"] = ev.c.str[:3]; ev["c4"] = ev.c.str[:4]
    ev = ev[ev.person_id.isin(persons)]
    src = next((c for c in ("source", "src", "origin", "field") if c in ev.columns), None)
    rows = []
    for pair in pairs:
        px, code = pair.split("=")
        if px not in mat.columns: print(f"diagnose: {px} not in matrix", file=sys.stderr); continue
        e = ev[ev.c3 == code]
        status = mat[px].reindex(e.person_id.astype(str).to_numpy()).to_numpy()
        e = e.assign(status=np.where(np.isnan(status), "NA", np.where(status == 1, "case", "control")))
        per = e.drop_duplicates("person_id")
        rows.append(dict(phecodex=px, code=code, table="carriers", key="all", status="", n=len(per)))
        for st, g in per.groupby("status"): rows.append(dict(phecodex=px, code=code, table="carriers", key="all", status=st, n=len(g)))
        # by 4-char code: a person counts once per code
        for (c4, st), g in e.drop_duplicates(["person_id", "c4"]).groupby(["c4", "status"]):
            rows.append(dict(phecodex=px, code=code, table="by_4char", key=c4, status=st, n=len(g)))
        if "event_date" in e.columns:
            nd = e.groupby(["person_id", "status"]).event_date.nunique().reset_index(name="k")
            nd["k"] = nd.k.clip(upper=5).astype(str).replace("5", "5+")
            for (k, st), g in nd.groupby(["k", "status"]): rows.append(dict(phecodex=px, code=code, table="by_n_dates", key=k, status=st, n=len(g)))
        if src:
            for (sv, st), g in e.drop_duplicates(["person_id", src]).groupby([src, "status"]):
                rows.append(dict(phecodex=px, code=code, table=f"by_{src}", key=str(sv), status=st, n=len(g)))
    out = pd.DataFrame(rows)
    out.loc[out.n < min_cell, "n"] = np.nan
    return out


def selftest():
    rng = np.random.default_rng(3)
    N = 600000; persons = np.array([f"p{i}" for i in range(N)])   # large enough that int64 products would overflow
    # codes: E11 (T2D), I50 (HF), I10 (HTN); planted phecodeX: EM_202 = E11 only; CV_404 = I50 or I10 (union);
    # GU_sexonly = evaluable only in females, cases = I10 carriers among them
    has = {c: rng.random(N) < p for c, p in (("E11", 0.08), ("I50", 0.03), ("I10", 0.25), ("J45", 0.1))}
    female = rng.random(N) < 0.55
    ev = pd.concat([pd.DataFrame(dict(person_id=persons[h], code=c + ".9", vocabulary="ICD10")) for c, h in has.items()])
    ev = pd.concat([ev, pd.DataFrame(dict(person_id=persons[:5], code="250.00", vocabulary="ICD9CM"))])  # ignored vocab
    mat = pd.DataFrame(index=persons)
    mat["EM_202"] = has["E11"].astype(float)
    mat["CV_404"] = (has["I50"] | has["I10"]).astype(float)
    mat["GU_sexonly"] = np.where(female, has["I10"].astype(float), np.nan)
    mat["NULL_random"] = (rng.random(N) < 0.05).astype(float)
    C, codes = code_indicators(ev, persons)
    out = agreement(mat, C, codes).set_index(["phecodex", "code"])
    def chk(name, cond):
        print(("ok  " if cond else "FAIL"), name); return cond
    ok = True
    ok &= chk("E11 == EM_202 exactly: r = 1, both fractions 1", abs(out.loc[("EM_202", "E11"), "r"] - 1) < 1e-12 and out.loc[("EM_202", "E11"), "frac_cases_with_code"] == 1)
    ok &= chk("EM_202 vs unrelated J45: |r| < 0.03", abs(out.loc[("EM_202", "J45"), "r"]) < 0.03 if ("EM_202", "J45") in out.index else True)
    e = has["I10"].mean(); exp_frac = (has["I10"]).sum() / (has["I50"] | has["I10"]).sum()
    ok &= chk("CV_404 (union): frac_code_in_cases = 1 for I10, frac_cases_with_code matches planted share",
              out.loc[("CV_404", "I10"), "frac_code_in_cases"] == 1 and abs(out.loc[("CV_404", "I10"), "frac_cases_with_code"] - exp_frac) < 1e-12)
    ok &= chk("sex-restricted phecodeX: n_eval = number of females, r = 1 with I10", out.loc[("GU_sexonly", "I10"), "n_eval"] == female.sum() and abs(out.loc[("GU_sexonly", "I10"), "r"] - 1) < 1e-12)
    ok &= chk("null phenotype: max |r| over codes < 0.03", out.loc["NULL_random"].r.abs().max() < 0.03)
    ok &= chk("ICD9CM events ignored: no code '250'", "250" not in codes)
    # reconcile r against pandas corr on dense columns
    dense_r = np.corrcoef(mat["EM_202"].to_numpy(), has["E11"].astype(float))[0, 1]
    ok &= chk("phi from counts == numpy corrcoef (1e-12)", abs(dense_r - out.loc[("EM_202", "E11"), "r"]) < 1e-12)
    ok &= chk("all |r| <= 1 at N=600k (no int64 overflow)", out.r.abs().max() <= 1 + 1e-9)
    # diagnose: plant a phecodeX that only counts E11 carriers with code E11.1 (a thinner dictionary) and check the split
    ev2 = ev.copy(); ev2.loc[ev2.code == "E11.9", "code"] = np.where(rng.random((ev2.code == "E11.9").sum()) < 0.4, "E11.1", "E11.9")
    thin = ev2[ev2.code == "E11.1"].person_id
    mat["EM_thin"] = pd.Series(persons).isin(thin).astype(float).to_numpy()
    dg = diagnose(mat, ev2, persons, ["EM_thin=E11"], 5).set_index(["table", "key", "status"])
    ok &= chk("diagnose: all E11.1 carriers are cases, all E11.9-only carriers controls",
              ("by_4char", "E111", "control") not in dg.index and dg.loc[("by_4char", "E119", "control"), "n"] > 0
              and dg.loc[("carriers", "all", "case"), "n"] == len(thin))
    print("SELFTEST", "PASSED" if ok else "FAILED"); sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--matrix"); ap.add_argument("--events"); ap.add_argument("--cohort"); ap.add_argument("--out")
    ap.add_argument("--min-both", type=int, default=5, help="suppress cells with fewer than this many case-carriers (disclosure control)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--diagnose", default="", help="comma-separated PHECODEX=CODE pairs; writes --diagnose-out instead of --out")
    ap.add_argument("--diagnose-out", default="phecodex_icd_diagnose.tsv")
    a = ap.parse_args()
    if a.selftest: selftest()
    if not (a.matrix and a.events and a.cohort and (a.out or a.diagnose)): ap.error("--matrix, --events, --cohort and --out (or --diagnose) are required")
    cohort = pd.read_csv(a.cohort, dtype=str)
    mat = load_matrix(a.matrix)
    persons = cohort.person_id.astype(str).to_numpy()
    mat = mat.reindex(persons)                                 # cohort order; missing persons -> NA everywhere
    mat.index = mat.index.astype(str)
    events = pd.read_csv(a.events, dtype=str)
    if a.diagnose:
        dg = diagnose(mat, events, persons, a.diagnose.split(","), a.min_both)
        dg.to_csv(a.diagnose_out, sep="\t", index=False); print(f"wrote {a.diagnose_out} ({len(dg)} rows)"); return
    C, codes = code_indicators(events, persons)
    out = agreement(mat, C, codes, min_both=a.min_both)
    out.to_csv(a.out, sep="\t", index=False, float_format="%.5g")
    print(f"{len(persons)} people, {mat.shape[1]} phecodeX, {len(codes)} ICD-10 3-char codes -> {len(out)} cells with n_both >= {a.min_both}; wrote {a.out}")


if __name__ == "__main__":
    main()
