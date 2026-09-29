"""Merge benchmark result folders into one table (per site x provider), per-provider totals and a per-account
credit check.

Per site and provider, per URL of the site's list (the same list for every provider):
  first try   : delivered by the first request at the provider's setting (a rival's first URL, where its
                setting was found, counts as delivered when one of its settings worked: in the rivals' favour)
  within two  : delivered by any request (bench.py gives every failed URL one more try)
Credits on the pages = every credit the page requests took (first tries, retries, the setting that
worked); credits a rival spent finding its setting (failed probes) are shown apart. Dollars per 1,000
delivered pages on the entry plan (bench.PLANS); Zyte has no credits: its dollars per site come from its
dashboard, in zyte_usd.json ({"site": dollars}) in the run folder.
URLs that no provider delivered (the site's own 404, a page kind the check does not know) are taken out
for everyone and listed. "nocredit" rows (an account out of credits; the request was sent again on
another account) are never counted.
Per account: the credits the replies reported, next to the account's balance drop (run.json).

    python report.py results/<run>
"""
import csv
import json
import os
import statistics
import sys
from collections import OrderedDict, defaultdict

import bench


def load(folders):
    """adjust.json ({provider: {site: {"credits": n, "why": ...}}}) adds credits a provider billed that no
    reply reported (read from its dashboard), e.g. a request our client abandoned at the timeout."""
    rows, runs, zyte, adjust = [], [], {}, {}
    for f in folders:
        with open(os.path.join(f, "requests.csv"), encoding="utf-8") as fh:
            rows += list(csv.DictReader(fh))
        if os.path.exists(os.path.join(f, "run.json")):
            with open(os.path.join(f, "run.json"), encoding="utf-8") as fh:
                runs.append(json.load(fh))
        if os.path.exists(os.path.join(f, "zyte_usd.json")):
            with open(os.path.join(f, "zyte_usd.json"), encoding="utf-8") as fh:
                zyte.update(json.load(fh))
        if os.path.exists(os.path.join(f, "adjust.json")):
            with open(os.path.join(f, "adjust.json"), encoding="utf-8") as fh:
                for p, sites in json.load(fh).items():
                    for s, a in sites.items():
                        adjust[(s, p)] = adjust.get((s, p), 0.0) + float(a["credits"])
    return rows, runs, zyte, adjust


def _order(r):
    return int(r.get("attempt") or 0)


def table(rows, zyte_usd, adjust=None):
    adjust = adjust or {}
    counted = [r for r in rows if r["phase"] != "nocredit"]
    ok_by_url = defaultdict(int)
    for r in counted:
        ok_by_url[r["url"]] += int(r["ok"])
    dead = sorted(u for u, n in ok_by_url.items() if n == 0)
    site_urls = defaultdict(OrderedDict)
    for r in counted:
        if r["url"] not in dead:
            site_urls[r["site"]][r["url"]] = 1
    tries = defaultdict(list)
    for r in sorted(counted, key=_order):
        tries[(r["site"], r["provider"], r["url"])].append(r)
    nocredit_only = defaultdict(set)
    for r in rows:
        if r["phase"] == "nocredit" and not tries.get((r["site"], r["provider"], r["url"])):
            nocredit_only[(r["site"], r["provider"])].add(r["url"])
    pairs = sorted({(r["site"], r["provider"]) for r in rows})
    out = []
    for site, prov in pairs:
        urls = [u for u in site_urls[site] if u not in nocredit_only[(site, prov)]]
        first_ok = final_ok = 0
        page_cr = search_cr = 0.0
        secs = []
        for u in urls:
            ts = tries.get((site, prov, u), [])
            run = [t for t in ts if t["phase"] in ("run", "smoke")]
            probes = [t for t in ts if t["phase"] == "probe"]
            if run:
                first_ok += run[0]["ok"] == "1"
            else:
                first_ok += any(t["ok"] == "1" for t in probes)
            won = [t for t in ts if t["ok"] == "1"]
            if won:
                final_ok += 1
                secs.append(float(won[0]["seconds"]))
            for t in ts:
                c = float(t["cost"] or 0)
                if t["phase"] == "probe" and t["ok"] != "1":
                    search_cr += c
                else:
                    page_cr += c
        page_cr += adjust.get((site, prov), 0.0)
        usd = None
        if prov == "zyte":
            if site in zyte_usd and final_ok:
                usd = round(float(zyte_usd[site]) / final_ok * 1000, 2)
        elif final_ok:
            price, per_plan = bench.PLANS[prov]["entry"]
            usd = round(page_cr * price / per_plan / final_ok * 1000, 2)
        out.append({"site": site, "provider": prov, "asked": len(urls), "first_ok": first_ok, "final_ok": final_ok,
                    "page_credits": round(page_cr, 3), "search_credits": round(search_cr, 3),
                    "credits_per_page": round(page_cr / final_ok, 2) if final_ok else None,
                    "usd_per_1k": usd, "median_s": round(statistics.median(secs), 1) if secs else None,
                    "secs": secs, "not_tried_no_credit": sorted(nocredit_only[(site, prov)])})
    return out, dead


