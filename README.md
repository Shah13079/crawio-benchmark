# Crawio scraping API benchmark

The test behind [crawio.com/benchmark](https://crawio.com/benchmark/): the same 110 fresh pages from 11
well-known sites, sent to six scraping APIs at the same time. Add your own keys and rerun it.

## Results (September 29, 2026, 06:32 to 06:59 UTC)

| API | Delivered (within 2 tries) | First try | $ per 1,000 regular pages | $ per 1,000 protected pages | Median time |
|---|---|---|---|---|---|
| Crawio | 110 / 110 (100%) | 109 | $0.10 | $0.58 | 8.1 s |
| Scrape.do | 110 / 110 (100%) | 109 | $0.13 | $1.26 | 4.4 s |
| ScrapingBee | 96 / 110 (87.3%) | 88 | $0.20 | $6.41 (56 of 70) | 11.2 s |
| ScraperAPI | 80 / 110 (72.7%) | 80 | $2.08 | $3.80 (40 of 70) | 4.4 s |
| Zyte API | 80 / 110 (72.7%) | 78 | $0.26 | $1.01 (40 of 70) | 15.2 s |
| ZenRows | 50 / 110 (45.5%) | 46 | $1.29 (30 of 40) | $6.27 (20 of 70) | 9.6 s |

Regular pages are the 4 sites Crawio bills at 1 credit: Amazon, Booking.com, Wikipedia and Zillow (40 pages).
Protected pages are the other 7 sites, behind heavy bot protection, at 6 credits (70 pages). On Crawio's plan of
$29 for 300,000 credits that is $0.10 and $0.58 per 1,000 pages. Where an API delivered fewer pages, its price
covers only those (count in brackets). There is no single average price: it would depend on each API's mix of
regular and protected pages.

Pages delivered out of 10, per site:

| Site | Crawio | Scrape.do | ScrapingBee | ScraperAPI | Zyte API | ZenRows |
|---|---|---|---|---|---|---|
| Idealista | 10 | 10 | 6 | 0 | 0 | 0 |
| G2 | 10 | 10 | 8 | 0 | 0 | 0 |
| Realtor.com | 10 | 10 | 10 | 0 | 0 | 0 |
| Glassdoor | 10 | 10 | 10 | 10 | 10 | 0 |
| Home Depot | 10 | 10 | 10 | 10 | 10 | 0 |
| Zillow | 10 | 10 | 10 | 10 | 10 | 0 |
| Indeed | 10 | 10 | 2 | 10 | 10 | 10 |
| Amazon | 10 | 10 | 10 | 10 | 10 | 10 |
| Booking.com | 10 | 10 | 10 | 10 | 10 | 10 |
| eBay | 10 | 10 | 10 | 10 | 10 | 10 |
| Wikipedia | 10 | 10 | 10 | 10 | 10 | 10 |

Cost is what each account was charged (checked against every account's balance and billing dashboard), per
1,000 delivered pages, priced on each API's entry plan: Crawio $29 for 300,000 credits, Scrape.do $29 for 250,000, ScrapingBee $49
for 250,000, ScraperAPI $49 for 100,000, ZenRows $57 for 250,000, Zyte API pay as you go. Credits a competitor
spent finding its setting are in the log, not in its price.

### Limits of this test

- **Trial accounts.** Competitors ran on free-trial or pay-as-you-go accounts. ScraperAPI's trial blocks its
  strongest setting ("ultra premium"), which paid plans include; its 0 of 10 on G2, Idealista and Realtor.com
  may differ on a paid plan.
- **ZenRows connections.** ZenRows' API closed the connection without an answer on some sites. A ZenRows-only
  rerun 10 minutes later got 10 of 10 on Zillow and 9 of 10 on Realtor.com. The tables show the main run.
- **ScrapingBee on Indeed.** Its cheapest setting worked on the first page, then failed. Its premium setting got
  10 of 10 in an earlier run the same day.
- **Small sample.** 10 pages per site.
- **Speed.** Crawio is slower overall: a median of 8.1 s against Scrape.do's 4.4 s. On regular pages Crawio was
  faster, 3.6 s against 4.3 s. On protected pages it was slower, 17.9 s against 4.5 s; its first 5 pages on each
  protected site took 22.0 s, the next 5 took 10.9 s.

## How it works

1. 11 sites, 10 fresh pages each (`--set b` in `bench.py`). No one had requested them before the test.
2. Every API gets the same site at the same time, 5 requests at once per API.
3. A page counts only if it holds the site's real content (a per-site marker), is at least 30 KB and is not a
   block or captcha page (`BLOCKED`).
4. Each competitor first finds its cheapest setting that works on the site (its `ladder`), on the site's first
   URL. Crawio has no settings to pick.
5. A failed page gets one retry: a competitor steps up one setting, Crawio asks again.
6. Credits come from each API's own cost header (Zyte: its dashboard's per-request prices).

## Run it yourself

```bash
pip install -r requirements.txt
# set only the APIs you want to test; several accounts of one API = comma-separated keys
export CRAWIO_BENCH_KEY=...   SCRAPEDO_KEY=...   SCRAPINGBEE_KEY=...
export SCRAPERAPI_KEY=...     ZENROWS_KEY=...    ZYTE_KEY=...

python bench.py --set b --dry                              # the URLs and each API's account, no page requests
python bench.py --set b --per-site 10 --out results/run    # the run
python report.py results/run                               # tables, cost by page type, per-account credit check
```

Each run folder gets `run.json` (times, settings, balances before and after), `urls.json`, `requests.csv`
(one row per attempt) and `bodies/` (every page and reply header, keys scrubbed). For Zyte, put the dollars
its dashboard shows per site in `results/run/zyte_usd.json` (`{"amazon": 0.0023, ...}`).

## The published log

`results/crawio-benchmark-2026-09-29.csv` is the September 29 run: every attempt with time, API, site, URL,
attempt type (`setting search`, `first try`, `retry`), setting, status codes, delivered, seconds, credits and
cost in dollars on the entry plan. Account labels, request ids and page text are left out.

## License

MIT
