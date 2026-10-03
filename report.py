"""Generate a styled HTML report from pipeline results (dependency-free UI).

Used by `demo.py --report`; also importable: write_report(results, path).
"""
from __future__ import annotations

import datetime
import html
import os
from typing import List

from src.models import PipelineResult

_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0;background:#0f1420;color:#e8ecf4}
header{padding:28px 32px;background:linear-gradient(135deg,#1b2a4a,#0f1420);
border-bottom:1px solid #243}
h1{margin:0;font-size:22px} .sub{color:#9fb0cc;font-size:13px;margin-top:6px}
.cards{display:flex;gap:12px;padding:20px 32px;flex-wrap:wrap}
.card{background:#182238;border:1px solid #2a3a5c;border-radius:10px;
padding:14px 18px;min-width:130px}
.card .n{font-size:26px;font-weight:700} .card .l{font-size:12px;color:#9fb0cc}
table{width:calc(100% - 64px);margin:0 32px 32px;border-collapse:collapse;
font-size:13px}
th{text-align:left;padding:10px 12px;background:#182238;color:#9fb0cc;
font-weight:600;border-bottom:1px solid #2a3a5c}
td{padding:10px 12px;border-bottom:1px solid #1d2942;vertical-align:top}
tr:hover td{background:#141d33}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;
font-weight:700}
.paid{background:#123f2a;color:#4ade80} .rej{background:#4a1d1d;color:#f87171}
.hold{background:#4a3a12;color:#fbbf24}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
details{margin-top:6px} summary{cursor:pointer;color:#7ea4e8;font-size:12px}
.kv{color:#9fb0cc;font-size:12px;margin:2px 0}
"""

_OUTCOME_CLASS = {"PAID": "paid", "HOLD_REVIEW": "hold"}


def _badge(outcome: str) -> str:
    cls = _OUTCOME_CLASS.get(outcome, "rej")
    return f'<span class="badge {cls}">{html.escape(outcome)}</span>'


def write_report(results: List[PipelineResult], path: str = "report.html") -> str:
    total = len(results)
    paid = sum(1 for r in results if r.outcome == "PAID")
    held = sum(1 for r in results if r.outcome == "HOLD_REVIEW")
    rejected = total - paid - held
    paid_sum = sum(r.invoice.total_amount or 0 for r in results if r.outcome == "PAID")

    rows = []
    for r in results:
        d = r.to_dict()
        inv = d["invoice"]
        issues = "".join(
            f"<div class='kv'>[{i['severity']}] <span class='mono'>{i['code']}</span>: "
            f"{html.escape(i['message'][:160])}</div>"
            for i in d["validation"]["issues"])
        flags = ", ".join(html.escape(f) for f in d["approval"]["risk_flags"])
        amt = inv["total_amount"]
        amt_s = f"{inv['currency']} {amt:,.2f}" if amt is not None else "n/a"
        items = "".join(
            f"<div class='kv'><span class='mono'>{html.escape(i['item'])}</span> "
            f"x{i['quantity']}</div>" for i in inv["items"])
        rows.append(f"""<tr>
<td class="mono">{html.escape(os.path.basename(d['source_file']))}</td>
<td>{_badge(d['outcome'])}</td>
<td>{html.escape(inv['vendor'] or '—')}</td>
<td class="mono">{amt_s}</td>
<td><details><summary>{len(inv['items'])} lines, {len(d['validation']['issues'])} issues</summary>
{items}{issues}
<div class='kv'>reasoning: {html.escape(d['outcome_reason'][:300])}</div>
<div class='kv'>risk: {flags or '—'}</div>
<div class='kv'>run: <span class='mono'>{d['run_id']}</span></div>
</details></td></tr>""")

    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Invoice Automation — Run Report</title><style>{_CSS}</style></head><body>
<header><h1>Invoice Processing — Run Report</h1>
<div class="sub">Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} ·
{total} invoices · multi-agent pipeline (ingest → validate → approve → pay)</div></header>
<div class="cards">
<div class="card"><div class="n" style="color:#4ade80">{paid}</div><div class="l">paid</div></div>
<div class="card"><div class="n" style="color:#fbbf24">{held}</div><div class="l">held for review</div></div>
<div class="card"><div class="n" style="color:#f87171">{rejected}</div><div class="l">rejected</div></div>
<div class="card"><div class="n">${paid_sum:,.0f}</div><div class="l">paid out</div></div>
</div>
<table><tr><th>file</th><th>outcome</th><th>vendor</th><th>amount</th><th>detail</th></tr>
{''.join(rows)}</table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path
