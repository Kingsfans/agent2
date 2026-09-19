"""Build exports/lead_research_dashboard.html from the master lead file.

Standard library only. Reads exports/medspa_leads_master.csv and writes one
self-contained, filterable HTML page: how many practices were found, how many
carry a contact address, and -- the part that matters before anyone hits send --
how much that address can be trusted.

    python3 outreach/lead_dashboard.py

Companion to dash_update.py. That one tracks the campaign (who was emailed, who
replied); this one tracks the research feeding it.
"""
import csv
import html
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "exports" / "medspa_leads_master.csv"
OUT = ROOT / "exports" / "lead_research_dashboard.html"

REGION_LABELS = {"las_vegas": "Las Vegas metro", "northern_nv": "Northern Nevada",
                 "va_nj_ct": "Virginia / NJ / CT", "oh_pa": "Ohio / Pennsylvania",
                 "tn_al_sc": "Tennessee / Alabama / SC", "wa_or_mn": "Washington / Oregon / Minnesota"}
STATUS_LABELS = {"new_with_email": "New, address found", "new_no_email": "New, no address yet",
                 "already_in_queue": "Already in send queue", "do_not_contact": "Suppressed (do-not-contact)",
                 "already_contacted": "Already contacted"}


def esc(s):
    return html.escape(str(s or ""))


def bars(items, total, color="--series-1"):
    """Single-series magnitude bars: one hue, length carries the number, the
    label carries identity."""
    total = total or 1
    out = []
    for label, n in items:
        pct = round(n / total * 100)
        out.append(
            f'<div class="hbar-row"><div class="hbar-label">{esc(label)}</div>'
            f'<div class="hbar-track"><div class="hbar-fill" style="width:{pct}%;'
            f'background:var({color})"></div></div><div class="hbar-n">{n}</div></div>')
    return "".join(out) or '<p class="muted">Nothing yet.</p>'


def main():
    if not SRC.exists():
        sys.exit(f"no {SRC} -- nothing to chart yet")
    rows = list(csv.DictReader(open(SRC, newline="", encoding="utf-8")))

    found = [r for r in rows if r["email"]]
    tier_a = [r for r in found if r["email_tier"] == "A_verified"]
    tier_b = [r for r in found if r["email_tier"] == "B_unconfirmed"]
    # A flagged row (franchise HQ, multi-state mobile operator, not a med spa)
    # can be perfectly real and still be the wrong practice to email.
    flagged = [r for r in rows if r.get("flag")]
    sendable = [r for r in tier_a if r["status"] == "new_with_email" and not r.get("flag")]
    mismatch = [r for r in found if r["domain_mismatch"]]
    hit_rate = len(found) / len(rows) * 100 if rows else 0

    funnel = [("Practices discovered", len(rows)),
              ("Contact address found", len(found)),
              ("Tier A (non-guessable)", len(tier_a)),
              ("Clear to send today", len(sendable))]

    by_region = []
    for key in REGION_LABELS:
        sub = [r for r in rows if r["region"] == key]
        if sub:
            sub_found = [r for r in sub if r["email"]]
            sub_a = [r for r in sub_found if r["email_tier"] == "A_verified"]
            by_region.append((REGION_LABELS[key], len(sub), len(sub_found), len(sub_a)))

    status_counts = [(STATUS_LABELS.get(k, k), v)
                     for k, v in Counter(r["status"] for r in rows).most_common()]

    # The evidence panel: a model inventing addresses writes info@own-domain
    # nearly every time, so the prefix mix is itself the quality signal.
    prefixes = Counter()
    for r in found:
        local, _, edom = r["email"].lower().partition("@")
        if edom != r["domain"].lower():
            prefixes["different domain"] += 1
        elif local in ("info", "contact", "hello"):
            prefixes[f"{local}@ (guessable)"] += 1
        else:
            prefixes[f"{local}@"] += 1
    top_prefixes = prefixes.most_common(9)

    def lead_rows():
        out = []
        for r in sorted(rows, key=lambda x: (x["email_tier"] != "A_verified",
                                             x["email_tier"] != "B_unconfirmed",
                                             x["region"], x["business_name"].lower())):
            tier = r["email_tier"]
            badge = ('<span class="pill tier-a">A</span>' if tier == "A_verified"
                     else '<span class="pill tier-b">B</span>' if tier == "B_unconfirmed"
                     else '<span class="muted">&mdash;</span>')
            email = (f'<a href="mailto:{esc(r["email"])}">{esc(r["email"])}</a>'
                     if r["email"] else '<span class="muted">not found</span>')
            src = (f'<a href="{esc(r["email_source"])}" target="_blank" rel="noopener">source</a>'
                   if r["email_source"] else "")
            flag = ' <span class="warn" title="address is on a different domain">&#9888;</span>' if r["domain_mismatch"] else ""
            if r.get("flag"):
                flag += f' <span class="pill flagged" title="{esc(r["flag"])}">{esc(r["flag"].replace("_", " "))}</span>'
            out.append(
                f'<tr data-region="{esc(r["region"])}" data-tier="{esc(tier)}" '
                f'data-status="{esc(r["status"])}" '
                f'data-text="{esc((r["business_name"] + " " + r["domain"] + " " + r["email"] + " " + r["city"]).lower())}">'
                f'<td><a href="{esc(r["website"])}" target="_blank" rel="noopener">{esc(r["business_name"])}</a>'
                f'<div class="muted sm">{esc(r["domain"])}</div></td>'
                f'<td class="nowrap">{esc(r["city"])}, {esc(r["state"])}</td>'
                f'<td>{email}{flag}</td><td class="ctr">{badge}</td>'
                f'<td class="sm">{esc(STATUS_LABELS.get(r["status"], r["status"]))}</td>'
                f'<td class="sm">{src}</td></tr>')
        return "".join(out)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lead Research</title>
