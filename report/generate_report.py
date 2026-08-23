"""Phase 5 -- static HTML report (Python-generated, per the PRD's own
contingency plan: no Next.js, no build step, reads engine's results.json).

Run: python -m report.generate_report
Produces: report/report.html
"""
from __future__ import annotations

import json
from pathlib import Path

from engine.customer_status import compute_from_db, suggested_split
from engine.db import connect

ROOT = Path(__file__).parent.parent
OUT_DIR = ROOT / "out"
DB_PATH = OUT_DIR / "finance.db"

# -- palette (validated default, see dataviz skill references/palette.md) --
LIGHT = {
    "surface": "#fcfcfb", "page": "#f9f9f7", "text": "#0b0b0b",
    "text2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
    "axis": "#c3c2b7", "border": "rgba(11,11,11,0.10)",
    "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b",
    "blue": "#2a78d6",
}
DARK = {
    "surface": "#1a1a19", "page": "#0d0d0d", "text": "#ffffff",
    "text2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
    "axis": "#383835", "border": "rgba(255,255,255,0.10)",
    "good": "#0ca30c", "warning": "#fab219", "critical": "#e66767",
    "blue": "#3987e5",
}

REASON_LABELS = {
    "EXACT": "Exact match", "TOL-FEE": "Tolerance write-off",
    "PART-EXP": "Partial, more expected", "RESID-DED": "Residual deduction",
    "BULK-N": "Bulk settlement", "REF-FUZZY": "Reference repaired",
    "FIFO-TIE": "FIFO tie-break", "DUP-ONACC": "Duplicate / on-account",
    "SUSPENSE": "Suspense (payer unknown)", "AMBIG-N": "Ambiguous candidates",
    "NO-MATCH": "No match",
}
EXCEPTION_CODES = {"SUSPENSE", "AMBIG-N", "NO-MATCH"}


def rupees(paise: int) -> str:
    return f"Rs {paise/100:,.2f}"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x*100:.1f}%"


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_threshold_chart(curve: list[dict], default_threshold: int) -> str:
    W, H = 760, 320
    ML, MR, MT, MB = 54, 16, 34, 34
    plot_w, plot_h = W - ML - MR, H - MT - MB
    max_val = max(max(r["auto_matched"], r["human_touches"], r["wrong_matches"])
                  for r in curve)
    # round tick step to a clean number, then round y_max up to a multiple of it
    tick_step = next(s for s in (5, 10, 15, 20, 25, 50) if max_val <= s * 5)
    y_max = ((max_val // tick_step) + 1) * tick_step

    def x_of(t: int) -> float:
        return ML + (t / 100) * plot_w

    def y_of(v: int) -> float:
        return MT + plot_h - (v / y_max) * plot_h

    series = [
        ("auto_matched", "Auto-matched", "var(--good)"),
        ("human_touches", "Human touches", "var(--warning)"),
        ("wrong_matches", "Wrong matches", "var(--critical)"),
    ]

    svg = [f'<svg class="chart-svg" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Confidence threshold trade-off curve">']

    # gridlines (y) + ticks, at clean multiples of tick_step
    for val in range(0, y_max + 1, tick_step):
        y = y_of(val)
        svg.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{W-MR}" y2="{y:.1f}" '
                    f'class="gridline"/>')
        svg.append(f'<text x="{ML-8}" y="{y+4:.1f}" class="axis-label" '
                    f'text-anchor="end">{val}</text>')
    # x axis ticks
    for t in (0, 20, 40, 60, 80, 100):
        x = x_of(t)
        svg.append(f'<text x="{x:.1f}" y="{H-MB+20}" class="axis-label" '
                    f'text-anchor="middle">{t}</text>')
    svg.append(f'<line x1="{ML}" y1="{MT+plot_h}" x2="{W-MR}" y2="{MT+plot_h}" '
                f'class="axis-line"/>')

    # default-threshold reference line
    dx = x_of(default_threshold)
    svg.append(f'<line x1="{dx:.1f}" y1="{MT}" x2="{dx:.1f}" y2="{MT+plot_h}" '
                f'class="ref-line"/>')
    svg.append(f'<text x="{dx:.1f}" y="{MT-12}" class="axis-label" '
                f'text-anchor="middle">default dial ({default_threshold})</text>')

    default_row = next(r for r in curve if r["threshold"] == default_threshold)

    for key, label, color in series:
        pts = [(x_of(r["threshold"]), y_of(r[key])) for r in curve]
        path = " ".join(f"{'M' if i==0 else 'L'}{x:.1f},{y:.1f}"
                         for i, (x, y) in enumerate(pts))
        svg.append(f'<path d="{path}" fill="none" stroke="{color}" '
                    f'stroke-width="2" stroke-linejoin="round" '
                    f'stroke-linecap="round"/>')
        mx, my = x_of(default_threshold), y_of(default_row[key])
        svg.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="4.5" fill="{color}" '
                    f'stroke="var(--surface)" stroke-width="2"/>')
        svg.append(f'<title>{esc(label)}: {default_row[key]} at threshold '
                    f'{default_threshold}</title>')
        label_y = my - 10 if key != "human_touches" else my + 16
        svg.append(f'<text x="{mx+8:.1f}" y="{label_y:.1f}" class="point-label" '
                    f'fill="var(--text)">{default_row[key]}</text>')

    svg.append('</svg>')
    return "\n".join(svg)


