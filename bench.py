"""Crawio vs other scraping APIs: the same URLs, sent to every API at the same time.

What it measures, per site and API: pages delivered (a page counts only when it holds the site's real data,
per-site `check`, is at least 30 KB and is no block page), median seconds, and credits per delivered page
(from each API's own cost header, else its documented price for the setting used). The report turns credits
into dollars per 1,000 delivered pages on each API's entry plan (PLANS).

How: for each site, every API runs in parallel with its own pool (CONCURRENCY at once). Each competitor first
finds its cheapest setting that works on the site (its ladder: default, then premium proxy, then rendering...),
probing on the site's first URL; the probes are logged apart ("probe"). Crawio has one automatic setting.
Every failed URL gets one more try ("retry"): a competitor steps up one setting, Crawio asks again. No warm-up
for anyone and no country parameter.

Keys come from the environment only: CRAWIO_BENCH_KEY, SCRAPEDO_KEY, ZENROWS_KEY, SCRAPERAPI_KEY,
SCRAPINGBEE_KEY, ZYTE_KEY. Several accounts of one API = comma-separated keys (labelled A, B, C; each site starts
on the account with the most credits left). An API without a key is skipped. Keys never reach the output files.
Zyte reports no per-request cost: its dollars come from its dashboard after the run (report.py, zyte_usd.json).

    python bench.py --set b --dry                       (URLs + accounts, no page requests)
    python bench.py --set b --per-site 10 --out results/run
    python report.py results/run
"""
import argparse
import base64
import concurrent.futures as cf
import csv
import gzip
import hashlib
import json
import os
import re
import statistics
import threading
import time
import urllib.parse

import requests

CONCURRENCY = 5
TIMEOUT_S = 120

KW_SHOP = ["headphones", "laptop", "coffee+maker", "running+shoes", "air+fryer", "backpack", "desk+lamp",
           "water+bottle", "phone+case", "yoga+mat"]
ZIPS = ["78701", "78702", "78703", "78704", "78705", "78721", "78722", "78723", "78731", "78745"]

BLOCKED = re.compile(r"Just a moment\.\.\.|Attention Required|Access Denied|has been denied|Pardon Our Interruption|"
                     r"captcha-delivery\.com|px-captcha|Robot Check|Request unsuccessful\..{0,60}incident ID|"
                     r"Please enable JS and disable any ad blocker|verify you are human", re.I)

