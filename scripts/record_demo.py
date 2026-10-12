#!/usr/bin/env python3
"""Record the AI Chaos Lab demo: dashboard + live shop, driven end to end.

Two headless Chrome pages are recorded at 1440x900:
  dashboard.webm  the lab dashboard; a "director" asks the AI planner for a plan,
                  starts the top-N campaign and scrolls to whatever is happening
                  (live impact -> blast radius -> agent -> findings -> report)
  shop.webm       the real Astronomy Shop; a scripted customer browses, adds to
                  cart and places orders, so injected faults show up as a customer
                  would see them
events.json       time-stamped moments in both videos, used by scripts/edit_demo.py

Usage: python scripts/record_demo.py [--top 5] [--out artifacts/demo-video]
Requires: pip install playwright imageio-ffmpeg, Google Chrome, the sandbox and
the dashboard (python -m demo.server) running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright

LAB = "http://127.0.0.1:8099/"
SHOP = "http://127.0.0.1:8081/"
SIZE = {"width": 1440, "height": 900}
STATE_JS = """() => ({
  busy, job: status && status.job,
  phase: phaseClock ? phaseClock.name : null,
  run: live.run ? live.run.run_id : null, scenario: live.run ? live.run.title : null,
  target: live.run ? live.run.target : null, recordStatus: live.record ? live.record.status : null,
  analysed: findings && findings.analysis && findings.run ? findings.run.run_id : null,
  index: campaign ? campaign.index : null, report: !!(campaign && campaign.report && campaign.report.report),
  planRows: plan && plan.experiments ? plan.experiments.length : 0,
})"""


class Log:
    """Timestamped event log for the recordings, used later to cut the video."""
    def __init__(self):
        """Start with no events and no recordings."""
        self.events, self.starts = [], {}

    def start(self, video: str):
        """Mark when a video recording started."""
        self.starts[video] = time.monotonic()

    def add(self, video: str, kind: str, **data):
        """Log an event at the current time in a video and print it."""
        t = round(time.monotonic() - self.starts[video], 2)
        self.events.append({"video": video, "t": t, "kind": kind, **data})
        print(f"[{video} {t:7.1f}s] {kind} {json.dumps(data)[:160]}", flush=True)


async def scroll_to(page, selector: str, block: str = "start"):
    """Smoothly scroll an element into view."""
    await page.evaluate(f"document.querySelector('{selector}')?.scrollIntoView({{behavior:'smooth', block:'{block}'}})")


async def director(page, log: Log, top: int, stop: asyncio.Event, ctx: dict):
    """Drive the dashboard: plan with the AI, run the top experiments as a campaign and show the
    results.
    """
    await page.goto(LAB)
    await page.wait_for_function("() => typeof status !== 'undefined' && status && status.health", timeout=60000)
    log.add("dashboard", "hero")
    await page.wait_for_timeout(6000)

    await scroll_to(page, "#plan")
    await page.wait_for_timeout(1500)
    log.add("dashboard", "plan_click")
    await page.click("#plan-btn")
    await page.wait_for_function("() => !busy && plan && plan.experiments && plan.experiments.length", timeout=240000)
    plan = await page.evaluate("plan")
    ctx["plan"] = plan
    log.add("dashboard", "plan_ready", experiments=[(e["scenario"], e["target"], e["variant"]) for e in plan["experiments"]])
    await page.wait_for_timeout(7000)      # let the viewer read the ranked plan

    await page.evaluate(f"""() => {{ const b = document.querySelector('#campaign-btn');
        b.textContent = 'Run top {top} as campaign'; }}""")
    log.add("dashboard", "campaign_click")
    await page.click("#campaign-btn")

    last = {"run": None, "phase": None, "analysed": None}
    showcase_until = 0.0
    deadline = time.monotonic() + 60 * 45
    while time.monotonic() < deadline:
        s = await page.evaluate(STATE_JS)
        ctx["state"] = s
        now = time.monotonic()
        if s["run"] and s["run"] != last["run"]:
            last.update(run=s["run"], phase=None)
            log.add("dashboard", "run_start", run=s["run"], scenario=s["scenario"], target=s["target"], index=s["index"])
            await scroll_to(page, "#experiment")
            showcase_until = now + 5
        if s["phase"] != last["phase"] and now >= showcase_until:
            last["phase"] = s["phase"]
            if s["phase"]:
                log.add("dashboard", "phase", phase=s["phase"], run=s["run"])
            if s["phase"] in ("baseline", "inject", "fault"):
                await scroll_to(page, "#live")
            elif s["phase"] in ("restore", "recovered"):
                await scroll_to(page, "#live")
            elif s["phase"] == "evidence":
                await scroll_to(page, "#graph")
        if s["recordStatus"] and last.get("evidence_shown") != s["run"]:
            last["evidence_shown"] = s["run"]
            log.add("dashboard", "evidence_ready", run=s["run"], status=s["recordStatus"])
            await scroll_to(page, "#graph")
            await page.wait_for_timeout(5000)
            await scroll_to(page, "#evidence-sec")
        if s["analysed"] and s["analysed"] != last["analysed"]:
            last["analysed"] = s["analysed"]
            log.add("dashboard", "analysis_ready", run=s["analysed"])
            await scroll_to(page, "#findings")
            await page.wait_for_timeout(3000)
            for _ in range(6):                     # slow pan through weaknesses, recs and the code fix
                await page.mouse.wheel(0, 320)
                await page.wait_for_timeout(1800)
            log.add("dashboard", "fix_shown", run=s["analysed"])
        if s["report"]:
            log.add("dashboard", "campaign_report")
            await scroll_to(page, "#campaign")
            await page.wait_for_timeout(4000)
            for _ in range(4):
                await page.mouse.wheel(0, 300)
                await page.wait_for_timeout(2000)
            await page.wait_for_timeout(3000)
            break
        if not s["busy"] and s["job"] is None and last["run"] and not s["report"]:
            await page.wait_for_timeout(5000)
            s = await page.evaluate(STATE_JS)
            if not s["busy"] and not s["report"]:
                log.add("dashboard", "campaign_ended_without_report")
                break
        await page.wait_for_timeout(1000)
    stop.set()


async def shopper(page, log: Log, stop: asyncio.Event, ctx: dict):
    """Keep using the shop (browse, add to cart, check out) during the recording and log what
    happens.
    """
    i = 0
    while not stop.is_set():
        scenario = (ctx.get("state") or {}).get("scenario")
        phase = (ctx.get("state") or {}).get("phase")
        tag = {"scenario": scenario, "phase": phase}
        try:
            t0 = time.monotonic()
            await page.goto(SHOP, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_selector('[data-cy="product-card"]', timeout=15000)
            log.add("shop", "home_loaded", seconds=round(time.monotonic() - t0, 1), **tag)
            await page.wait_for_timeout(1500)
            await page.mouse.wheel(0, 500)
            await page.wait_for_timeout(1200)
            cards = page.locator('[data-cy="product-card"]')
            await cards.nth(i % max(1, await cards.count())).click()
            t0 = time.monotonic()
            await page.wait_for_selector('[data-cy="product-add-to-cart"]', timeout=15000)
            log.add("shop", "product_loaded", seconds=round(time.monotonic() - t0, 1), **tag)
            await page.wait_for_timeout(1500)
            await page.click('[data-cy="product-add-to-cart"]')
            t0 = time.monotonic()
            await page.wait_for_url("**/cart**", timeout=15000)
            await page.wait_for_selector('[data-cy="checkout-place-order"]', timeout=15000)
            log.add("shop", "cart_loaded", seconds=round(time.monotonic() - t0, 1), **tag)
            await page.wait_for_timeout(1500)
            await page.click('[data-cy="checkout-place-order"]')
            t0 = time.monotonic()
            # On failure the shop UI shows nothing: the order call throws and the page stays on /cart.
            outcome = "checkout silently failed (stayed on cart, no message)"
            for _ in range(30):
                if "/cart/checkout/" in page.url or "Your order is complete" in await page.inner_text("body"):
                    outcome = "order complete"
                    break
                await page.wait_for_timeout(500)
            log.add("shop", "checkout_result", outcome=outcome, seconds=round(time.monotonic() - t0, 1), **tag)
            await page.wait_for_timeout(6000)   # gentle pace: keep the recording's own load low
        except Exception as error:
            log.add("shop", "step_failed", error=type(error).__name__, detail=str(error).splitlines()[0][:120], **tag)
            await page.wait_for_timeout(2500)
        i += 1


async def main():
    """Record the dashboard and shop side by side while running the top planned experiments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--out", default="artifacts/demo-video")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log, stop, ctx = Log(), asyncio.Event(), {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        lab_ctx = await browser.new_context(viewport=SIZE, record_video_dir=str(out / "raw-dashboard"), record_video_size=SIZE)
        shop_ctx = await browser.new_context(viewport=SIZE, record_video_dir=str(out / "raw-shop"), record_video_size=SIZE)
        lab = await lab_ctx.new_page()
        log.start("dashboard")
        shop = await shop_ctx.new_page()
        log.start("shop")
        try:
            await asyncio.gather(director(lab, log, args.top, stop, ctx), shopper(shop, log, stop, ctx))
        finally:
            stop.set()
            (out / "events.json").write_text(json.dumps({"events": log.events, "plan": ctx.get("plan")}, indent=1))
            await lab_ctx.close()
            await shop_ctx.close()
            await browser.close()
            for name, page in (("dashboard", lab), ("shop", shop)):
                src = Path(await page.video.path())
                src.rename(out / f"{name}.webm")
            print("saved", out / "dashboard.webm", out / "shop.webm", out / "events.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
