"""Audit foundation_filings.total_grants_paid against authoritative efile-XML
grants-paid figures, for every donor foundation and every tax year we have XML.

Authoritative element by form type:
  990-PF : TotalGrantOrContriPdDurYrAmt  (Part I line 25, contributions/grants paid)
  990    : Part IX line 1+2+3 = GrantsToDomesticOrgsGrp + GrantsToDomesticIndividualsGrp
           + ForeignGrantsGrp  (each /TotalAmt)
  990-EZ : GrantsAndSimilarAmountsPaidAmt (line 10)

Every XML's Filer EIN is checked against the expected EIN before its value is
trusted. Reads IRS indices on disk to enumerate filings; locates XML via
all_filing_locations.json (path-corrected) with a zip-scan fallback.

Pass --apply to write corrections into foundation_filings.total_grants_paid.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://www.irs.gov/efile}"
DB = "data/institutional_risk.db"
ZIP_DIR = Path("data/raw_sources/irs_zips")
INDEX_DIR = Path("data/raw_sources/irs_index")
LOCMAP = "data/reference/all_filing_locations.json"


def fix_zip_path(p: str) -> str:
    """Location map was built under the old data\\irs_zips layout."""
    p = p.replace("data\\irs_zips\\", "data\\raw_sources\\irs_zips\\")
    p = p.replace("data/irs_zips/", "data/raw_sources/irs_zips/")
    return p


def load_locmap() -> dict:
    if not os.path.exists(LOCMAP):
        return {}
    raw = json.load(open(LOCMAP, encoding="utf-8"))
    return {k: (fix_zip_path(v[0]), v[1]) for k, v in raw.items()}


def build_zip_index(needed: set[str]) -> dict:
    """Single pass over all zips: object_id -> (zip, inner_xml_path)."""
    found = {}
    for z in sorted(glob.glob(str(ZIP_DIR / "*.zip"))):
        try:
            with zipfile.ZipFile(z) as zf:
                for n in zf.namelist():
                    # inner names look like .../2023..NNN_public.xml
                    base = n.rsplit("/", 1)[-1]
                    oid = base.split("_")[0]
                    if oid in needed and oid not in found:
                        found[oid] = (z, n)
        except zipfile.BadZipFile:
            continue
    return found


def _amt(el, path):
    if el is None:
        return 0
    v = el.findtext(path)
    try:
        return int(v) if v else 0
    except ValueError:
        return 0


def extract_grants(root, return_type: str) -> int | None:
    r990 = root.find(f".//{NS}IRS990")
    rpf = root.find(f".//{NS}IRS990PF")
    rez = root.find(f".//{NS}IRS990EZ")
    if return_type == "990PF":
        node = rpf
        if node is None:
            return None
        # Part I line 25 col (a) "per books" = ProPublica contrpdpbks. Prefer it
        # so 990-PF values stay on one consistent measure; fall back to others.
        for tag in ("ContriPaidRevAndExpnssAmt", "TotalGrantOrContriPdDurYrAmt",
                    "ContriPaidDsbrsChrtblAmt", "ContributionsGiftsGrantsPaidAmt"):
            v = node.findtext(f".//{NS}{tag}")
            if v:
                try:
                    return int(v)
                except ValueError:
                    pass
        return None
    if return_type == "990":
        node = r990
        if node is None:
            return None
        total = (_amt(node, f".//{NS}GrantsToDomesticOrgsGrp/{NS}TotalAmt")
                 + _amt(node, f".//{NS}GrantsToDomesticIndividualsGrp/{NS}TotalAmt")
                 + _amt(node, f".//{NS}ForeignGrantsGrp/{NS}TotalAmt"))
        return total
    if return_type in ("990EZ", "990-EZ"):
        node = rez
        if node is None:
            return None
        v = node.findtext(f"{NS}GrantsAndSimilarAmountsPaidAmt")
        try:
            return int(v) if v else 0
        except ValueError:
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    donors = {}  # ein -> (ticker, name)
    for r in conn.execute("select ein, donor_ticker, foundation_name, relationship from donor_foundations"):
        donors[r["ein"]] = (r["donor_ticker"], r["foundation_name"], r["relationship"])

    # current DB values: (ein, tax_year) -> grants_paid
    current = {}
    for r in conn.execute("select ein, tax_year, total_grants_paid, form_type from foundation_filings"):
        current[(r["ein"], r["tax_year"])] = (r["total_grants_paid"], r["form_type"])

    locmap = load_locmap()

    # enumerate filings from indices
    filings = {}  # (ein, tax_year) -> dict(object_id, return_type, tax_period)
    for idx in sorted(glob.glob(str(INDEX_DIR / "index_*.csv"))):
        with open(idx, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ein = row.get("EIN")
                if ein not in donors:
                    continue
                if row["RETURN_TYPE"] == "990T":
                    continue
                tp = row["TAX_PERIOD"]
                if not tp or len(tp) < 6:
                    continue
                ty = int(tp[:4])
                key = (ein, ty)
                # prefer latest SUB_DATE if duplicate
                prev = filings.get(key)
                if prev is None or row.get("SUB_DATE", "") > prev.get("sub_date", ""):
                    filings[key] = {"object_id": row["OBJECT_ID"],
                                    "return_type": row["RETURN_TYPE"],
                                    "tax_period": tp,
                                    "sub_date": row.get("SUB_DATE", "")}

    # locate every needed object in one pass (those not already in locmap)
    needed = {m["object_id"] for m in filings.values() if m["object_id"] not in locmap}
    zip_index = build_zip_index(needed)
    print(f"Located {len(zip_index)}/{len(needed)} objects via zip scan; {len(locmap)} in locmap.")

    results = []
    for (ein, ty), meta in sorted(filings.items()):
        ticker, name, rel = donors[ein]
        oid = meta["object_id"]
        loc = locmap.get(oid) or zip_index.get(oid)
        if not loc:
            results.append({"ein": ein, "ticker": ticker, "ty": ty, "status": "NO_XML_LOCATED",
                            "rtype": meta["return_type"], "xml": None,
                            "current": current.get((ein, ty), (None, None))[0]})
            continue
        zp, xp = loc
        if not os.path.exists(zp):
            results.append({"ein": ein, "ticker": ticker, "ty": ty, "status": "ZIP_MISSING",
                            "rtype": meta["return_type"], "xml": None,
                            "current": current.get((ein, ty), (None, None))[0]})
            continue
        try:
            with zipfile.ZipFile(zp) as zf:
                root = ET.parse(zf.open(xp)).getroot()
        except Exception as e:
            results.append({"ein": ein, "ticker": ticker, "ty": ty, "status": f"PARSE_ERR:{e}",
                            "rtype": meta["return_type"], "xml": None,
                            "current": current.get((ein, ty), (None, None))[0]})
            continue
        hdr = root.find(f"{NS}ReturnHeader")
        filer = hdr.find(f"{NS}Filer") if hdr is not None else None
        filer_ein = filer.findtext(f"{NS}EIN") if filer is not None else None
        rtype = hdr.findtext(f"{NS}ReturnTypeCd") if hdr is not None else meta["return_type"]
        identity_ok = (filer_ein == ein)
        xml_val = extract_grants(root, rtype)
        cur = current.get((ein, ty), (None, None))[0]
        if not identity_ok:
            status = "IDENTITY_MISMATCH"
        elif xml_val is None:
            status = "XML_NO_FIELD"
        elif cur is None:
            status = "FILL"
        elif cur == xml_val:
            status = "MATCH"
        elif abs((xml_val - cur)) / max(xml_val, cur, 1) <= 0.01:
            status = "MATCH~"
        else:
            status = "DIFF"
        results.append({"ein": ein, "ticker": ticker, "ty": ty, "rtype": rtype,
                        "status": status, "current": cur, "xml": xml_val,
                        "filer_ein": filer_ein, "zip": zp, "xp": xp})

    # report
    order = {"DIFF": 0, "FILL": 1, "IDENTITY_MISMATCH": 2, "XML_NO_FIELD": 3,
             "NO_XML_LOCATED": 4, "ZIP_MISSING": 5, "MATCH~": 6, "MATCH": 7}
    results.sort(key=lambda r: (order.get(r["status"], 9), r["ticker"], r["ty"]))
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    print("STATUS COUNTS:", dict(counts))
    print()
    print(f"{'tick':5} {'yr':>4} {'rtype':>6} {'status':17} {'current':>14} {'xml':>14}")
    for r in results:
        cur = f"{r['current']:,}" if r["current"] is not None else "--"
        xv = f"{r['xml']:,}" if r.get("xml") is not None else "--"
        if r["status"] in ("MATCH",):
            continue  # hide perfect matches in console
        print(f"{r['ticker']:5} {r['ty']:>4} {str(r['rtype']):>6} {r['status']:17} {cur:>14} {xv:>14}")

    json.dump(results, open("data/reference/grants_paid_xml_audit.json", "w"), indent=1)
    print("\nWrote data/reference/grants_paid_xml_audit.json")

    if args.apply:
        n = 0
        for r in results:
            if r["status"] in ("DIFF", "FILL") and r.get("xml") is not None:
                row = conn.execute("select id, raw_json from foundation_filings where ein=? and tax_year=?",
                                   (r["ein"], r["ty"])).fetchone()
                if row is None:
                    continue
                raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
                raw["_grants_paid_basis"] = f"efile_xml_{r['rtype']}"
                raw["_grants_paid_value"] = r["xml"]
                conn.execute("update foundation_filings set total_grants_paid=?, raw_json=? where id=?",
                             (r["xml"], json.dumps(raw), row["id"]))
                n += 1
        conn.commit()
        print(f"\nAPPLIED {n} corrections (DIFF + FILL).")
    conn.close()


if __name__ == "__main__":
    main()