def totals(t, zyte_usd):
    agg = defaultdict(lambda: {"asked": 0, "first": 0, "final": 0, "credits": 0.0, "search": 0.0, "secs": [],
                               "zyte_usd": 0.0, "zyte_sites_priced": 0, "sites": 0})
    for r in t:
        a = agg[r["provider"]]
        a["asked"] += r["asked"]; a["first"] += r["first_ok"]; a["final"] += r["final_ok"]
        a["credits"] += r["page_credits"]; a["search"] += r["search_credits"]; a["secs"] += r["secs"]
        a["sites"] += 1
        if r["provider"] == "zyte" and r["site"] in zyte_usd:
            a["zyte_usd"] += float(zyte_usd[r["site"]]); a["zyte_sites_priced"] += 1
    out = {}
    for p, a in agg.items():
        usd = None
        if p == "zyte":
            if a["final"] and a["zyte_sites_priced"] == a["sites"]:
                usd = round(a["zyte_usd"] / a["final"] * 1000, 2)
        elif a["final"]:
            price, per_plan = bench.PLANS[p]["entry"]
            usd = round(a["credits"] * price / per_plan / a["final"] * 1000, 2)
        out[p] = {"asked": a["asked"], "first_try": a["first"], "within_two": a["final"],
                  "first_pct": round(100.0 * a["first"] / a["asked"], 1) if a["asked"] else 0.0,
                  "final_pct": round(100.0 * a["final"] / a["asked"], 1) if a["asked"] else 0.0,
                  "page_credits": round(a["credits"], 3), "search_credits": round(a["search"], 3),
                  "usd_per_1k": usd, "median_s": round(statistics.median(a["secs"]), 1) if a["secs"] else None}
    return out


def by_type(t, zyte_usd):
    """Dollars per 1,000 delivered pages per provider, apart for regular and protected pages. Regular = the sites
    where Crawio charged 1 credit a page, protected = the other sites (Crawio's credit rule: 1 or 6). One average
    over both would depend on each provider's mix of the two. Empty when the run has no Crawio rows."""
    regular = {r["site"] for r in t if r["provider"] == "crawio" and r["credits_per_page"] == 1}
    if not regular:
        return {}, regular
    agg = defaultdict(lambda: {"asked": 0, "final": 0, "credits": 0.0, "zyte_usd": 0.0, "sites": 0, "priced": 0})
    for r in t:
        a = agg[(r["provider"], "regular" if r["site"] in regular else "protected")]
        a["asked"] += r["asked"]; a["final"] += r["final_ok"]; a["credits"] += r["page_credits"]
        a["sites"] += 1
        if r["provider"] == "zyte" and r["site"] in zyte_usd:
            a["zyte_usd"] += float(zyte_usd[r["site"]]); a["priced"] += 1
    out = {}
    for (p, kind), a in agg.items():
        usd = None
        if p == "zyte":
            if a["final"] and a["priced"] == a["sites"]:
                usd = round(a["zyte_usd"] / a["final"] * 1000, 2)
        elif a["final"]:
            price, per_plan = bench.PLANS[p]["entry"]
            usd = round(a["credits"] * price / per_plan / a["final"] * 1000, 2)
        out[(p, kind)] = {"asked": a["asked"], "delivered": a["final"], "usd_per_1k": usd}
    return out, regular