def build_legend(series: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<span class="legend-item"><span class="swatch" '
        f'style="background:{color}"></span>{esc(label)}</span>'
        for label, color in series)
    return f'<div class="legend">{items}</div>'


def render_body(results: dict, sample_allocations: list[dict], totals: dict,
                 provisional_customers: list[dict], holdout: dict | None = None) -> str:
    default_threshold = results["default_threshold"]

    kpis = [
        ("Match rate vs ground truth", pct(results["overall_match_rate"]),
         f"{totals['payments']} synthetic payments, 11 reason codes"),
        ("Auto-resolution rate", pct(results["auto_resolution_rate"]),
         "industry benchmark: 85-95% straight-through"),
        ("Human touches", str(results["human_touches"]),
         f"{results['raw_exception_count']} raw exceptions, grouped"),
        ("Value requiring review", rupees(results["value_requiring_review_paise"]),
         "rupee view, not row count"),
        ("Precision @ default dial", pct(results["precision_at_default_threshold"]),
         f"of what posted automatically at {default_threshold}"),
        ("Resolved on re-queue", str(results["resolved_on_requeue_count"]),
         "genuinely unresolvable on arrival, closed by a later payment"),
    ]
    kpi_html = "".join(
        f'<div class="stat-tile{" headline" if l == "Auto-resolution rate" else ""}">'
        f'<div class="stat-label">{esc(l)}</div>'
        f'<div class="stat-value">{v}</div>'
        f'<div class="stat-sub">{esc(s)}</div></div>'
        for l, v, s in kpis)

    breakdown_rows = "".join(
        f'<tr><td class="mono">{esc(code)}</td><td class="text-secondary">{esc(REASON_LABELS.get(code, code))}</td>'
        f'<td class="num">{d["expected"]}</td><td class="num">{d["correct"]}</td>'
        f'<td class="num">{pct(d["accuracy"])}</td></tr>'
        for code, d in sorted(results["reason_code_breakdown"].items(),
                               key=lambda kv: -kv[1]["expected"]))

    eq = results["exception_queue"]
    group_sections = []
    for code, group in sorted(eq["by_code"].items(),
                               key=lambda kv: -kv[1]["value_paise"]):
        rows_html = "".join(
            f'<tr><td class="mono">{esc(r["payment_id"])}</td>'
            f'<td class="text-secondary">{esc(r["payer_name"] or "-")}</td>'
            f'<td class="num">{rupees(r["amount_paise"])}</td>'
            f'<td class="text-secondary">{esc(r["rationale"])}</td></tr>'
            for r in group["rows"])
        group_sections.append(
            f'<div class="exception-group">'
            f'<div class="exception-group-head">'
            f'<span class="code-pill code-{code.lower().replace("-", "")}">{esc(code)}</span>'
            f'<span class="text-secondary">{esc(REASON_LABELS.get(code, code))} '
            f'&middot; {group["count"]} payments &middot; {rupees(group["value_paise"])}</span>'
            f'</div>'
            f'<table class="data-table"><thead><tr><th>Payment</th><th>Payer</th>'
            f'<th class="num">Amount</th><th>Why it\'s parked</th></tr></thead>'
            f'<tbody>{rows_html}</tbody></table></div>')

    prov_blocks = []
    for c in provisional_customers:
        split_items = "".join(
            f'<span>{esc(s["invoice_id"])}: {rupees(s["amount_paise"])}</span>&nbsp;&nbsp;'
            for s in c["suggested_split"])
        prov_blocks.append(f'''<div class="prov-customer">
      <div>
        <div class="prov-customer-id mono">{esc(c["customer_id"])} &middot; {esc(c["name"])}</div>
        <div class="prov-customer-sub">{c["unmatched_payment_count"]}
          payment{"s" if c["unmatched_payment_count"] != 1 else ""} never matched an
          invoice, totalling {rupees(c["unmatched_cash_paise"])}</div>
        <div class="tier tier-ledger">
          <div class="tier-label">Naive ledger view</div>
          <div class="tier-value">{rupees(c["open_balance_ledger_paise"])} open</div>
          <div class="tier-sub">of {rupees(c["total_invoiced_paise"])} invoiced, looking only at
            posted allocations</div>
        </div>
      </div>
      <div>
        <div class="prov-customer-id">&nbsp;</div>
        <div class="prov-customer-sub">&nbsp;</div>
        <div class="tier tier-certain">
          <div class="tier-label">Certain balance</div>
          <div class="tier-value">{rupees(c["balance_paise"])} owed</div>
          <div class="tier-sub">ledger balance minus cash we know arrived, unapplied</div>
        </div>
        <div class="split-list">Assumed split (FIFO, not posted): {split_items or "&mdash;"}</div>
      </div>
    </div>''')
    prov_customer_html = "".join(prov_blocks) if prov_blocks else (
        '<p class="text-secondary" style="margin:0;">No PROVISIONAL customers in this run.</p>')

    chart_svg = build_threshold_chart(results["threshold_curve"], default_threshold)
    legend = build_legend([("Auto-matched", "var(--good)"),
                            ("Human touches", "var(--warning)"),
                            ("Wrong matches", "var(--critical)")])

    sample_rows = "".join(
        f'<tr><td class="mono">{esc(r["payment_id"])}</td>'
        f'<td class="mono">{esc(r["invoice_id"] or "-")}</td>'
        f'<td class="num">{rupees(r["amount_paise"])}</td>'
        f'<td><span class="code-pill code-{r["reason_code"].lower().replace("-", "")}">'
        f'{esc(r["reason_code"])}</span></td>'
        f'<td class="num">{r["confidence"]}</td>'
        f'<td class="text-secondary">{esc(r["rationale"])}</td></tr>'
        for r in sample_allocations)

    holdout_section = ""
    if holdout is not None:
        holdout_mismatch_rows = "".join(
            f'<tr><td class="mono">{esc(m["payment_id"])}</td>'
            f'<td class="text-secondary">expected <b>{esc(m["expected_reason_code"])}</b>, '
            f'got <b>{esc(m["actual_reason_code"])}</b><br>{esc(m["notes"])}</td></tr>'
            for m in holdout["mismatches"])
        mismatch_table = (
            f'<div class="card"><table class="data-table"><thead><tr><th>Payment</th>'
            f'<th>What happened</th></tr></thead><tbody>{holdout_mismatch_rows}'
            f'</tbody></table></div>') if holdout_mismatch_rows else ""
        holdout_section = f'''
  <h2>Held-out validation &mdash; what happens on data we didn't design for</h2>
  <p class="tier-note">The submission dataset above is built to the same 12-code
    spec the engine is written against &mdash; a match rate against it alone
    proves the code runs, not that it generalizes. This holdout batch is
    messy cases the spec never enumerated: a phantom invoice reference, a 2x
    overpayment, near-identical payer names, a refund, a payment predating
    its own invoice, a narration naming two conflicting invoices. Two of six
    categories are documented misses, explained below.</p>
  <div class="kpi-row" style="grid-template-columns: repeat(2, 1fr); margin-bottom: 16px;">
    <div class="stat-tile"><div class="stat-label">Submission match rate</div>
      <div class="stat-value">{pct(results["overall_match_rate"])}</div>
      <div class="stat-sub">spec-matched dataset</div></div>
    <div class="stat-tile headline"><div class="stat-label">Holdout match rate</div>
      <div class="stat-value">{pct(holdout["overall_match_rate"])}</div>
      <div class="stat-sub">messy cases the spec never enumerated</div></div>
  </div>
  {mismatch_table}
'''

    return f'''<title>AI Finance Controller</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root {{
    color-scheme: light;
    --surface: {LIGHT["surface"]}; --page: {LIGHT["page"]};
    --text: {LIGHT["text"]}; --text2: {LIGHT["text2"]}; --muted: {LIGHT["muted"]};
    --grid: {LIGHT["grid"]}; --axis: {LIGHT["axis"]}; --border: {LIGHT["border"]};
    --good: {LIGHT["good"]}; --warning: {LIGHT["warning"]}; --critical: {LIGHT["critical"]};
    --blue: {LIGHT["blue"]};
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface: {DARK["surface"]}; --page: {DARK["page"]};
      --text: {DARK["text"]}; --text2: {DARK["text2"]}; --muted: {DARK["muted"]};
      --grid: {DARK["grid"]}; --axis: {DARK["axis"]}; --border: {DARK["border"]};
      --good: {DARK["good"]}; --warning: {DARK["warning"]}; --critical: {DARK["critical"]};
      --blue: {DARK["blue"]};
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface: {DARK["surface"]}; --page: {DARK["page"]};
    --text: {DARK["text"]}; --text2: {DARK["text2"]}; --muted: {DARK["muted"]};
    --grid: {DARK["grid"]}; --axis: {DARK["axis"]}; --border: {DARK["border"]};
    --good: {DARK["good"]}; --warning: {DARK["warning"]}; --critical: {DARK["critical"]};
    --blue: {DARK["blue"]};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--page); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 32px 20px 64px;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .tagline {{ color: var(--text2); margin: 0 0 28px; font-size: 14px; }}
  h1 {{ position: relative; padding-left: 14px; }}
  h1::before {{
    content: ""; position: absolute; left: 0; top: 3px; bottom: 3px;
    width: 4px; border-radius: 2px; background: var(--blue);
  }}
  h2 {{
    font-size: 13px; margin: 40px 0 12px; color: var(--text2);
    text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
  }}
  .mono {{ font-family: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 20px; overflow-x: auto;
  }}
  .kpi-row {{
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
  }}
  .stat-tile {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px;
  }}
  .stat-tile.headline {{ border-color: var(--blue); border-width: 1.5px; }}
  .stat-label {{ font-size: 12px; color: var(--text2); }}
  .stat-value {{
    font-size: 26px; font-weight: 600; margin: 4px 0;
    font-family: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  }}
  .stat-tile.headline .stat-value {{ color: var(--blue); }}
  .stat-sub {{ font-size: 11px; color: var(--muted); }}
  .legend {{ display: flex; gap: 16px; margin-bottom: 8px; font-size: 12px; color: var(--text2); }}
  .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
  .swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .chart-svg {{ width: 100%; height: auto; }}
  .gridline {{ stroke: var(--grid); stroke-width: 1; }}
  .axis-line {{ stroke: var(--axis); stroke-width: 1; }}
  .ref-line {{ stroke: var(--axis); stroke-width: 1; stroke-dasharray: 3 3; }}
  .axis-label {{ font-size: 10px; fill: var(--muted); }}
  .point-label {{ font-size: 11px; font-weight: 600; font-family: "IBM Plex Mono", ui-monospace, monospace; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.data-table th {{
    text-align: left; font-weight: 500; color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.03em;
    padding: 8px 10px; border-bottom: 1px solid var(--grid);
  }}
  table.data-table td {{
    padding: 8px 10px; border-bottom: 1px solid var(--grid);
    font-variant-numeric: tabular-nums;
  }}
  table.data-table td.num {{ text-align: right; font-family: "IBM Plex Mono", ui-monospace, monospace; }}
  th.num {{ text-align: right; }}
  .text-secondary {{ color: var(--text2); }}
  .exception-group {{ margin-bottom: 20px; }}
  .exception-group-head {{
    display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 13px;
  }}
  .code-pill {{
    display: inline-flex; align-items: center; gap: 5px; padding: 2px 9px 2px 7px;
    border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.02em;
    border: 1px solid var(--border); color: var(--text2);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }}
  .code-pill::before {{
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: var(--pill-color); flex: none;
  }}
  .code-suspense, .code-ambign, .code-nomatch {{ --pill-color: var(--critical); }}
  .code-tolfee, .code-partexp, .code-residded {{ --pill-color: var(--warning); }}
  .code-exact, .code-bulkn, .code-reffuzzy, .code-fifotie, .code-duponacc {{ --pill-color: var(--good); }}
  footer {{ margin-top: 48px; color: var(--muted); font-size: 12px; }}
  .tier-note {{ color: var(--text2); font-size: 13px; margin: -4px 0 16px; max-width: 66ch; }}
  .prov-customer {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
    padding: 16px 0; border-bottom: 1px solid var(--grid);
  }}
  .prov-customer:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .prov-customer-id {{ font-size: 13px; font-weight: 600; margin-bottom: 2px; }}
  .prov-customer-sub {{ font-size: 12px; color: var(--muted); margin-bottom: 10px; }}
  .tier {{ padding: 10px 12px; border-radius: 8px; }}
  .tier-ledger {{ background: color-mix(in srgb, var(--warning) 10%, transparent); }}
  .tier-certain {{ background: color-mix(in srgb, var(--good) 10%, transparent); }}
  .tier-label {{
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text2); margin-bottom: 4px;
  }}
  .tier-value {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 18px; font-weight: 600;
  }}
  .tier-sub {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}
  .split-list {{ font-size: 11px; color: var(--muted); margin-top: 8px; }}
  .split-list span {{ font-family: "IBM Plex Mono", ui-monospace, monospace; color: var(--text2); }}
  @media (max-width: 640px) {{ .prov-customer {{ grid-template-columns: 1fr; }} }}
  @media (max-width: 760px) {{ .kpi-row {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
<div class="wrap">
  <h1>AI Finance Controller</h1>
  <p class="tagline">Payment-to-invoice allocation, {totals["payments"]} payments
    against {totals["invoices"]} open invoices &middot; measured, not asserted</p>

  <div class="kpi-row">{kpi_html}</div>
{holdout_section}
  <h2>Two-tier report &mdash; certain balance vs. assumed split</h2>
  <p class="tier-note">A customer whose payments never individually matched
    an invoice isn't a blank. Every payment below is, correctly, its own
    NO-MATCH exception &mdash; and the customer's outstanding balance is
    still provably exact, because it's arithmetic (invoiced minus received),
    not allocation. What's <em>not</em> provable is which specific invoice
    each rupee will eventually close, so that split is FIFO-assumed and
    never posted to the ledger.</p>
  <div class="kpi-row" style="grid-template-columns: repeat(2, 1fr); margin-bottom: 16px;">
    <div class="stat-tile"><div class="stat-label">Customer status accuracy</div>
      <div class="stat-value">{pct(results["customer_status"]["status_accuracy"])}</div>
      <div class="stat-sub">CLEAN / PARTIAL / PROVISIONAL vs ground truth</div></div>
    <div class="stat-tile headline"><div class="stat-label">Balance accuracy</div>
      <div class="stat-value">{pct(results["customer_status"]["balance_accuracy"])}</div>
      <div class="stat-sub">certain even for PROVISIONAL customers</div></div>
  </div>
  <div class="card">{prov_customer_html}</div>

  <h2>Confidence threshold trade-off</h2>
  <div class="card">
    {legend}
    {chart_svg}
  </div>

  <h2>Reason code accuracy vs ground truth</h2>
  <div class="card">
    <table class="data-table">
      <thead><tr><th>Code</th><th>Meaning</th><th class="num">Expected</th>
      <th class="num">Correct</th><th class="num">Accuracy</th></tr></thead>
      <tbody>{breakdown_rows}</tbody>
    </table>
  </div>

  <h2>Exception queue &mdash; ranked by value</h2>
  <div class="card">{"".join(group_sections)}</div>

  <h2>Sample allocations</h2>
  <div class="card">
    <table class="data-table">
      <thead><tr><th>Payment</th><th>Invoice</th><th class="num">Amount</th>
      <th>Reason</th><th class="num">Confidence</th><th>Rationale</th></tr></thead>
      <tbody>{sample_rows}</tbody>
    </table>
  </div>

  <footer>Generated from out/results.json by report/generate_report.py &middot;
    AI Finance Controller &middot; Razorpay Buildathon Track 04</footer>
</div>
'''


