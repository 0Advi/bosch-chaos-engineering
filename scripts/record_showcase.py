#!/usr/bin/env python3
"""Record a slow, real-time showcase: a few faults injected from the dashboard while a customer
uses the real shop, both recorded side by side (headless Chrome via Playwright).

For each scenario: select it on the dashboard (shows "See it in the shop"), show the healthy shop,
run it, wait for FAULT LIVE, reload / act in the shop so the breakage is on screen, hold it, press
"End fault now", show the shop recovering, then the dashboard's blast radius and score.

Usage: python scripts/record_showcase.py [--out artifacts/showcase-video] [--fault 35]
Then:  python scripts/edit_showcase.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright

LAB = "http://127.0.0.1:8099/"
SHOP = "http://localhost:8081"
SIZE = {"width": 1440, "height": 900}

# A restarted product-catalog stays sluggish for a while, so its crash goes last.
SCENARIOS = [
    {"sid": "payment-failure", "target": "payment", "variant": "100%", "kind": "order"},
    {"sid": "image-slow-load", "target": "image-provider", "variant": "10sec", "kind": "browse"},
    {"sid": "service-crash", "target": "product-catalog", "variant": "kill", "kind": "browse"},
]


async def settle(log: Log, n: int):
    """Wait until the catalog answers fast three times in a row (the shop has fully recovered)."""
    from urllib.request import urlopen
    good, deadline = 0, time.monotonic() + 120
    while good < 3 and time.monotonic() < deadline:
        started = time.monotonic()
        try:
            await asyncio.to_thread(lambda: urlopen(SHOP + "/api/products?currencyCode=USD", timeout=10).read())
            good = good + 1 if time.monotonic() - started < 0.5 else 0
        except Exception:
            good = 0
        await asyncio.sleep(1)
    log.add("dashboard", "settled", n=n, ok=good >= 3)


class Log:
    """Wall-clock event log for the showcase recordings."""
    def __init__(self):
        """Start with no events and no recordings."""
        self.events, self.starts = [], {}

    def start(self, video: str):
        """Mark when a video recording started."""
        self.starts[video] = time.time()

    def add(self, video: str, kind: str, **data):
        """Log an event at the current time in a video and print it."""
        t = round(time.time() - self.starts[video], 2)
        self.events.append({"video": video, "t": t, "kind": kind, **data})
        print(f"[{video:9} {t:7.1f}s] {kind} {json.dumps(data)[:150]}", flush=True)


async def scroll(page, selector: str):
    """Smoothly scroll an element to the top of the view."""
    await page.evaluate(f"document.querySelector('{selector}')?.scrollIntoView({{behavior:'smooth', block:'start'}})")


async def show_products(shop):
    """Bring the Hot Products grid into view (that is where catalog and image faults show)."""
    await shop.evaluate("""() => { const t = [...document.querySelectorAll('h1,h2,h3')]
        .find((h) => /Hot Products/i.test(h.textContent)); if (t) window.scrollTo({top: t.getBoundingClientRect().top
        + window.scrollY - 40, behavior: 'smooth'}); }""")


async def phase(lab) -> str | None:
    """The dashboard's current experiment phase (None when idle)."""
    return await lab.evaluate("phaseClock ? phaseClock.name : null")