SITES = {
    "books": {"urls": ["https://books.toscrape.com/catalogue/page-%d.html" % i for i in range(1, 11)],
              "check": r'class="product_pod"'},
    "wikipedia": {"urls": ["https://en.wikipedia.org/wiki/%s" % a for a in
                           ["Web_scraping", "Python_(programming_language)", "Austin,_Texas", "Coffee", "Solar_energy",
                            "Electric_car", "Machine_learning", "Olympic_Games", "Pizza", "Mount_Everest"]],
                  "check": r'id="firstHeading"'},
    "amazon": {"urls": ["https://www.amazon.com/s?k=%s" % k for k in KW_SHOP],
               "check": r'data-component-type="s-search-result"'},
    "walmart": {"urls": ["https://www.walmart.com/search?q=%s" % k for k in KW_SHOP],
                "check": r'data-item-id|"itemStacks"'},
    "etsy": {"urls": ["https://www.etsy.com/search?q=%s" % k for k in
                      ["candles", "necklace", "wall+art", "mug", "earrings", "planner", "tote+bag", "ring",
                       "sticker", "blanket"]],
             "check": r"data-listing-id|<title[^>]*>[^<]*Etsy"},
    "zillow": {"urls": ["https://www.zillow.com/austin-tx-%s/" % z for z in ZIPS],
               "check": r'listResults|"searchResults"|<title[^>]*>[^<]*Homes For Sale[^<]*Zillow'},
    "realtor": {"urls": ["https://www.realtor.com/realestateandhomes-search/%s" % z for z in ZIPS],
                "check": r'property_id|data-testid="property-card|<title[^>]*>[^<]*Homes for Sale[^<]*Realtor'},
    "redfin": {"urls": ["https://www.redfin.com/zipcode/%s" % z for z in ZIPS],
               "check": r"<title[^>]*>[^<]*Homes for Sale[^<]*Redfin"},
    "trulia": {"urls": ["https://www.trulia.com/TX/Austin/%s/" % z for z in ZIPS],
               "check": r"<title[^>]*>[^<]*Homes For Sale[^<]*Trulia"},
    "idealista": {"urls": ["https://www.idealista.com/venta-viviendas/%s/" % c for c in
                           ["madrid-madrid", "barcelona-barcelona", "valencia-valencia", "sevilla-sevilla",
                            "malaga-malaga", "zaragoza-zaragoza", "bilbao-vizcaya", "alicante-alicante",
                            "murcia-murcia", "granada-granada"]],
                  "check": r"item-info-container|class=\"item-link|<title[^>]*>[^<]*idealista"},
    "indeed": {"urls": ["https://www.indeed.com/jobs?q=%s&l=Austin%%2C+TX" % k for k in
                        ["python", "data+analyst", "nurse", "marketing", "accountant", "sales", "designer",
                         "teacher", "engineer", "customer+service"]],
               "check": r"data-jk=|job_seen_beacon|<title[^>]*>[^<]*Jobs[^<]*Indeed"},
    "g2": {"urls": ["https://www.g2.com/products/%s/reviews" % s for s in
                    ["slack", "asana", "trello", "notion", "jira", "figma", "canva", "mailchimp",
                     "hubspot-marketing-hub", "salesforce-salesforce-sales-cloud"]],
           "check": r"<title[^>]*>[^<]*Reviews[^<]*\| G2</title>"},
    "glassdoor": {"urls": ["https://www.glassdoor.com/Reviews/%s-Reviews-E%s.htm" % p for p in
                           [("Google", "9079"), ("Microsoft", "1651"), ("Amazon", "6036"), ("Apple", "1138"),
                            ("Meta", "40772"), ("IBM", "354"), ("Netflix", "11891"), ("Salesforce", "11159"),
                            ("Oracle", "1737"), ("Intel", "1519")]],
                  "check": r"<title[^>]*>[^<]*Reviews[^<]*Glassdoor"},
    "tripadvisor": {"urls": ["https://www.tripadvisor.com/Hotels-g%s-%s-Hotels.html" % p for p in
                             [("30196", "Austin_Texas"), ("60763", "New_York_City_New_York"),
                              ("35805", "Chicago_Illinois"), ("60713", "San_Francisco_California"),
                              ("32655", "Los_Angeles_California"), ("34438", "Miami_Florida"),
                              ("60878", "Seattle_Washington"), ("60745", "Boston_Massachusetts"),
                              ("45963", "Las_Vegas_Nevada"), ("28970", "Washington_DC_District_of_Columbia")]],
                    "check": r"<title[^>]*>[^<]*Hotels[^<]*Tripadvisor"},
    "booking": {"urls": ["https://www.booking.com/city/us/%s.html" % c for c in
                         ["austin", "new-york", "chicago", "san-francisco", "los-angeles", "miami", "seattle",
                          "boston", "las-vegas", "washington"]],
                "check": r"<title[^>]*>[^<]*Hotels"},
    "homedepot": {"urls": ["https://www.homedepot.com/s/%s" % k for k in
                           ["drill", "hammer", "paint", "ladder", "circular+saw", "toolbox", "flashlight",
                            "drill+bits", "screwdriver", "tape+measure"]],
                  "check": r"<title[^>]*>[^<]*Home Depot"},
    "ebay": {"urls": ["https://www.ebay.com/sch/i.html?_nkw=%s" % k for k in KW_SHOP],
             "check": r"<title[^>]*>[^<]*eBay"},
    "bestbuy": {"urls": ["https://www.bestbuy.com/site/searchpage.jsp?st=%s" % k for k in KW_SHOP],
                "check": r"<title[^>]*>[^<]*Best Buy"},
    "yelp": {"urls": ["https://www.yelp.com/search?find_desc=%s&find_loc=Austin%%2C+TX" % k for k in
                      ["coffee", "pizza", "tacos", "sushi", "bakery", "bbq", "brunch", "gym", "dentist", "plumber"]],
             "check": r"<title[^>]*>[^<]*Yelp"},
    "crunchbase": {"urls": ["https://www.crunchbase.com/organization/%s" % s for s in
                            ["openai", "anthropic", "stripe", "airbnb", "databricks", "canva", "figma",
                             "revolut", "notion-labs", "space-exploration-technologies"]],
                   "check": r"<title[^>]*>[^<]*Crunchbase"},
}