def main() -> None:
    with open(OUT_DIR / "results.json") as f:
        results = json.load(f)

    holdout = None
    holdout_path = OUT_DIR / "results_holdout.json"
    if holdout_path.exists():
        with open(holdout_path) as f:
            holdout = json.load(f)

    conn = connect(DB_PATH)
    invoice_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    totals = {"payments": results["totals"]["payments"], "invoices": invoice_count}
    sample_allocations = [dict(r) for r in conn.execute(
        "SELECT payment_id, invoice_id, amount_paise, reason_code, "
        "confidence, rationale FROM allocations "
        "ORDER BY allocation_id LIMIT 20")]

    rollups = compute_from_db(conn)
    provisional_customers = []
    for cid, r in sorted(rollups.items(), key=lambda kv: -kv[1].balance_paise):
        if r.status != "PROVISIONAL":
            continue
        cust = conn.execute("SELECT name FROM customers WHERE customer_id = ?",
                             (cid,)).fetchone()
        unmatched_count = conn.execute(
            "SELECT COUNT(*) FROM allocations a JOIN payments p "
            "ON p.payment_id = a.payment_id WHERE a.invoice_id IS NULL "
            "AND a.reason_code IN ('NO-MATCH', 'AMBIG-N') "
            "AND p.virtual_account = (SELECT virtual_account FROM customers "
            "WHERE customer_id = ?)", (cid,)).fetchone()[0]
        provisional_customers.append({
            "customer_id": cid,
            "name": cust["name"],
            "total_invoiced_paise": r.total_invoiced_paise,
            "open_balance_ledger_paise": r.open_balance_ledger_paise,
            "unmatched_cash_paise": r.unmatched_cash_paise,
            "balance_paise": r.balance_paise,
            "unmatched_payment_count": unmatched_count,
            "suggested_split": suggested_split(conn, cid),
        })
    conn.close()

    body = render_body(results, sample_allocations, totals, provisional_customers, holdout)

    full_page = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
{body}
</body>
</html>
'''
    report_dir = Path(__file__).parent
    (report_dir / "report.html").write_text(full_page, encoding="utf-8")
    (report_dir / "report_body.html").write_text(body, encoding="utf-8")
    print(f"wrote {report_dir / 'report.html'}")


if __name__ == "__main__":
    main()
