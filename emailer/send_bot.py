"""The outreach bot: build a wave, put it in the mailbox, record every one.

This is the autonomous version of the /outreach-wave skill. It does what a
person clicking through Gmail would do, in the same order, with the same rules:

    python3 emailer/send_bot.py                 # dry run -- prints the wave, sends nothing
    python3 emailer/send_bot.py --drafts        # write real Gmail drafts for review
    python3 emailer/send_bot.py --send          # actually send
    python3 emailer/send_bot.py --send --limit 5

Every message is recorded in outreach/sent_log.csv the instant it leaves,
one at a time -- not batched at the end. If the process dies mid-wave, the
rows already written are exactly the messages that already went out, so the
next run picks up without re-contacting anyone.

What it will not do, by design:
  - contact anyone in outreach/do_not_contact.csv (the engine filters them out)
  - exceed DAILY_CAP or HOURLY_CAP
  - follow up with anyone who replied, bounced or unsubscribed
  - send a message whose body still has an unfilled {placeholder} in it
  - strip the CAN-SPAM footer, which is rendered from the templates

Stops the whole wave on the first quota/rate/block error from the provider,
because retrying into a throttle is how a sending domain gets burned.
"""
import argparse
import csv
import smtplib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "outreach"))
sys.path.insert(0, str(ROOT / "emailer"))
import serve_send as ss  # noqa: E402
import mailbox as mbx  # noqa: E402

BOT_LOG = ROOT / "outreach" / "bot_log.csv"
BOT_LOG_COLS = ["run_at", "mode", "attempted", "delivered", "failed", "initial", "followups", "stopped_reason"]

# Provider responses that mean "stop now", not "try the next one".
FATAL = ("quota", "rate", "limit", "blocked", "suspended", "too many", "try again later",
         "4.7.0", "5.4.5", "421", "454", "550 5.7")
# Seconds between messages, so a wave doesn't leave as one burst.
SEND_DELAY = float(ss.cfg("SEND_DELAY_SECONDS", "6"))


def matches(row, where):
    """Geography filter for a queue row: --where NV or --where "las vegas".

    A two-letter term is treated as a state code and matched exactly against the
    state column only. Substring-matching it against the city would quietly pull
    in DeNVer and JacksoNVille -- a filter that looks like it works while mailing
    the wrong market is worse than no filter."""
    if not where:
        return True
    where = where.strip().lower()
    state = (row.get("state") or "").strip().lower()
    if len(where) == 2:
        return where == state
    return where in (row.get("city") or "").lower() or where == state


def build_wave(n, where=""):
    """The next n messages to go out: follow-ups that are due first, then new
    prospects. Mirrors serve_send.cmd_next, but in-process so nothing has to
    round-trip through a JSON file.

    `where` narrows new prospects to one city or state -- useful for working a
    metro at a time. Follow-ups are never filtered: someone already contacted is
    owed the rest of their sequence regardless of which market you're working."""
    rows = ss.load_sent()
    sd = ss.sender()
    queue_index = {r["email"].strip().lower(): r for r in ss.load_queue()}
    budget = max(0, ss.DAILY_CAP - ss.today_count(rows))
    n = min(n, budget, ss.HOURLY_CAP)
    items = []

    fu_cap = int(n * ss.FU_SHARE + 0.999) if n else 0
    for r in ss.due_followups(rows):
        if len(items) >= fu_cap:
            break
        q = queue_index.get(r["email"], {})
        name = ss.display_name(q.get("business_name", ""), r["domain"])
        peps = (q.get("peptides") or "peptides").strip()
        stage = int(r["stage"]) + 1
        subject, body = ss.render_followup(name, peps, stage, sd)
        items.append({"kind": "fu", "stage": stage, "to": r["email"], "name": name,
                      "city": q.get("city", ""), "state": q.get("state", ""),
                      "subject": subject, "body": body})

    for r in ss.initial_candidates(rows):
        if len(items) >= n:
            break
        if not matches(r, where):
            continue
        addr = r["email"].strip().lower()
        name = ss.display_name(r.get("business_name", ""), addr.split("@", 1)[1])
        peps = (r.get("peptides") or "peptides").strip()
        subject, body = ss.render_initial(name, peps, sd)
        items.append({"kind": "initial", "stage": 0, "to": addr, "name": name,
                      "city": r.get("city", ""), "state": r.get("state", ""),
                      "subject": subject, "body": body})

    return items, budget


def validate(items):
    """Refuse to send anything with an unfilled placeholder. A '{business_name}'
    that reaches a real inbox is worse than a message that never goes."""
    bad = [i["to"] for i in items if "{" in i["subject"] or "{" in i["body"]]
    if bad:
        sys.exit(f"ERROR: unfilled placeholders in messages to {bad} -- "
                 f"check emailer/message_medspa.txt and emailer/followup_medspa.txt")


