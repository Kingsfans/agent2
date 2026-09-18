"""The whole campaign in one command: sync the inbox, scrape, send, publish.

This is the loop the business actually runs on. Every step skips work that is
already done, so it is safe on a schedule and safe to run twice by accident.

    python3 run_campaign.py                     # dry run: reports, changes nothing
    python3 run_campaign.py --drafts            # scrape + write Gmail drafts
    python3 run_campaign.py --send              # scrape + send for real
    python3 run_campaign.py --send --no-scrape  # outreach only, skip discovery
    python3 run_campaign.py --send --commit     # ...and commit the state files

Order matters and is not arbitrary:

  1. inbox sync   -- anyone who replied, bounced or opted out drops out of the
                     cadence BEFORE the wave is built, so they can't be mailed
                     again in the same run that their reply arrived.
  2. discovery    -- top the queue up (only once per day; a second run the same
                     day skips it, since the daily target is a daily target).
  3. outreach     -- one wave, inside DAILY_CAP / HOURLY_CAP.
  4. dashboard    -- refresh exports/lead_pipeline_tracker.html.
  5. commit       -- optional; the CSVs are the campaign's only memory, and
                     uncommitted memory is how a practice gets emailed twice.

Schedule it hourly during the workday:

    0 9-17 * * 1-5  cd /path/to/repo && python3 run_campaign.py --send --commit \\
                    >> campaign.log 2>&1
"""
import argparse
import csv
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _sub in ("", "outreach", "emailer", "medspa"):
    sys.path.insert(0, str(ROOT / _sub) if _sub else str(ROOT))
import serve_send as ss  # noqa: E402
import dash_update  # noqa: E402

SCRAPE_LOG = ROOT / "outreach" / "scrape_log.csv"
STATE_FILES = ["medspa/candidates.csv", "medspa/enriched.csv", "medspa/.query_progress",
               "outreach/medspa_queue.csv", "outreach/sent_log.csv", "outreach/do_not_contact.csv",
               "outreach/scrape_log.csv", "outreach/bot_log.csv", "outreach/contacted.csv",
               "outreach/replied.csv", "outreach/bounced.csv", "outreach/not_yet_emailed.csv",
               "exports/lead_pipeline_tracker.html"]


def scraped_today():
    """Discovery is a daily target, so a second run the same day shouldn't redo it."""
    if not SCRAPE_LOG.exists():
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(SCRAPE_LOG, newline="", encoding="utf-8") as f:
        return any(r.get("date") == today for r in csv.DictReader(f))


def step(title):
    print(f"\n{'=' * 64}\n== {title}\n{'=' * 64}")


def main(mode, do_scrape, do_commit, wave_size, scrape_target):
    live = mode in ("drafted", "sent")

    step("1/5  inbox sync -- replies, bounces, opt-outs")
    if live:
        import inbox_sync
        try:
            inbox_sync.main(days=7, apply=True)
        except SystemExit as e:
            print(f"inbox sync skipped: {e}")
    else:
        print("(dry run -- skipped; needs mailbox credentials)")

    step("2/5  discovery -- top up the queue")
    if not live:
        # Discovery crawls real sites and writes enriched.csv, so it is not a
        # read-only step -- a dry run has to skip it outright to stay dry.
        print("(dry run -- skipped; discovery writes candidate and enrichment data)")
    elif not do_scrape:
        print("skipped (--no-scrape)")
    elif scraped_today():
        print("already scraped today -- skipping (the daily target is a daily target)")
    else:
        import daily_scrape
        daily_scrape.main(target=scrape_target, dry_run=False,
                          max_queries=daily_scrape.MAX_QUERIES_PER_DAY)

    step("3/5  outreach wave")
    import send_bot
    send_bot.main(mode, wave_size, send_bot.SEND_DELAY)

    step("4/5  dashboard")
    dash_update.main()

    step("5/5  commit")
    if not do_commit:
        print("skipped (--commit to write state back to git)")
    else:
        commit_state()

    step("campaign status")
    ss.cmd_stats()


def commit_state():
    existing = [f for f in STATE_FILES if (ROOT / f).exists()]
    subprocess.run(["git", "add", *existing], cwd=ROOT, check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if staged.returncode == 0:
        print("no state changes to commit")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subprocess.run(["git", "commit", "-m", f"Campaign run {stamp}"], cwd=ROOT, check=False)
    pushed = subprocess.run(["git", "push"], cwd=ROOT)
    print("pushed" if pushed.returncode == 0 else "commit made, push failed -- push by hand")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--drafts", action="store_true", help="write Gmail drafts instead of sending")
    g.add_argument("--send", action="store_true", help="send the wave for real")
    p.add_argument("--no-scrape", action="store_true", help="skip discovery, outreach only")
    p.add_argument("--commit", action="store_true", help="commit and push the state files afterwards")
    p.add_argument("--wave", type=int, default=ss.HOURLY_CAP, help="messages this wave (default HOURLY_CAP)")
    p.add_argument("--scrape-target", type=int, default=None, help="override the daily scrape target")
    args = p.parse_args()

    import daily_scrape as _ds
    main("drafted" if args.drafts else "sent" if args.send else "dry",
         not args.no_scrape, args.commit, args.wave,
         args.scrape_target or _ds.DEFAULT_TARGET)
