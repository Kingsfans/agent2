"""Read the inbox and update the campaign from it -- replies, bounces, opt-outs.

Until now this was the one step that needed a person: somebody had to read the
mailbox and run `serve_send.py mark`. This does it on its own.

    python3 emailer/inbox_sync.py              # dry run: what it would mark
    python3 emailer/inbox_sync.py --apply      # actually mark them
    python3 emailer/inbox_sync.py --apply --days 14

What each signal does:
  reply         -> status=replied. Follow-ups stop. This is a lead; go read it.
  bounce        -> status=bounced, address added to do_not_contact.csv forever.
  unsubscribe   -> status=unsubscribed, address added to do_not_contact.csv forever.

Run this BEFORE every wave. Someone who answered an hour ago should not get a
follow-up an hour later, and an address that bounced should never be tried twice.

Matching note: a reply often arrives from a different address than the one that
was emailed -- front desks forward, practices reply from a personal mailbox. So
an address is matched first exactly, then by domain against the address that was
actually contacted. A reply from a domain nobody was emailed at is ignored.
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "outreach"))
sys.path.insert(0, str(ROOT / "emailer"))
import serve_send as ss  # noqa: E402
import mailbox as mbx  # noqa: E402

DNC = ROOT / "outreach" / "do_not_contact.csv"


def resolve(addresses, sent_rows):
    """Map inbound addresses onto the rows we actually emailed.

    Exact address wins; otherwise the domain, as long as that domain maps to
    exactly one contacted address (two would be a guess, and guessing here
    marks the wrong practice as replied)."""
    by_email = {r["email"]: r for r in sent_rows}
    by_domain = {}
    for r in sent_rows:
        by_domain.setdefault(r["domain"], []).append(r)

    hits = []
    for addr in addresses:
        addr = addr.lower().strip()
        if addr in by_email:
            hits.append((addr, by_email[addr], "exact"))
            continue
        domain = addr.split("@", 1)[-1]
        rows = by_domain.get(domain, [])
        if len(rows) == 1:
            hits.append((addr, rows[0], "domain"))
    return hits


def add_to_dnc(entries, reason):
    """Append to the do-not-contact list, skipping anything already on it."""
    existing = ss.load_dnc()
    new = [e for e in entries if e not in existing]
    if not new:
        return 0
    is_new_file = not DNC.exists()
    with open(DNC, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "domain", "reason", "status"])
        if is_new_file:
            w.writeheader()
        for e in new:
            w.writerow({"email": e if "@" in e else "", "domain": e.split("@")[-1],
                        "reason": reason, "status": reason})
    return len(new)


def main(days, apply):
    sent_rows = ss.load_sent()
    if not sent_rows:
        print("nothing emailed yet -- nothing to reconcile.")
        return

    print(f"scanning the last {days} days of inbox mail...")
    with mbx.Mailbox() as box:
        replies, bounces, unsubs = box.scan_inbox(days=days)
    print(f"found: {len(replies)} replies, {len(bounces)} bounces, {len(unsubs)} opt-outs\n")

    # An opt-out outranks a plain reply: both stop the cadence, only one suppresses.
    replies -= unsubs

    changes = {"replied": [], "bounced": [], "unsubscribed": []}
    for status, addresses in (("replied", replies), ("bounced", bounces), ("unsubscribed", unsubs)):
        for addr, row, how in resolve(addresses, sent_rows):
            if row["status"] == status:
                continue
            # A reply never overrides a hard signal already on the record.
            if status == "replied" and row["status"] in ("bounced", "unsubscribed"):
                continue
            changes[status].append((addr, row, how))

    total = sum(len(v) for v in changes.values())
    for status, items in changes.items():
        for addr, row, how in items:
            via = "" if how == "exact" else f"  (matched {row['email']} by domain)"
            print(f"  {status:<13} {addr}{via}")
    if not total:
        print("nothing new to mark -- the campaign record already matches the inbox.")
        return

    if not apply:
        print(f"\n(dry run -- {total} changes. Re-run with --apply to write them.)")
        return

    for status, items in changes.items():
        for _, row, _ in items:
            row["status"] = status
    ss.save_sent(sent_rows)

    n_dnc = 0
    for status in ("bounced", "unsubscribed"):
        entries = {row["email"] for _, row, _ in changes[status]}
        n_dnc += add_to_dnc(entries, status)

    print(f"\nmarked {total} ({len(changes['replied'])} replied, {len(changes['bounced'])} bounced, "
          f"{len(changes['unsubscribed'])} unsubscribed); {n_dnc} added to do_not_contact.csv")
    if changes["replied"]:
        print("\nReplies are leads -- go read them in the mailbox.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="how far back to scan (default 7)")
    p.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    args = p.parse_args()
    main(args.days, args.apply)
