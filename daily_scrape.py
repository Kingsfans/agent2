"""Run one day's discovery + scraping target for the Marinexis med spa campaign.

Finds new candidate med spa sites (medspa/discover.py), crawls -- scrapes -- at
least DAILY_SCRAPE_TARGET of them for contact info and peptide keywords
(medspa/enrich.py), promotes the qualified ones into the send queue
(medspa/build_queue.py), and refreshes the dashboard.

This is the discovery half of the pipeline only. It does NOT send any email and
does NOT touch DAILY_CAP / HOURLY_CAP -- outreach volume stays governed by
outreach/serve_send.py regardless of how much gets scraped here. Scraping more
candidates does not mean emailing more people faster; it means a deeper, more
qualified queue for the send engine to work through at its own safe pace.

    python3 daily_scrape.py                  # target from .env (default 500)
    python3 daily_scrape.py --target 750
    python3 daily_scrape.py --dry-run         # discover + enrich, skip queue --apply

Needs one search API key in .env (SERPER_API_KEY, BRAVE_SEARCH_API_KEY, or
GOOGLE_CSE_API_KEY + GOOGLE_CSE_CX) to hit the target unattended (e.g. from a
scheduled GitHub Action). Without one, use Claude's own web search interactively
via `medspa/discover.py plan` + `add`, the way the /find-medspas skill does it --
this script will still enrich and promote whatever is already in
medspa/candidates.csv, it just won't be able to add fresh candidates by itself.

Logs one row per run to outreach/scrape_log.csv, which the dashboard reads to
show scraping volume against the daily target.
"""
import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "medspa"))
sys.path.insert(0, str(ROOT / "outreach"))
import discover  # noqa: E402
import enrich  # noqa: E402
import build_queue  # noqa: E402
import dash_update  # noqa: E402

LOG = ROOT / "outreach" / "scrape_log.csv"
LOG_COLS = ["date", "started_at", "finished_at", "target", "queries_run",
            "candidates_before", "candidates_after", "new_candidates",
            "sites_enriched", "queue_before", "queue_after", "qualified_added",
            "met_target"]

DEFAULT_TARGET = int(discover.env("DAILY_SCRAPE_TARGET", "500"))
MAX_QUERIES_PER_DAY = int(discover.env("MAX_QUERIES_PER_DAY", "150"))
QUERIES_PER_BATCH = 25


def log_run(row):
    is_new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if is_new:
            w.writeheader()
        w.writerow(row)


def discover_until(target, max_queries):
    """Keep running search queries -- cycling through every city x peptide-keyword
    combination -- until there's at least `target` unenriched candidates queued up,
    or the day's query ceiling is hit (so an API bill can't run away)."""
    queries_run = 0
    if not discover.search_api("test"):
        print("no search API key configured -- skipping automated discovery. "
              "Use `medspa/discover.py plan` + Claude web search + `add` for new "
              "candidates, then re-run this script to enrich and promote them.")
        return 0

    enriched_domains = set(enrich.load(enrich.ENR))
    while queries_run < max_queries:
        all_candidates = discover.load_candidates()
        backlog = len(set(all_candidates) - enriched_domains)
        if backlog >= target:
            break
        items, _ = discover.plan(min(QUERIES_PER_BATCH, max_queries - queries_run))
        if not items:
            break
        last = items[0][0]
        for idx, q, city, state in items:
            try:
                discover.cmd_search(q, city, state)
                last = idx
                queries_run += 1
            except Exception as e:
                print(f"! query failed ({type(e).__name__}: {e}) -- stopping this batch")
                break
        discover.advance(last + 1)
    return queries_run


def main(target, dry_run, max_queries):
    started = datetime.now(timezone.utc)
    date = started.strftime("%Y-%m-%d")
    cands_before = len(discover.load_candidates())
    queue_before = len(build_queue.load_csv(build_queue.QUEUE))

    print(f"=== daily scrape {date}: target={target} sites, query ceiling={max_queries} ===")
    print("--- step 1: discover new candidates ---")
    queries_run = discover_until(target, max_queries)
    cands_after = len(discover.load_candidates())
    print(f"candidates: {cands_before} -> {cands_after} ({queries_run} queries run)")

    print("--- step 2: enrich (crawl) candidates ---")
    sites_enriched = enrich.main(limit=target, recheck=False)

    print("--- step 3: promote qualified med spas into the send queue ---")
    qualified_added = build_queue.main(apply=not dry_run)
    queue_after = queue_before + qualified_added if not dry_run else queue_before

    print("--- step 4: refresh dashboard ---")
    dash_update.main()

    finished = datetime.now(timezone.utc)
    met_target = sites_enriched >= target
    row = {
        "date": date,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": target,
        "queries_run": queries_run,
        "candidates_before": cands_before,
        "candidates_after": cands_after,
        "new_candidates": cands_after - cands_before,
        "sites_enriched": sites_enriched,
        "queue_before": queue_before,
        "queue_after": queue_after,
        "qualified_added": qualified_added,
        "met_target": "yes" if met_target else "no",
    }
    log_run(row)

    print(f"\n=== done: scraped {sites_enriched}/{target} sites, "
          f"{'MET' if met_target else 'missed'} target, "
          f"+{qualified_added} added to queue ===")
    if not met_target:
        print("Below target usually means: the candidate backlog ran out (add a search "
              "API key, or more cities/keywords) or the query ceiling was hit before "
              "enough new candidates were found -- raise MAX_QUERIES_PER_DAY in .env.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", type=int, default=DEFAULT_TARGET,
                   help=f"minimum sites to scrape today (default {DEFAULT_TARGET}, from .env DAILY_SCRAPE_TARGET)")
    p.add_argument("--max-queries", type=int, default=MAX_QUERIES_PER_DAY,
                   help=f"search-API query ceiling for the day (default {MAX_QUERIES_PER_DAY})")
    p.add_argument("--dry-run", action="store_true", help="discover + enrich, but don't add to the send queue")
    args = p.parse_args()
    main(args.target, args.dry_run, args.max_queries)