def record_one(item, mode):
    """Write this one message to sent_log.csv immediately.

    Deliberately re-reads and re-writes the whole file per message: the file is
    small, and the alternative -- holding state in memory until the wave ends --
    is what turns a crash into a duplicate send."""
    rows = ss.load_sent()
    index = {r["email"]: r for r in rows}
    stamp = ss.iso(ss.now())
    addr = item["to"].strip().lower()

    if item["kind"] == "initial":
        if addr in index:
            return False
        rows.append({"email": addr, "domain": addr.split("@", 1)[1], "audience": "medspa",
                     "mode": mode, "via": ss.sender()["SENDER_EMAIL"], "sent_at": stamp,
                     "last_touch_at": stamp, "stage": "0", "status": "active"})
    else:
        row = index.get(addr)
        if not row or int(row["stage"]) >= int(item["stage"]):
            return False
        row["stage"] = str(item["stage"])
        row["last_touch_at"] = stamp
        row["mode"] = mode
    ss.save_sent(rows)
    return True


def log_run(mode, attempted, delivered, failed, initial, followups, stopped):
    is_new = not BOT_LOG.exists()
    with open(BOT_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BOT_LOG_COLS)
        if is_new:
            w.writeheader()
        w.writerow({"run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "mode": mode, "attempted": attempted, "delivered": delivered, "failed": failed,
                    "initial": initial, "followups": followups, "stopped_reason": stopped})


def is_fatal(err):
    text = str(err).lower()
    return any(marker in text for marker in FATAL)


def main(mode, count, delay, where=""):
    items, budget = build_wave(count, where)
    validate(items)

    n_fu = sum(1 for i in items if i["kind"] == "fu")
    scope = f", where={where!r}" if where else ""
    print(f"WAVE {len(items)}  followups={n_fu} new={len(items) - n_fu}  "
          f"(budget_left_today={budget}, daily_cap={ss.DAILY_CAP}, hourly_cap={ss.HOURLY_CAP}{scope})")
    for i, it in enumerate(items, 1):
        kind = f"FU{it['stage']}" if it["kind"] == "fu" else "INIT"
        where = f"{it['city']}, {it['state']}".strip(", ") or "-"
        print(f"  {i:>2}. {kind:<5} {it['to']:<38} {it['name'][:30]:<30} {where}")

    if not items:
        print("\nNothing to send. Queue is empty for today, or the daily cap is spent.")
        return
    if mode == "dry":
        print("\n(dry run -- nothing was drafted or sent. Use --drafts or --send.)")
        return

    delivered = failed = n_init = n_fup = 0
    stopped = ""
    verb = "drafted" if mode == "drafted" else "sent"
    print(f"\n{verb} so far:")

    with mbx.Mailbox() as box:
        for i, it in enumerate(items, 1):
            try:
                if mode == "drafted":
                    box.create_draft(it["to"], it["subject"], it["body"])
                else:
                    box.send(it["to"], it["subject"], it["body"])
            except (smtplib.SMTPException, OSError, RuntimeError) as e:
                failed += 1
                fatal = is_fatal(e)
                print(f"  {i:>2}. FAILED {it['to']}: {type(e).__name__}: {e}")
                if fatal:
                    stopped = f"{type(e).__name__}: {e}"
                    print("\n!! provider returned a quota/block error -- stopping the wave now.")
                    print("   Lower DAILY_CAP in .env and resume tomorrow. Do not retry into a block.")
                    break
                continue

            # Record before anything else can go wrong.
            record_one(it, mode)
            delivered += 1
            if it["kind"] == "initial":
                n_init += 1
            else:
                n_fup += 1
            print(f"  {i:>2}. {verb} {it['to']}")

            if delay and i < len(items):
                time.sleep(delay)

    log_run(mode, len(items), delivered, failed, n_init, n_fup, stopped)
    print(f"\n=== {verb} {delivered}/{len(items)}  (new={n_init} followups={n_fup} failed={failed}) ===")
    if mode == "drafted":
        print("Drafts are in your Gmail Drafts folder. They are recorded as contacted, so the")
        print("bot will not queue them again -- review and send them from Gmail.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--drafts", action="store_true", help="write real Gmail drafts instead of sending")
    g.add_argument("--send", action="store_true", help="actually send the wave")
    p.add_argument("--limit", type=int, default=ss.HOURLY_CAP, help="cap this wave (default HOURLY_CAP)")
    p.add_argument("--delay", type=float, default=SEND_DELAY, help="seconds between messages")
    p.add_argument("--where", default="", metavar="CITY|ST",
                   help='only new prospects in this city or state, e.g. --where "Las Vegas" or --where NV')
    args = p.parse_args()

    main("drafted" if args.drafts else "sent" if args.send else "dry",
         args.limit, args.delay, args.where)