# A second, fresh URL set per site (--set b, 2026-09-29): nothing in it was requested by any earlier run,
# so no provider (ours included) can have learned or cached these exact pages.
KW_SHOP_B = ["wireless+mouse", "blender", "sunglasses", "notebook", "hiking+boots", "rice+cooker", "monitor",
             "tent", "earbuds", "kettle"]
ZIPS_B = ["78741", "78746", "78748", "78749", "78750", "78751", "78752", "78753", "78757", "78758"]
URLS_B = {
    "wikipedia": ["https://en.wikipedia.org/wiki/%s" % a for a in
                  ["Tokyo", "Photosynthesis", "Jazz", "Volcano", "Chess", "Honey", "Marathon", "Bitcoin",
                   "Great_Wall_of_China", "Rainforest"]],
    "amazon": ["https://www.amazon.com/s?k=%s" % k for k in KW_SHOP_B],
    "ebay": ["https://www.ebay.com/sch/i.html?_nkw=%s" % k for k in KW_SHOP_B],
    "homedepot": ["https://www.homedepot.com/s/%s" % k for k in
                  ["garden+hose", "shovel", "mulch", "drill+driver", "work+gloves", "level", "pliers", "wrench",
                   "sandpaper", "extension+cord"]],
    "zillow": ["https://www.zillow.com/austin-tx-%s/" % z for z in ZIPS_B],
    "realtor": ["https://www.realtor.com/realestateandhomes-search/%s" % z for z in ZIPS_B],
    "idealista": ["https://www.idealista.com/venta-viviendas/%s/" % c for c in
                  ["cordoba-cordoba", "valladolid-valladolid", "vigo-pontevedra", "gijon-asturias",
                   "santander-cantabria", "salamanca-salamanca", "burgos-burgos", "toledo-toledo", "cadiz-cadiz",
                   "leon-leon"]],
    "indeed": ["https://www.indeed.com/jobs?q=%s&l=Austin%%2C+TX" % k for k in
               ["data+scientist", "product+manager", "hvac", "forklift", "social+worker", "copywriter",
                "phlebotomist", "dispatcher", "recruiter", "web+developer"]],
    "glassdoor": ["https://www.glassdoor.com/Reviews/%s-Reviews-E%s.htm" % p for p in
                  [("Adobe", "1090"), ("NVIDIA", "7633"), ("Tesla", "43129"), ("Uber", "575263"),
                   ("Airbnb", "391850"), ("Spotify", "408251"), ("Deloitte", "2763"), ("Accenture", "4138"),
                   ("Walmart", "715"), ("Starbucks", "2202")]],
    "g2": ["https://www.g2.com/products/%s/reviews" % s for s in
           ["hubspot-sales-hub", "zendesk-for-customer-service", "microsoft-teams", "google-workspace", "dropbox",
            "shopify", "zoom-workplace", "monday-com", "clickup", "airtable"]],
    # checked in Chrome 2026-09-29: zendesk-support-suite redirects here; quickbooks-online has an old title
    # ("... Reviews, Features, and Pricing 2024", no "| G2") the check cannot read, replaced by zoom-workplace
    "booking": ["https://www.booking.com/city/us/%s.html" % c for c in
                ["denver", "phoenix", "san-diego", "dallas", "houston", "atlanta", "orlando", "nashville",
                 "new-orleans", "portland"]],
}