async def wait_for(lab, predicate: str, timeout: float) -> bool:
    """Poll a JavaScript predicate in the page until true or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await lab.evaluate(predicate):
            return True
        await asyncio.sleep(0.5)
    return False


async def place_order(shop, log: Log, label: str, n: int) -> str:
    """Click Place order in the shop and wait (up to 18 s) for the result, logging success or
    failure.
    """
    await shop.locator('[data-cy="checkout-place-order"]').scroll_into_view_if_needed()
    await shop.wait_for_timeout(800)
    log.add("shop", "place_order_click", label=label, n=n)
    await shop.click('[data-cy="checkout-place-order"]')
    for _ in range(36):                       # up to 18 s
        if "/cart/checkout/" in shop.url:
            log.add("shop", "order_result", outcome="order complete", label=label, n=n)
            return "complete"
        await shop.wait_for_timeout(500)
    log.add("shop", "order_result", outcome="no confirmation (silently failed)", label=label, n=n)
    return "failed"


async def scenario(lab, shop, log: Log, n: int, spec: dict, fault_s: int):
    """Record one scenario: select it, start it, show the shop failing during the fault, then
    recovery and the AI analysis.
    """
    sid, target = spec["sid"], spec["target"]
    # 1. Select on the dashboard; the "See it in the shop" panel explains where to look.
    await lab.evaluate(f"""async () => {{ pickScenario('{sid}', '{target}', '{spec['variant']}');
        await new Promise((r) => setTimeout(r, 1200));
        document.querySelector('#presenter').checked = true; applyPresenterDefaults(); renderShopView();
        document.querySelector('#variant').value = '{spec['variant']}';
        document.querySelector('#fault_s').value = {fault_s}; document.querySelector('#baseline_s').value = 6; }}""")
    await scroll(lab, "#experiment")
    view = await lab.evaluate("shopView")
    log.add("dashboard", "select", n=n, sid=sid, target=target, expect=view.get("expect"), path=view.get("path"))

    # 2. Healthy shop.
    if spec["kind"] == "order":
        await shop.goto(SHOP + "/product/66VCHSJNUP")
        await shop.wait_for_selector('[data-cy="product-add-to-cart"]', timeout=30000)
        await shop.wait_for_timeout(1500)
        await shop.click('[data-cy="product-add-to-cart"]')
        await shop.wait_for_url("**/cart**", timeout=30000)
        await shop.wait_for_selector('[data-cy="checkout-place-order"]', timeout=30000)
        await shop.locator('[data-cy="checkout-place-order"]').scroll_into_view_if_needed()
    else:
        await shop.goto(SHOP + "/")
        await shop.wait_for_selector('[data-cy="product-card"]', timeout=30000)
        await show_products(shop)
    log.add("shop", "healthy", n=n)
    await asyncio.sleep(4)

    # 3. Run (with the AI analyst afterwards).
    log.add("dashboard", "run", n=n)
    await lab.evaluate("startJob('/api/run', designerParams())")
    if not await wait_for(lab, "phaseClock && phaseClock.name === 'fault'", 90):
        rec = await lab.evaluate("live.record ? {status: live.record.status, reason: live.record.abort_reason} : null")
        log.add("dashboard", "no_fault", n=n, record=rec)
        await wait_for(lab, "!busy", 120)
        return
    log.add("dashboard", "fault_live", n=n)
    fault_at = time.monotonic()

    # 4. The customer hits the fault.
    if spec["kind"] == "order":
        await place_order(shop, log, "during fault", n)
    else:
        await shop.goto(SHOP + "/")
        log.add("shop", "reload_during_fault", n=n)
        await shop.wait_for_timeout(2500)
        await show_products(shop)
    await asyncio.sleep(5)
    await scroll(lab, "#live")                 # live path matrix lighting up
    while time.monotonic() - fault_at < fault_s - 8:
        await asyncio.sleep(1)

    # 5. End the fault from the dashboard (unless a guardrail already stopped it); the customer retries.
    await scroll(lab, "#experiment")
    await asyncio.sleep(1.5)
    if await lab.evaluate("!document.querySelector('#end-fault').disabled"):
        log.add("dashboard", "end_fault", n=n)
        await lab.click("#end-fault")
    else:
        log.add("dashboard", "auto_stopped", n=n, reason=await lab.evaluate("live.guard"))
    await wait_for(lab, "phaseClock && ['restore','recovered','evidence'].includes(phaseClock.name)", 40)
    await asyncio.sleep(2)
    if spec["kind"] == "order":
        await shop.goto(SHOP + "/cart")
        await shop.wait_for_selector('[data-cy="checkout-place-order"]', timeout=30000)
        await place_order(shop, log, "after restore", n)
    else:
        await shop.goto(SHOP + "/")
        await shop.wait_for_selector('[data-cy="product-card"]', timeout=40000)
        await shop.wait_for_timeout(1500)
        await show_products(shop)
    log.add("shop", "recovered", n=n)
    await asyncio.sleep(4)

    # 6. Dashboard results: blast radius and score.
    await wait_for(lab, "live.record && live.record.run_id === live.run.run_id", 120)
    await scroll(lab, "#live")
    log.add("dashboard", "results", n=n)
    await asyncio.sleep(4)
    await scroll(lab, "#graph")
    log.add("dashboard", "blast", n=n)
    await asyncio.sleep(5)
    await scroll(lab, "#evidence-sec")         # traces, error spans, logs + the AI agent working
    log.add("dashboard", "evidence", n=n)
    await asyncio.sleep(5)

    # 7. AI analysis: weaknesses, recommendations, validated code fix.
    if await wait_for(lab, "findings && findings.analysis && findings.run && findings.run.run_id === live.record.run_id", 180):
        await scroll(lab, "#findings")
        log.add("dashboard", "analysis", n=n)
        await asyncio.sleep(3)
        for _ in range(5):
            await lab.mouse.wheel(0, 320)
            await asyncio.sleep(1.6)
        log.add("dashboard", "analysis_end", n=n)
    await wait_for(lab, "!busy", 120)
    rec = await lab.evaluate("({run: live.record.run_id, status: live.record.status, score: live.record.resilience && "
                             "live.record.resilience.score, ttr: live.record.time_to_recover_s})")
    log.add("dashboard", "done", n=n, **rec)


async def main():
    """Record the dashboard and shop for the three showcase scenarios."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="artifacts/showcase-video")
    parser.add_argument("--fault", type=int, default=35)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = Log()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        lab_ctx = await browser.new_context(viewport=SIZE, record_video_dir=str(out / "raw-dashboard"), record_video_size=SIZE)
        shop_ctx = await browser.new_context(viewport=SIZE, record_video_dir=str(out / "raw-shop"), record_video_size=SIZE)
        lab = await lab_ctx.new_page()
        log.start("dashboard")
        shop = await shop_ctx.new_page()
        log.start("shop")
        try:
            await lab.goto(LAB)
            await lab.wait_for_function("() => typeof status !== 'undefined' && status && status.health", timeout=60000)
            await shop.goto(SHOP + "/")
            log.add("dashboard", "intro")          # hero + header chips: our own sandbox, all green
            await asyncio.sleep(7)
            for n, spec in enumerate(SCENARIOS, 1):
                await settle(log, n)
                try:
                    await scenario(lab, shop, log, n, spec, args.fault)
                except Exception as error:   # keep recording the other scenarios
                    log.add("dashboard", "scenario_error", n=n, error=f"{type(error).__name__}: {str(error)[:120]}")
                    await wait_for(lab, "!busy", 180)
                await asyncio.sleep(6)
            # Safety controls: guardrails, health gate, journal/restore, kill switch.
            await scroll(lab, "#experiment")
            await lab.evaluate("document.querySelector('.guards-edit').open = true")
            log.add("dashboard", "safety")
            await asyncio.sleep(9)
        finally:
            (out / "events.json").write_text(json.dumps({"events": log.events, "starts": log.starts}, indent=1))
            await lab_ctx.close()
            await shop_ctx.close()
            await browser.close()
            for name, page in (("dashboard", lab), ("shop", shop)):
                Path(await page.video.path()).rename(out / f"{name}.webm")
            print("saved", out, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
