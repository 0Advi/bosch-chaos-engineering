#!/usr/bin/env python3
"""Results matrix of a campaign for slides: CSV, Markdown and a 1920px PNG.

Usage: python scripts/demo_matrix.py [--campaign campaign-...] [--out artifacts/demo-video]
Defaults to the most recent campaign in artifacts/demo-runs/.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
from pathlib import Path

RUNS = Path("artifacts/demo-runs")
LABELS = {"home": "Home", "catalog": "Product list", "product": "Product page", "recommendations": "Recommendations",
          "ads": "Ads", "image": "Images", "shipping": "Shipping quote", "cart_add": "Add to cart",
          "cart_get": "View cart", "checkout": "Checkout"}
COLUMNS = ["#", "AI-proposed injection", "Category", "Why the AI chose it", "Outcome", "Customer paths impacted",
           "Blast radius", "Path availability under fault", "Time to impact", "Time to recover",
           "Resilience score", "Top weakness found (AI)", "Top recommended fix (AI)"]


def load(name: str):
    path = RUNS / name
    return json.loads(path.read_text()) if path.exists() else None


def _top_rec(analysis: dict) -> str:
    recs = sorted((analysis.get("report") or {}).get("recommendations") or [], key=lambda r: r.get("priority", "P9"))
    return f"[{recs[0]['priority']}] {recs[0]['title']} ({recs[0]['component']})" if recs else "-"


def rows_for(campaign: dict) -> list[dict]:
    plan = {(e["scenario"], e["target"]): e for e in (load("plan-latest.json") or {}).get("experiments", [])}
    rows = []
    for n, rid in enumerate(campaign["runs"], 1):
        r = load(f"{rid}.json") or {}
        a = load(f"{rid}.analysis.json") or {}
        res, blast = r.get("resilience") or {}, r.get("blast_radius") or {}
        p = plan.get((r.get("scenario"), r.get("target")), {})
        impacted = blast.get("impacted_endpoints") or {}
        if not r.get("injected"):
            outcome = "Not injected: " + (r.get("abort_reason") or "").replace("health gate: ", "health gate - ")
        elif r.get("status") == "aborted":
            outcome = "Auto-stopped by guardrail: " + (r.get("abort_reason") or "").replace("guardrail: ", "")
        else:
            outcome = "Completed, fault restored"
        findings = (a.get("report") or {}).get("findings") or []
        fix = a.get("fix") or {}
        rows.append({
            "#": n,
            "AI-proposed injection": f"{r.get('title')} on {r.get('target')} ({r.get('variant')})",
            "Category": r.get("category", ""),
            "Why the AI chose it": p.get("reason", ""),
            "Outcome": outcome,
            "Customer paths impacted": (f"{len(impacted)}/10: " + ", ".join(LABELS.get(k, k) for k in impacted))
            if r.get("injected") else "-",
            "Blast radius": ("Contained" if blast.get("contained") else
                             "Spread to: " + ", ".join(LABELS.get(k, k) for k in blast.get("unexpected_endpoints", [])))
            if r.get("injected") else "-",
            "Path availability under fault": f"{res['parts']['availability']}%" if res else "-",
            "Time to impact": f"{r['time_to_first_failure_s']} s" if r.get("time_to_first_failure_s") is not None else "-",
            "Time to recover": f"{r['time_to_recover_s']} s" if r.get("time_to_recover_s") is not None else "-",
            "Resilience score": f"{res['score']}/100 ({res['grade']})" if res else "-",
            "Top weakness found (AI)": (f"[{findings[0]['severity']}] {findings[0]['title']}" if findings else "-"),
            "Top recommended fix (AI)": _top_rec(a),
        })
    return rows


def to_markdown(rows, overall) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in rows:
        lines.append("| " + " | ".join(str(r[c]).replace("|", "/").replace("\n", " ") for c in COLUMNS) + " |")
    return "\n".join(lines) + f"\n\n**Overall resilience score: {overall}/100**\n"


def to_html(rows, overall, campaign_id) -> str:
    def cell(c, v):
        cls = ""
        if c == "Outcome":
            cls = "ok" if v.startswith("Completed") else "warn"
        if c == "Blast radius":
            cls = "ok" if v == "Contained" else ("bad" if v.startswith("Spread") else "")
        return f'<td class="{cls}">{html.escape(str(v))}</td>'
    body = "".join("<tr>" + "".join(cell(c, r[c]) for c in COLUMNS) + "</tr>" for r in rows)
    head = "".join(f"<th>{html.escape(c)}</th>" for c in COLUMNS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  body {{ margin:0; background:#070707; color:#fff; font:15px/1.4 Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif; }}
  .wrap {{ padding:40px 44px; }}
  h1 {{ font:400 34px 'SF Mono', Menlo, monospace; letter-spacing:-.04em; margin:0 0 6px; }}
  p {{ color:#a7ada6; margin:0 0 22px; }}
  table {{ border-collapse:collapse; width:100%; background:#0b130c; border:1px solid rgba(255,255,255,.3); }}
  th {{ font:500 11.5px 'SF Mono', Menlo, monospace; text-transform:uppercase; letter-spacing:.06em; color:#c6f4b2;
       text-align:left; padding:12px 10px; border-bottom:1px solid rgba(255,255,255,.3); vertical-align:bottom; }}
  td {{ padding:12px 10px; border-top:1px solid rgba(255,255,255,.13); vertical-align:top; font-size:13.5px; }}
  td:first-child {{ font:400 20px 'SF Mono', Menlo, monospace; color:#a7ada6; }}
  td.ok {{ color:#8fe07b; }} td.warn {{ color:#f2a46b; }} td.bad {{ color:#ff6b57; }}
  .score {{ margin-top:18px; font:350 28px 'SF Mono', Menlo, monospace; }} .score b {{ color:#c6f4b2; font-weight:400; }}
</style></head><body><div class="wrap">
<h1>AI-proposed chaos experiments: results matrix</h1>
<p>OpenTelemetry Astronomy Shop (25 services) · {html.escape(campaign_id)} · ranked by the AI planner · measured live, fault auto-restored</p>
<table><tr>{head}</tr>{body}</table>
<div class="score">Overall resilience score: <b>{overall}/100</b></div></div></body></html>"""


async def screenshot(html_path: Path, png: Path):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        await page.goto(html_path.resolve().as_uri())
        await page.screenshot(path=str(png), full_page=True)
        await browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign")
    parser.add_argument("--out", default="artifacts/demo-video")
    args = parser.parse_args()
    cid = args.campaign or sorted(RUNS.glob("campaign-*.json"))[-1].stem
    campaign = load(f"{cid}.json")
    rows = rows_for(campaign)
    overall = ((campaign.get("report") or {}).get("overall_score"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "matrix.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (out / "matrix.md").write_text(to_markdown(rows, overall))
    (out / "matrix.html").write_text(to_html(rows, overall, cid))
    asyncio.run(screenshot(out / "matrix.html", out / "matrix.png"))
    print(to_markdown(rows, overall))
    print("saved", out / "matrix.csv", out / "matrix.md", out / "matrix.png")


if __name__ == "__main__":
    main()