# Live pricing pages, read 2026-09-28 (entry plan, ~$100 plan): (price USD, credits)
PLANS = {
    "crawio": {"entry": (29, 300_000), "mid": (99, 1_500_000), "src": "https://crawio.com/pricing/"},
    "scrapedo": {"entry": (29, 250_000), "mid": (99, 1_250_000), "src": "https://scrape.do/pricing/"},
    "zenrows": {"entry": (57, 250_000), "mid": (165, 1_200_000), "src": "https://www.zenrows.com/pricing"},
    "scraperapi": {"entry": (49, 100_000), "mid": (149, 1_000_000), "src": "https://www.scraperapi.com/pricing/"},
    "scrapingbee": {"entry": (49, 250_000), "mid": (99, 1_000_000), "src": "https://www.scrapingbee.com/pricing/"},
}
# Zyte API, dollars per 1,000 requests by tier 1-5 (zyte.com/pricing, read 2026-09-28): pay as you go and
# the $100/month commitment, for the raw response and for browser rendering
ZYTE_TIERS = {"entry": {"http": [0.13, 0.23, 0.44, 0.70, 1.27], "browser": [1.01, 2.01, 4.02, 8.04, 16.08]},
              "mid": {"http": [0.10, 0.17, 0.33, 0.53, 0.95], "browser": [0.75, 1.50, 3.00, 6.00, 12.00]}}


def _rid(headers):
    """The provider's own request id, when its reply carries one (any header named like a request id)."""
    for k, v in headers.items():
        kl = k.lower()
        if "request-id" in kl or "request_id" in kl or "requestid" in kl or kl.endswith("-rid"):
            return v
    return ""


def _crawio(url, _params, key):
    r = requests.post("https://api.crawio.com/scrape", timeout=TIMEOUT_S,
                      headers={"x-api-key": key, "content-type": "application/json"},
                      json={"url": url, "timeout": 90})
    try:
        d = r.json()
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    body = d.get("body") if isinstance(d.get("body"), str) else ""
    cost = d.get("cost") if r.status_code == 200 else 0
    meta = {k: v for k, v in d.items() if k != "body"}
    return r.status_code, d.get("status"), body, cost, meta, d.get("request_id") or ""


def _get(base, key_param, cost_header, url, params, key, scale=1):
    r = requests.get(base, params={key_param: key, "url": url, **params}, timeout=TIMEOUT_S)
    cost = r.headers.get(cost_header)
    if cost is None:                                         # the header's exact name varies: any "*cost*" header
        cost = next((v for k, v in r.headers.items() if "cost" in k.lower()), None)
    try:
        cost = float(cost) * scale if cost is not None else None
    except ValueError:
        cost = None
    hdrs = dict(r.headers)
    return r.status_code, r.status_code, r.text, cost, hdrs, _rid(hdrs)


def _zyte(url, params, key):
    field = "browserHtml" if params.get("browserHtml") else "httpResponseBody"
    r = requests.post("https://api.zyte.com/v1/extract", auth=(key, ""), timeout=TIMEOUT_S,
                      json={"url": url, field: True})
    try:
        d = r.json()
    except ValueError:
        d = {}
    if field == "httpResponseBody":
        try:
            body = base64.b64decode(d.get("httpResponseBody") or "").decode("utf-8", "replace")
        except (ValueError, TypeError):
            body = ""
    else:
        body = d.get("browserHtml") or ""
    hdrs = dict(r.headers)
    return r.status_code, d.get("statusCode"), body, 0, hdrs, _rid(hdrs)   # cost: Zyte's dashboard, after the run