def accounts(rows, runs):
    """Credits the replies reported per account (every row, the setting search and nocredit rows included:
    it is what the account was charged), next to the account's balance drop from run.json."""
    ours = defaultdict(lambda: [0, 0.0])
    for r in rows:
        k = (r["provider"], r.get("account") or "")
        ours[k][0] += 1
        ours[k][1] += float(r["cost"] or 0)
    drop = {}
    for run in runs:
        for p, accts in (run.get("balances_before") or {}).items():
            for label, b in accts.items():
                a = ((run.get("balances_after") or {}).get(p) or {}).get(label)
                if b is not None and a is not None:
                    drop[(p, label)] = drop.get((p, label), 0.0) + (b - a)
    out = []
    for (p, label), (n, cr) in sorted(ours.items()):
        d = drop.get((p, label))
        out.append({"provider": p, "account": label, "requests": n, "credits_reported": round(cr, 3),
                    "balance_drop": d, "gap": round(d - cr, 3) if d is not None else None})
    return out


def main(folders):
    rows, runs, zyte_usd, adjust = load(folders)
    t, dead = table(rows, zyte_usd, adjust)
    tt = totals(t, zyte_usd)
    acc = accounts(rows, runs)
    provs = sorted({r["provider"] for r in t}, key=lambda p: (p != "crawio", p))
    sites = sorted({r["site"] for r in t})
    cell = {(r["site"], r["provider"]): r for r in t}
    if dead:
        print("Taken out for every provider (no one delivered them): " + ", ".join(dead) + "\n")
    print("| Site | " + " | ".join(provs) + " |")
    print("|---|" + "---|" * len(provs))
    for s in sites:
        cells = []
        for p in provs:
            r = cell.get((s, p))
            cells.append("" if not r else "%d/%d (1st %d), %s cr, %s, %ss" % (
                r["final_ok"], r["asked"], r["first_ok"], r["credits_per_page"],
                ("$%.2f" % r["usd_per_1k"]) if r["usd_per_1k"] is not None else "$?", r["median_s"]))
        print("| %s | %s |" % (s, " | ".join(cells)))
    print("\n| Provider | First try | Within two tries | $ per 1k delivered (entry plan) | Median s | Setting search cr |")
    print("|---|---|---|---|---|---|")
    for p in provs:
        a = tt[p]
        print("| %s | %d/%d (%.1f%%) | %d/%d (%.1f%%) | %s | %s | %s |" % (
            p, a["first_try"], a["asked"], a["first_pct"], a["within_two"], a["asked"], a["final_pct"],
            ("$%.2f" % a["usd_per_1k"]) if a["usd_per_1k"] is not None else "?", a["median_s"], a["search_credits"]))
    bt, regular = by_type(t, zyte_usd)
    if bt:
        print("\nRegular pages = %s (Crawio: 1 credit a page); protected pages = the other sites." % ", ".join(sorted(regular)))
        print("\n| Provider | $ per 1k regular pages | $ per 1k protected pages |")
        print("|---|---|---|")
        for p in provs:
            cells = []
            for kind in ("regular", "protected"):
                c = bt.get((p, kind))
                if not c or c["usd_per_1k"] is None:
                    cells.append("?")
                else:
                    cells.append("$%.2f%s" % (c["usd_per_1k"], "" if c["delivered"] == c["asked"]
                                              else " (%d of %d)" % (c["delivered"], c["asked"])))
            print("| %s | %s |" % (p, " | ".join(cells)))
    print("\n| Provider | Account | Requests | Credits reported | Balance drop | Gap |")
    print("|---|---|---|---|---|---|")
    for a in acc:
        print("| %s | %s | %d | %s | %s | %s |" % (a["provider"], a["account"], a["requests"], a["credits_reported"],
                                                  a["balance_drop"], a["gap"]))
    skipped = [(r["site"], r["provider"], u) for r in t for u in r["not_tried_no_credit"]]
    if skipped:
        print("\nWARNING: not tried (every account out of credits): %s" % skipped)
    for r in t:
        r.pop("secs")
    with open(os.path.join(folders[0], "report.json"), "w", encoding="utf-8") as fh:
        json.dump({"dead_urls": dead, "table": t, "totals": tt, "accounts": acc,
                   "by_type": {"%s/%s" % k: v for k, v in bt.items()}}, fh, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