<style>
  :root {{
    --bg:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b; --muted:#52514e; --faint:#898781; --line:#e1e0d9;
    --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
    --status-good:#0ca30c; --status-warning:#fab219; --status-critical:#d03b3b; --card-soft:#eef4fc;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --muted:#c3c2b7; --faint:#898781; --line:#2c2c2a;
      --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --card-soft:#182233;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --muted:#c3c2b7; --faint:#898781; --line:#2c2c2a;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --card-soft:#182233;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); padding:32px 16px 64px;
         font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:24px; margin:0 0 4px; letter-spacing:-.01em; }}
  h2 {{ font-size:15px; margin:0 0 14px; font-weight:600; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); margin-bottom:24px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
  .kpi .v {{ font-size:28px; font-weight:650; letter-spacing:-.02em; }}
  .kpi .l {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; margin-top:2px; }}
  .kpi .h {{ color:var(--faint); font-size:12px; margin-top:6px; }}
  .kpi.good .v {{ color:var(--status-good); }}
  .kpi.accent .v {{ color:var(--series-1); }}
  .panel {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }}
  .note {{ color:var(--muted); font-size:12px; margin-top:10px; }}
  .two {{ display:grid; gap:16px; grid-template-columns:1fr 1fr; }}
  @media (max-width:820px) {{ .two {{ grid-template-columns:1fr; }} }}
  .hbar-row {{ display:grid; grid-template-columns:minmax(176px,auto) 1fr 44px; align-items:center;
              gap:10px; margin-bottom:7px; }}
  .hbar-label {{ font-size:12px; color:var(--muted); }}
  .hbar-track {{ background:var(--card-soft); border-radius:4px; height:14px; overflow:hidden; }}
  .hbar-fill {{ height:100%; border-radius:4px; }}
  .hbar-n {{ font-size:12px; text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--faint);
       font-weight:600; padding:0 8px 8px 0; border-bottom:1px solid var(--line); }}
  td {{ padding:9px 8px 9px 0; border-bottom:1px solid var(--line); vertical-align:top; }}
  td.nowrap {{ white-space:nowrap; }} td.ctr {{ text-align:center; }}
  .sm {{ font-size:12px; }} .muted {{ color:var(--muted); }}
  a {{ color:var(--series-1); }}
  .pill {{ display:inline-block; font-size:11px; font-weight:650; padding:2px 8px; border-radius:999px; }}
  .tier-a {{ background:var(--status-good); color:#fff; }}
  .tier-b {{ background:var(--status-warning); color:#1a1a19; }}
  .flagged {{ background:var(--card-soft); color:var(--muted); border:1px solid var(--line);
             font-weight:500; }}
  .warn {{ color:var(--status-warning); }}
  .filters {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
  input, select {{ font:inherit; font-size:13px; padding:7px 10px; border-radius:8px;
                   border:1px solid var(--line); background:var(--card); color:var(--ink); }}
  input {{ flex:1; min-width:200px; }}
  #count {{ color:var(--muted); font-size:12px; align-self:center; }}
</style></head><body><div class="wrap">

<h1>Lead research</h1>
<div class="sub">Marinexis Biologics &middot; med spa prospecting &middot; {stamp}</div>

<div class="grid">
  <div class="card kpi"><div class="v">{len(rows)}</div><div class="l">Practices found</div>
    <div class="h">across {len(by_region)} regions</div></div>
  <div class="card kpi accent"><div class="v">{len(found)}</div><div class="l">Address found</div>
    <div class="h">{hit_rate:.0f}% of practices</div></div>
  <div class="card kpi good"><div class="v">{len(tier_a)}</div><div class="l">Tier A</div>
    <div class="h">non-guessable, trust these</div></div>
  <div class="card kpi"><div class="v">{len(tier_b)}</div><div class="l">Tier B</div>
    <div class="h">verify before sending</div></div>
  <div class="card kpi"><div class="v">{len(sendable)}</div><div class="l">Clear to send</div>
    <div class="h">Tier A, not queued or suppressed</div></div>
</div>

<div class="panel"><h2>From search to sendable</h2>
  {bars(funnel, len(rows))}
  <div class="note">Every stage is a real filter. The drop from discovered to address-found is
    whether a contact page happened to be indexed; the drop to Tier A is whether the address
    could have been invented rather than read.</div>
</div>

<div class="two">
  <div class="panel"><h2>Coverage by region</h2>
    <table><thead><tr><th>Region</th><th>Found</th><th>Address</th><th>Tier A</th></tr></thead><tbody>
    {"".join(f'<tr><td>{esc(n)}</td><td>{t}</td><td>{e}</td><td>{a}</td></tr>'
             for n, t, e, a in by_region)}
    </tbody></table>
    <div class="note">Virginia, New Jersey and Connecticut were chosen because the existing
      429-lead queue has no coverage there &mdash; a sweep of Florida or California would
      mostly rediscover leads you already hold.</div>
  </div>
  <div class="panel"><h2>Address shape &mdash; the quality signal</h2>
    {bars(top_prefixes, max((n for _, n in top_prefixes), default=1), "--series-3")}
    <div class="note">A model inventing an address writes <code>info@</code> at the practice's own
      domain almost every time. Distinctive prefixes and addresses on a different domain are
      things a guesser would never produce, which is why they are graded Tier A.</div>
  </div>
</div>

<div class="panel"><h2>Record status</h2>
  {bars(status_counts, len(rows), "--series-2")}
  <div class="note">{len(mismatch)} addresses sit on a different domain than the practice site
    (flagged &#9888; in the table) &mdash; usually a rebrand, and worth a glance before sending.</div>
</div>

<div class="panel">
  <h2>All leads</h2>
  <div class="filters">
    <input id="q" type="search" placeholder="Search name, domain, city or address&hellip;">
    <select id="region"><option value="">All regions</option>
      {"".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in REGION_LABELS.items())}
    </select>
    <select id="tier"><option value="">All tiers</option>
      <option value="A_verified">Tier A only</option>
      <option value="B_unconfirmed">Tier B only</option>
      <option value="none">No address</option>
    </select>
    <span id="count"></span>
  </div>
  <table><thead><tr><th>Practice</th><th>Location</th><th>Contact address</th>
    <th class="ctr">Tier</th><th>Status</th><th>Proof</th></tr></thead>
  <tbody id="leads">{lead_rows()}</tbody></table>
</div>

</div>
<script>
(function () {{
  var q = document.getElementById('q'), region = document.getElementById('region'),
      tier = document.getElementById('tier'), count = document.getElementById('count'),
      rows = Array.prototype.slice.call(document.querySelectorAll('#leads tr'));
  function apply() {{
    var text = q.value.trim().toLowerCase(), r = region.value, t = tier.value, shown = 0;
    rows.forEach(function (row) {{
      var rowTier = row.dataset.tier || 'none';
      var ok = (!text || row.dataset.text.indexOf(text) !== -1)
            && (!r || row.dataset.region === r)
            && (!t || (t === 'none' ? !row.dataset.tier : rowTier === t));
      row.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }});
    count.textContent = shown + ' of ' + rows.length + ' shown';
  }}
  [q, region, tier].forEach(function (el) {{ el.addEventListener('input', apply); }});
  apply();
}})();
</script>
</body></html>"""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    print(f"lead dashboard: {len(rows)} practices, {len(found)} with an address "
          f"(tier A {len(tier_a)}, tier B {len(tier_b)}), {len(sendable)} clear to send -> {OUT}")


if __name__ == "__main__":
    main()