PROVIDERS = {
    "crawio": {"env": "CRAWIO_BENCH_KEY", "ladder": [{}], "price": [None], "call": _crawio},
    "scrapedo": {"env": "SCRAPEDO_KEY",
                 "ladder": [{}, {"render": "true"}, {"super": "true"}, {"super": "true", "render": "true"}],
                 "price": [1, 5, 10, 25],
                 "call": lambda u, p, k: _get("https://api.scrape.do/", "token", "Scrape.do-Request-Cost", u, p, k)},
    "zenrows": {"env": "ZENROWS_KEY",
                "ladder": [{}, {"js_render": "true"}, {"premium_proxy": "true"},
                           {"premium_proxy": "true", "js_render": "true"}],
                "price": [1, 5, 10, 25],
                # X-Request-Cost is in thousands of credits (measured 2026-09-28: 0.001 per plain page,
                # the dashboard moved 160 -> 162 credits for two of them)
                "call": lambda u, p, k: _get("https://api.zenrows.com/v1/", "apikey", "X-Request-Cost", u, p, k,
                                             scale=1000)},
    "scraperapi": {"env": "SCRAPERAPI_KEY",
                   "ladder": [{}, {"render": "true"}, {"premium": "true"}, {"premium": "true", "render": "true"},
                              {"ultra_premium": "true"}], "price": [1, 10, 10, 25, 30],
                   "call": lambda u, p, k: _get("https://api.scraperapi.com/", "api_key", "sa-credit-cost", u, p, k)},
    "scrapingbee": {"env": "SCRAPINGBEE_KEY",
                    "ladder": [{"render_js": "false"}, {"render_js": "true"},
                               {"render_js": "false", "premium_proxy": "true"}, {"premium_proxy": "true"},
                               {"stealth_proxy": "true"}], "price": [1, 5, 10, 25, 75],
                    "call": lambda u, p, k: _get("https://app.scrapingbee.com/api/v1/", "api_key", "Spb-cost", u, p,
                                                 k)},
    "zyte": {"env": "ZYTE_KEY", "ladder": [{}, {"browserHtml": True}], "price": [0, 0], "call": _zyte},
}


def balance(provider, key):
    """Credits left on one account, from the provider's own account API (None: no such API, or it failed)."""
    try:
        if provider == "scrapedo":
            d = requests.get("https://api.scrape.do/info", params={"token": key}, timeout=30).json()
            return float(d["RemainingMonthlyRequest"])
        if provider == "scrapingbee":
            d = requests.get("https://app.scrapingbee.com/api/v1/usage", params={"api_key": key}, timeout=30).json()
            return float(d["max_api_credit"]) - float(d["used_api_credit"])
        if provider == "zenrows":
            d = requests.get("https://api.zenrows.com/v1/subscriptions/self/details", headers={"X-API-Key": key},
                             timeout=30).json()
            return float(d["credit_limit"]) - float(d["usage_credits"])
        if provider == "scraperapi":
            d = requests.get("https://api.scraperapi.com/account", params={"api_key": key}, timeout=30).json()
            return float(d["requestLimit"]) - float(d["requestCount"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    return None


# Accounts: a provider's env variable may hold several keys, comma-separated; they are labelled A, B, C...
# in that order. Only labels reach files and output, never a key.
KEYS = {}                  # provider -> [(label, key)]
ACTIVE = {}                # provider -> label in use (None: every account out of credits)
SPENT = set()              # (provider, label) found out of credits during the run
ACCT_LOCK = threading.Lock()
NOCREDIT_HTTP = {401, 402, 403, 429}


def load_keys(providers):
    for p in providers:
        keys = [k.strip() for k in os.environ.get(PROVIDERS[p]["env"], "").split(",") if k.strip()]
        KEYS[p] = [(chr(65 + i), k) for i, k in enumerate(keys)]


def balances(providers):
    return {p: {label: balance(p, key) for label, key in KEYS[p]} for p in providers}


def pick_account(provider):
    """The account with the most credits left (never one found spent); the first when there is no balance API."""
    best = None
    for label, key in KEYS[provider]:
        if (provider, label) in SPENT:
            continue
        b = balance(provider, key)
        if best is None or (b is not None and (best[1] is None or b > best[1])):
            best = (label, b)
    with ACCT_LOCK:
        ACTIVE[provider] = best[0] if best else None
        return ACTIVE[provider]


LOCK = threading.Lock()
SEQ = [0]
OUT = {"dir": None}


def _record(writer, key, **row):
    """One CSV row per attempt, plus the page (gzipped) and the reply's headers under bodies/. The key is
    scrubbed from everything written, in case a reply echoes it."""
    body, hdrs = row.pop("body"), row.pop("headers")
    scrub = (lambda s: s.replace(key, "<key>")) if key else (lambda s: s)
    with LOCK:
        SEQ[0] += 1
        row = {"attempt": SEQ[0], **row}
    row["error"] = scrub(row["error"])
    if OUT["dir"]:
        d = os.path.join(OUT["dir"], "bodies", row["provider"], row["site"])
        os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, "%05d.html.gz" % row["attempt"]), "wt", encoding="utf-8") as fh:
            fh.write(scrub(body))
        with open(os.path.join(d, "%05d.json" % row["attempt"]), "w", encoding="utf-8") as fh:
            fh.write(scrub(json.dumps({"row": row, "headers": hdrs}, indent=1, default=str)))
    with LOCK:
        writer.writerow(row)
    return row


