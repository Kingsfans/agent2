"""Rebuild exports/lead_pipeline_tracker.html -- the full-funnel campaign dashboard.

Standard library only. Reads every tracking CSV in the repo (candidates,
enriched, queue, sent log, do-not-contact, scrape log) and writes one
self-contained HTML file with no external assets, so it opens anywhere and can
be shared as-is.

    python3 outreach/dash_update.py
"""
import csv
import html
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "outreach"))
sys.path.insert(0, str(ROOT / "medspa"))
import serve_send as ss  # noqa: E402
import discover  # noqa: E402
import enrich  # noqa: E402

OUT = ROOT / "exports" / "lead_pipeline_tracker.html"
SCRAPE_LOG = ROOT / "outreach" / "scrape_log.csv"


def read(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def esc(s):
    return html.escape(str(s))


def bar_col(items, get_n, get_label, max_n=None, color_var="--series-1"):
    """A single-series horizontal magnitude bar list -- one color, length encodes
    the number, label carries identity (this is a within-series magnitude
    comparison, not an identity comparison, so one hue for every row is correct)."""
    max_n = max_n or max((get_n(x) for x in items), default=1) or 1
    out = []
    for it in items:
        n = get_n(it)
        pct = round(n / max_n * 100) if max_n else 0
        out.append(
            f'<div class="hbar-row"><div class="hbar-label">{esc(get_label(it))}</div>'
            f'<div class="hbar-track"><div class="hbar-fill" style="width:{pct}%;background:var({color_var})">'
            f'</div></div><div class="hbar-n">{n}</div></div>'
        )
    return "".join(out)


def main():
    # ---- source data ---------------------------------------------------
    candidates = read(ROOT / "medspa" / "candidates.csv")
    enriched = read(ROOT / "medspa" / "enriched.csv")
    queue = read(ROOT / "outreach" / "medspa_queue.csv")
    dnc = read(ROOT / "outreach" / "do_not_contact.csv")
    scrape_runs = read(SCRAPE_LOG)
    sent_rows = ss.load_sent()

    # ---- funnel ----------------------------------------------------------
    n_candidates = len(candidates)
    n_enriched = len(enriched)
    n_ok = sum(1 for r in enriched if r.get("status") == "ok")
    n_queue_now = len(queue)
    st = Counter(r["status"] for r in sent_rows)
    n_emailed = len(sent_rows)
    n_replied = st.get("replied", 0)
    n_bounced = st.get("bounced", 0)
    n_unsub = st.get("unsubscribed", 0)
    n_queue_pending = len(ss.initial_candidates(sent_rows))
    n_dnc = len({(r.get("email") or "").strip().lower() or (r.get("domain") or "").strip().lower() for r in dnc if r})
    today = ss.today_count(sent_rows)
    reply_rate = (n_replied / n_emailed * 100) if n_emailed else 0.0
    bounce_rate = (n_bounced / n_emailed * 100) if n_emailed else 0.0

    funnel = [
        ("Candidates found", n_candidates),
        ("Sites scraped", n_enriched),
        ("Qualified (in queue or emailed)", n_ok),
        ("Emailed", n_emailed),
        ("Replied", n_replied),
    ]

    # ---- daily scrape volume vs target ------------------------------------
    scrape_days = scrape_runs[-14:]
    target_today = int(scrape_days[-1]["target"]) if scrape_days else int(discover.env("DAILY_SCRAPE_TARGET", "500"))
    scrape_peak = max([target_today] + [int(r.get("sites_enriched") or 0) for r in scrape_days], default=1) or 1

    def scrape_bars():
        if not scrape_days:
            return '<p class="muted">No daily_scrape.py runs logged yet -- run <code>python3 daily_scrape.py</code>.</p>'
        out = []
        for r in scrape_days:
            n = int(r.get("sites_enriched") or 0)
            met = (r.get("met_target") or "") == "yes"
            pct = round(n / scrape_peak * 100)
            target_pct = round(int(r.get("target") or target_today) / scrape_peak * 100)
            color = "--status-good" if met else "--status-warning"
            out.append(
                f'<div class="bar"><div class="bar-track" style="height:100%">'
                f'<div class="bar-target" style="bottom:{target_pct}%"></div>'
                f'<div class="bar-fill" style="height:{pct}%;background:var({color})" '
                f'title="{n} scraped, target {r.get("target")}"></div></div>'
                f'<div class="bar-n">{n}</div><div class="bar-d">{esc(r["date"][5:])}</div></div>'
            )
        return "".join(out)

    scrape_total_30 = sum(int(r.get("sites_enriched") or 0) for r in scrape_runs[-30:])
    scrape_hit_days = sum(1 for r in scrape_runs if (r.get("met_target") or "") == "yes")
    scrape_run_days = len(scrape_runs)

    # ---- sends per day (existing chart, kept) -----------------------------
    by_day = Counter(ss.local_day(r["last_touch_at"]) for r in sent_rows if r["last_touch_at"])
    send_days = sorted(by_day)[-14:]
    send_peak = max((by_day[d] for d in send_days), default=1) or 1

    def send_bars():
        out = []
        for d in send_days:
            n = by_day[d]
            pct = round(n / send_peak * 100)
            out.append(
                f'<div class="bar"><div class="bar-track" style="height:100%">'
                f'<div class="bar-fill" style="height:{pct}%;background:var(--series-1)" title="{n} sent"></div>'
                f'</div><div class="bar-n">{n}</div><div class="bar-d">{esc(d[5:])}</div></div>')
        return "".join(out) or '<p class="muted">No sends yet.</p>'

    # ---- geography ---------------------------------------------------------
    by_state = Counter((r.get("state") or "").strip() for r in queue if (r.get("state") or "").strip())

    # ---- enrichment outcome breakdown --------------------------------------
    status_labels = {"ok": "Qualified contact found", "no_email": "No usable email",
                      "unreachable": "Site unreachable", "robots_disallowed": "Blocked by robots.txt"}
    enrich_status = Counter(r.get("status", "") for r in enriched if r.get("status"))

    # ---- keyword coverage ---------------------------------------------------
    kw_counts = Counter()
    for r in enriched:
        for term in (r.get("peptides") or "").split(","):
            term = term.strip()
            if term:
                kw_counts[term] += 1
    top_keywords = kw_counts.most_common(12)

    # ---- discovery sweep progress -------------------------------------------
    try:
        _, sweep_total = discover.plan(1)
        pos = int(discover.PROGRESS.read_text().strip()) if discover.PROGRESS.exists() else 0
        sweep_n = pos // sweep_total + 1 if sweep_total else 1
        sweep_pct = round((pos % sweep_total) / sweep_total * 100) if sweep_total else 0
    except SystemExit:
        sweep_total = pos = sweep_n = sweep_pct = 0

    # ---- replies table -------------------------------------------------------
    replied_rows = [r for r in sent_rows if r["status"] == "replied"]

    def reply_rows():
        out = []
        for r in sorted(replied_rows, key=lambda x: x["last_touch_at"], reverse=True)[:25]:
            out.append(f'<tr><td>{esc(r["domain"])}</td><td>{esc(r["email"])}</td>'
                       f'<td class="muted">{esc(r["last_touch_at"][:10])}</td></tr>')
        return "".join(out) or '<tr><td colspan="3" class="muted">No replies yet.</td></tr>'

    def state_rows():
        if not by_state:
            return '<tr><td colspan="3" class="muted">No geography on file.</td></tr>'
        top = by_state.most_common(10)
        mx = max(n for _, n in top)
        out = []
        for s, n in top:
            pct = round(n / mx * 100)
            out.append(f'<tr><td>{esc(s)}</td><td class="num">{n}</td>'
                       f'<td><div class="mini" style="width:{pct}%"></div></td></tr>')
        return "".join(out)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marinexis Pipeline Dashboard</title>
<style>
  :root {{
    --bg:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b; --muted:#52514e; --faint:#898781; --line:#e1e0d9;
    --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --series-4:#eda100;
    --status-good:#0ca30c; --status-warning:#fab219; --status-serious:#ec835a; --status-critical:#d03b3b;
    --card-soft:#eef4fc;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#0d0d0d; --card:#1a1a19; --ink:#ffffff; --muted:#c3c2b7; --faint:#898781; --line:#2c2c2a;
      --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
      --status-good:#0ca30c; --status-warning:#fab219; --status-serious:#ec835a; --status-critical:#d03b3b;
      --card-soft:#182233;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg:#0d0d0d; --card:#1a1a19; --ink:#ffffff; --muted:#c3c2b7; --faint:#898781; --line:#2c2c2a;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
    --status-good:#0ca30c; --status-warning:#fab219; --status-serious:#ec835a; --status-critical:#d03b3b;
    --card-soft:#182233;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",
        Roboto,Helvetica,Arial,sans-serif; padding:32px 16px 64px; }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:24px; margin:0 0 4px; letter-spacing:-.01em; }}
  h2 {{ font-size:15px; margin:0 0 14px; font-weight:600; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); margin-bottom:24px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
  .kpi .v {{ font-size:28px; font-weight:650; letter-spacing:-.02em; font-variant-numeric:proportional-nums; }}
  .kpi .l {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; margin-top:2px; }}
  .kpi .h {{ color:var(--faint); font-size:12px; margin-top:6px; }}
  .kpi.good .v {{ color:var(--status-good); }}
  .kpi.warn .v {{ color:var(--status-warning); }}
  .kpi.crit .v {{ color:var(--status-critical); }}
  .kpi.accent .v {{ color:var(--series-1); }}
  .panel {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }}
  .panel .note {{ color:var(--muted); font-size:12px; margin-top:8px; }}
  .chart {{ display:flex; align-items:flex-end; gap:8px; height:170px; }}
  .bar {{ flex:1; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; height:100%; }}
  .bar-track {{ position:relative; width:100%; display:flex; align-items:flex-end; }}
  .bar-fill {{ width:100%; border-radius:4px 4px 0 0; min-height:3px; }}
  .bar-target {{ position:absolute; left:0; right:0; border-top:2px dashed var(--faint); }}
  .bar-n {{ font-size:11px; color:var(--muted); margin-top:4px; font-variant-numeric:tabular-nums; }}
  .bar-d {{ font-size:10px; color:var(--faint); }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em;
       color:var(--faint); font-weight:600; padding:0 8px 8px 0; border-bottom:1px solid var(--line); }}
  td {{ padding:8px 8px 8px 0; border-bottom:1px solid var(--line); }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  .mini {{ height:8px; background:var(--card-soft); border:1px solid var(--series-1); border-radius:4px; }}
  .muted {{ color:var(--muted); }}
  .two {{ display:grid; gap:16px; grid-template-columns:1fr 1fr; }}
  @media (max-width:720px) {{ .two {{ grid-template-columns:1fr; }} }}
  .funnel-row {{ display:grid; grid-template-columns:200px 1fr 60px; align-items:center; gap:12px; margin-bottom:10px; }}
  .funnel-label {{ font-size:13px; color:var(--muted); }}
  .funnel-track {{ background:var(--card-soft); border-radius:6px; height:22px; overflow:hidden; }}
  .funnel-fill {{ height:100%; background:var(--series-1); border-radius:6px; }}
  .funnel-n {{ font-size:13px; font-weight:600; text-align:right; font-variant-numeric:tabular-nums; }}
  .hbar-row {{ display:grid; grid-template-columns:1fr 3fr 36px; align-items:center; gap:10px; margin-bottom:7px; }}
  .hbar-label {{ font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .hbar-track {{ background:var(--card-soft); border-radius:4px; height:14px; overflow:hidden; }}
  .hbar-fill {{ height:100%; border-radius:4px; }}
  .hbar-n {{ font-size:12px; text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); }}
  .pill {{ display:inline-flex; align-items:center; gap:6px; font-size:12px; padding:3px 9px; border-radius:999px;
          border:1px solid var(--line); margin:2px 6px 2px 0; }}
  .dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
  .legend {{ margin-top:10px; }}
</style></head><body><div class="wrap">

<h1>Marinexis Biologics &middot; pipeline dashboard</h1>
<div class="sub">Med spa outreach campaign &middot; refreshed {stamp}</div>

<div class="grid">
  <div class="card kpi"><div class="v">{n_candidates}</div><div class="l">Candidates found</div>
    <div class="h">{n_enriched} scraped so far</div></div>
  <div class="card kpi"><div class="v">{n_queue_pending}</div><div class="l">Queue left</div>
    <div class="h">qualified, not yet emailed</div></div>
  <div class="card kpi accent"><div class="v">{n_emailed}</div><div class="l">Emailed</div>
    <div class="h">{today} today &middot; cap {ss.DAILY_CAP}/day</div></div>
  <div class="card kpi good"><div class="v">{n_replied}</div><div class="l">Replied</div>
    <div class="h">{reply_rate:.1f}% of sends</div></div>
  <div class="card kpi warn"><div class="v">{n_bounced}</div><div class="l">Bounced</div>
    <div class="h">{bounce_rate:.1f}% &middot; {n_unsub} unsubscribed</div></div>
  <div class="card kpi"><div class="v">{n_dnc}</div><div class="l">Do-not-contact</div>
    <div class="h">permanently suppressed</div></div>
</div>

<div class="panel">
  <h2>Pipeline funnel</h2>
  {"".join(
    f'<div class="funnel-row"><div class="funnel-label">{esc(label)}</div>'
    f'<div class="funnel-track"><div class="funnel-fill" style="width:{round(n / max(funnel[0][1], 1) * 100)}%">'
    f'</div></div><div class="funnel-n">{n}</div></div>'
    for label, n in funnel
  )}
  <div class="note">Each stage as a share of candidates found. "Qualified" = a real contact email, a peptide
    mention, and a US signal -- the gates <code>medspa/build_queue.py</code> enforces before anyone reaches the
    send queue.</div>
</div>

<div class="panel">
  <h2>Daily scrape volume vs. {target_today}/day target</h2>
  <div class="chart">{scrape_bars()}</div>
  <div class="note">
    <span class="pill"><span class="dot" style="background:var(--status-good)"></span>target met</span>
    <span class="pill"><span class="dot" style="background:var(--status-warning)"></span>below target</span>
    &middot; dashed line = that day's target &middot; {scrape_hit_days}/{scrape_run_days} logged days on target &middot;
    {scrape_total_30} sites scraped in the last 30 logged runs
  </div>
</div>

<div class="two">
  <div class="panel"><h2>Sends per day (last 14 active days)</h2>
    <div class="chart">{send_bars()}</div></div>
  <div class="panel"><h2>Discovery sweep progress</h2>
    <div class="funnel-row" style="grid-template-columns:1fr 60px;">
      <div class="funnel-track"><div class="funnel-fill" style="width:{sweep_pct}%;background:var(--series-3)">
      </div></div><div class="funnel-n">{sweep_pct}%</div></div>
    <div class="note">Sweep {sweep_n} through every city &times; peptide-keyword query combination
      ({sweep_total} total). Position wraps back to 0 once a sweep finishes, so
      <code>daily_scrape.py</code> never runs dry -- it just starts the next sweep.</div>
  </div>
</div>

<div class="two">
  <div class="panel"><h2>Queue by state</h2>
    <table><thead><tr><th>State</th><th>Leads</th><th></th></tr></thead>
    <tbody>{state_rows()}</tbody></table></div>
  <div class="panel"><h2>Replies</h2>
    <table><thead><tr><th>Practice</th><th>Address</th><th>Last touch</th></tr></thead>
    <tbody>{reply_rows()}</tbody></table></div>
</div>

<div class="two">
  <div class="panel"><h2>Scrape outcome (every candidate visited)</h2>
    {bar_col(
      [(status_labels.get(k, k), v) for k, v in enrich_status.most_common()],
      lambda x: x[1], lambda x: x[0], color_var="--series-1",
    )}
    <div class="note">{n_enriched} sites crawled total. A "no usable email" or "blocked by robots.txt" result
      never reaches the send queue -- see <code>medspa/build_queue.py</code>'s gates.</div>
  </div>
  <div class="panel"><h2>Top peptide keywords found on-site</h2>
    {bar_col(top_keywords, lambda x: x[1], lambda x: x[0], color_var="--series-3") or
      '<p class="muted">No enrichment data yet.</p>'}
    <div class="note">Counted from <code>medspa/peptide_terms.txt</code> hits on crawled sites -- this is also
      what <code>daily_scrape.py</code> turns into search queries per city.</div>
  </div>
</div>

</div></body></html>"""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    print(f"dashboard refreshed: candidates={n_candidates} scraped={n_enriched} queue_pending={n_queue_pending} "
          f"emailed={n_emailed} today={today} replies={n_replied} bounces={n_bounced}")


if __name__ == "__main__":
    main()