def attempt(provider, site, url, level, phase, writer):
    """One request at one setting. An account that answers out of credits (401/402/403/429 while its balance
    API shows less than this setting's price) is logged as phase "nocredit", set aside, and the same request
    goes to the account with the most credits left; "nocredit" rows are never counted."""
    spec = PROVIDERS[provider]
    params = spec["ladder"][level]
    while True:
        with ACCT_LOCK:
            label = ACTIVE.get(provider)
        base = {"provider": provider, "account": label or "", "site": site, "url": url, "level": level,
                "params": json.dumps(params)}
        t = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + "Z"
        if label is None:
            return _record(writer, None, ts=ts, **base, phase="nocredit", http="NOKEY", site_status="", ok=0,
                           seconds=0, cost=0, bytes=0, request_id="", error="no account with credits left",
                           body="", headers={})
        key = dict(KEYS[provider])[label]
        try:
            http, site_status, body, cost, hdrs, rid = spec["call"](url, params, key)
            err = ""
        except Exception as e:                                # timeout / connection on the client side
            http, site_status, body, cost, hdrs, rid, err = "EXC", None, "", 0, {}, "", str(e)[:120]
        secs = round(time.time() - t, 2)
        ok = (len(body) >= SITES[site].get("min_bytes", 30_000) and bool(re.search(SITES[site]["check"], body))
              and not BLOCKED.search(body[:20000]))
        if not ok and http in NOCREDIT_HTTP and spec["price"][level] is not None:
            left = balance(provider, key)
            if left is not None and left < spec["price"][level]:
                _record(writer, key, ts=ts, **base, phase="nocredit", http=http, site_status=site_status, ok=0,
                        seconds=secs, cost=cost or 0, bytes=len(body), request_id=rid,
                        error="out of credits (%s left)" % left, body=body, headers=hdrs)
                with ACCT_LOCK:
                    SPENT.add((provider, label))
                pick_account(provider)
                continue
        if cost is None:                                     # no cost header: the documented price, only when it bills
            cost = spec["price"][level] if ok else 0
        return _record(writer, key, ts=ts, **base, phase=phase, http=http, site_status=site_status, ok=int(ok),
                       seconds=secs, cost=cost, bytes=len(body), request_id=rid,
                       error=err or ("" if ok else body[:160].replace("\n", " ")), body=body, headers=hdrs)


def run_provider_site(provider, site, urls, writer):
    """Find a rival's cheapest working setting on the first URL (probe), then fetch the rest at it; Crawio has
    one automatic setting, so it has nothing to find and every URL is a normal request.
    Every URL that fails gets ONE more try (phase "retry"), the way a customer's script would: a rival
    steps up to its next setting (and keeps the higher setting for the URLs still to come if that
    worked), Crawio simply asks again. The report counts first-try and within-two-tries success, and
    every credit either try took."""
    ladder = PROVIDERS[provider]["ladder"]
    pick_account(provider)
    probes = []
    if len(ladder) == 1:
        level, rest = 0, urls
    else:
        level = None
        for lv in range(len(ladder)):                        # cheapest setting that works on the first URL
            r = attempt(provider, site, urls[0], lv, "probe", writer)
            probes.append(r)
            if r["ok"]:
                level = lv
                break
        if level is None and len(urls) > 1:                  # one more chance at the strongest setting
            r = attempt(provider, site, urls[1], len(ladder) - 1, "probe", writer)
            probes.append(r)
            if r["ok"]:
                level = len(ladder) - 1
        if level is None:
            return {"provider": provider, "site": site, "level": None, "rows": [], "probes": probes}
        rest = urls[1:] if probes[-1]["url"] == urls[0] else urls[2:]
    state = {"level": level}
    lock = threading.Lock()

    def one(u):
        with lock:
            lv = state["level"]
        r = attempt(provider, site, u, lv, "run", writer)
        if r["ok"]:
            return [r]
        nxt = min(lv + 1, len(ladder) - 1)
        r2 = attempt(provider, site, u, nxt, "retry", writer)
        if r2["ok"] and nxt > lv:
            with lock:
                state["level"] = max(state["level"], nxt)
        return [r, r2]

    with cf.ThreadPoolExecutor(CONCURRENCY) as ex:
        rows = [x for pair in ex.map(one, rest) for x in pair]
    return {"provider": provider, "site": site, "level": level, "rows": probes[-1:] + rows, "probes": probes}


def summarize(results):
    """Console summary per site and provider (report.py is the table of record): URLs delivered within two
    tries / URLs asked, credits on the pages (the failed setting search apart)."""
    out = []
    for res in results:
        p, s, rows, probes = res["provider"], res["site"], res["rows"], res["probes"]
        urls = {r["url"] for r in probes + rows}
        done = {r["url"] for r in probes + rows if r["ok"]}
        credits = sum(float(r["cost"] or 0) for r in rows)
        out.append({"site": s, "provider": p,
                    "setting": json.dumps(PROVIDERS[p]["ladder"][res["level"]]) if res["level"] is not None else "none worked",
                    "delivered": "%d/%d" % (len(done), len(urls)),
                    "median_s": round(statistics.median(r["seconds"] for r in rows if r["ok"]), 1) if done else None,
                    "credits_per_page": round(credits / len(done), 2) if done else None,
                    "search_credits": sum(float(r["cost"] or 0) for r in probes if not r["ok"])})
    return out


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def smoke(url, providers, out):
    """One request per ACCOUNT (every key of every provider) at its cheapest setting, to check each key, its
    cost reading and the saved files, and that each dashboard shows it, before the real run."""
    os.makedirs(out, exist_ok=True)
    OUT["dir"] = out
    with open(os.path.join(out, "requests.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for p in providers:
            for label, _ in KEYS[p]:
                with ACCT_LOCK:
                    ACTIVE[p] = label
                r = attempt(p, "wikipedia", url, 0, "smoke", w)
                print("%-12s %s  http=%s ok=%s cost=%s bytes=%s %ss rid=%s" % (
                    p, label, r["http"], r["ok"], r["cost"], r["bytes"], r["seconds"], r["request_id"]), flush=True)


FIELDS = ["attempt", "ts", "provider", "account", "site", "url", "level", "params", "phase", "http", "site_status",
          "ok", "seconds", "cost", "bytes", "request_id", "error"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default=",".join(SITES))
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--per-site", type=int, default=10)
    ap.add_argument("--out", default="results/run")
    ap.add_argument("--set", default="a", choices=("a", "b"), help="URL set: a (first run) or b (fresh)")
    ap.add_argument("--dry", action="store_true", help="print the URLs and each provider's account; no page requests")
    ap.add_argument("--smoke", metavar="URL", help="one request per account to URL (a Wikipedia page), then stop")
    ap.add_argument("--meta", help="a JSON file merged into run.json (live versions, switches)")
    a = ap.parse_args()
    if a.set == "b":
        for s, urls in URLS_B.items():
            SITES[s]["urls"] = urls
    providers = [p for p in a.providers.split(",") if os.environ.get(PROVIDERS[p]["env"])]
    sites = a.sites.split(",")
    load_keys(providers)
    if a.smoke:
        smoke(a.smoke, providers, a.out)
        return
    before = balances(providers)
    if a.dry:
        for s in sites:
            urls = SITES[s]["urls"][:a.per_site]
            print("%-10s %2d URLs  %s ... %s" % (s, len(urls), urls[0], urls[-1]))
        for p in providers:
            print("%-12s balances %s -> starts on %s" % (p, before[p], pick_account(p)))
        return
    os.makedirs(a.out, exist_ok=True)
    OUT["dir"] = a.out
    here = os.path.dirname(os.path.abspath(__file__))
    meta = {"started_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "url_set": a.set,
            "per_site": a.per_site, "sites": sites, "providers": providers, "concurrency": CONCURRENCY,
            "timeout_s": TIMEOUT_S, "blocked_pattern": BLOCKED.pattern,
            "checks": {s: {"check": SITES[s]["check"], "min_bytes": SITES[s].get("min_bytes", 30_000)} for s in sites},
            "ladders": {p: PROVIDERS[p]["ladder"] for p in providers},
            "prices": {p: PROVIDERS[p]["price"] for p in providers}, "plans": PLANS, "zyte_tiers": ZYTE_TIERS,
            "accounts": {p: [label for label, _ in KEYS[p]] for p in providers}, "balances_before": before,
            "harness_sha256": {f: _sha256(os.path.join(here, f)) for f in ("bench.py", "report.py")}}
    if a.meta:
        with open(a.meta, encoding="utf-8") as fh:
            meta.update(json.load(fh))
    with open(os.path.join(a.out, "urls.json"), "w", encoding="utf-8") as fh:
        json.dump({s: SITES[s]["urls"][:a.per_site] for s in sites}, fh, indent=1)
    with open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    results = []
    with open(os.path.join(a.out, "requests.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for site in sites:
            urls = SITES[site]["urls"][:a.per_site]
            print("%s site %s: %s" % (time.strftime("%H:%M:%S"), site, ", ".join(providers)), flush=True)
            with cf.ThreadPoolExecutor(len(providers)) as ex:        # every provider on this site at the same time
                futs = [ex.submit(run_provider_site, p, site, urls, w) for p in providers]
                for f in futs:
                    results.append(f.result())
            fh.flush()
    meta["finished_utc"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    meta["balances_after"] = balances(providers)
    meta["accounts_found_spent"] = sorted("%s %s" % x for x in SPENT)
    with open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    summary = summarize(results)
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"run_at": meta["started_utc"], "concurrency": CONCURRENCY, "per_site": a.per_site,
                   "plans": PLANS, "rows": summary}, fh, indent=1)
    for r in summary:
        print("%-12s %-12s %-6s %6ss %7s cr/page  search %s cr  %s" % (
            r["site"], r["provider"], r["delivered"], r["median_s"], r["credits_per_page"], r["search_credits"],
            r["setting"]), flush=True)


if __name__ == "__main__":
    main()
