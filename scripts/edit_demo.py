#!/usr/bin/env python3
"""Cut the recorded demo (scripts/record_demo.py) into a <= 3-minute 1080p video.

Story: intro -> AI plan -> for each experiment: title, lab + live shop side by side
while the fault is injected, blast radius, AI findings and code fix -> resilience report.
Captions are burned in. Output: <out>/demo.mp4 (+ the segment list in edit.json).

Usage: python scripts/edit_demo.py [--out artifacts/demo-video] [--max 178]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
RUNS = Path("artifacts/demo-runs")
MONO = "/System/Library/Fonts/SFNSMono.ttf"
SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"
BG = "0x070707"
SHOP_LAG = 0.6  # the shop page starts recording ~0.6 s after the dashboard page


def caption_filter(tmp: Path, name: str, text: str, title: str | None = None) -> str:
    parts = []
    if title:
        (tmp / f"{name}.title.txt").write_text(title)
        parts.append(f"drawtext=expansion=none:textfile='{tmp / (name + '.title.txt')}':fontfile='{MONO}':fontsize=40:"
                     f"fontcolor=0xc6f4b2:x=60:y=34")
    (tmp / f"{name}.txt").write_text(text)
    parts.append(f"drawtext=expansion=none:textfile='{tmp / (name + '.txt')}':fontfile='{SANS}':fontsize=32:fontcolor=white:"
                 f"box=1:boxcolor=0x0b130c@0.92:boxborderw=20:x=(w-text_w)/2:y=h-text_h-46:line_spacing=10")
    return ",".join(parts)


def render(tmp: Path, seg: dict, out: Path):
    speed = seg.get("speed", 1.0)
    cap = caption_filter(tmp, seg["name"], seg["caption"], seg.get("title"))
    if seg["kind"] == "full":
        args = ["-ss", f"{seg['start']:.2f}", "-t", f"{seg['dur']:.2f}", "-i", seg["src"]]
        vf = (f"[0:v]setpts=PTS/{speed},scale=1536:960,pad=1920:1080:192:112:color={BG},{cap}[v]")
    elif seg["kind"] == "still":
        args = ["-loop", "1", "-t", f"{seg['dur']:.2f}", "-i", seg["src"]]
        vf = (f"[0:v]scale=1840:900:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:120:color={BG},"
              f"{cap}[v]")
    else:  # side by side: dashboard | shop
        args = ["-ss", f"{seg['start']:.2f}", "-t", f"{seg['dur']:.2f}", "-i", seg["src"],
                "-ss", f"{max(0, seg['start'] - SHOP_LAG):.2f}", "-t", f"{seg['dur']:.2f}", "-i", seg["src2"]]
        labels = ""
        for i, label in enumerate(("AI CHAOS LAB  (live impact)", "REAL SHOP  (customer view)")):
            (tmp / f"{seg['name']}.l{i}.txt").write_text(label)
            labels += (f",drawtext=expansion=none:textfile='{tmp / (seg['name'] + f'.l{i}.txt')}':fontfile='{MONO}':fontsize=26:"
                       f"fontcolor=0xa7ada6:x={30 + i * 960}:y=150")
        if seg.get("timer"):  # real elapsed seconds since the customer's click, on the shop side
            labels += (f",drawtext=fontfile='{MONO}':fontsize=40:fontcolor=0xff6b57:box=1:boxcolor=0x070707@0.85:"
                       f"boxborderw=14:x=1000:y=820:text='{seg['timer']}\\: %{{eif\\:t*{speed:.3f}\\:d}} s'")
        vf = (f"[0:v]setpts=PTS/{speed},scale=950:594[a];[1:v]setpts=PTS/{speed},scale=950:594[b];"
              f"[a][b]hstack=inputs=2,pad=1920:1080:5:195:color={BG}{labels},{cap}[v]")
    cmd = [FFMPEG, "-y", "-loglevel", "error", *args, "-filter_complex", vf, "-map", "[v]", "-r", "30",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-an", str(out)]
    subprocess.run(cmd, check=True)


def first(events, kind, run=None, video="dashboard", after=0.0, **match):
    for e in events:
        if e["video"] == video and e["kind"] == kind and e["t"] >= after and (run is None or e.get("run") == run) \
                and all(e.get(k) == v for k, v in match.items()):
            return e
    return None


def short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def customer_moment(ev: list, inj_t: float, rec: dict, run_t: float) -> dict | None:
    """The moment in the shop video where the injected fault hit the scripted customer, held on screen
    (~3 s) next to the lab. Uses only what the recording actually captured; returns dashboard-time
    start, duration and speed, plus an honest caption."""
    tl = rec.get("timeline") or {}
    restore_t = run_t + tl.get("restore", 0) - tl.get("baseline", 0)
    shop = [e for e in ev if e["video"] == "shop" and inj_t - 3 <= e["t"] <= restore_t + 20]
    for e in shop:  # 1. an order that silently failed
        if e["kind"] == "checkout_result" and "silently failed" in e.get("outcome", ""):
            click = e["t"] - e["seconds"] - 0.6
            dur = e["seconds"] + 1.2
            return {"start": click + SHOP_LAG, "dur": dur, "speed": dur / 3.2, "timer": "Order pending",
                    "caption": f"Customer clicks Place order... still on the cart {e['seconds']:.0f} s later, "
                               "no message: the order silently failed"}
    for i, e in enumerate(shop):  # 2. a customer step that hung until the browser gave up
        if e["kind"] == "step_failed":
            prev = next((p for p in reversed(shop[:i]) if p["kind"] in ("product_loaded", "cart_loaded")), None)
            if prev:
                click = prev["t"] + 1.3
                dur = e["t"] - click + 0.6
                action = "Add to cart" if prev["kind"] == "product_loaded" else "Place order"
                return {"start": click + SHOP_LAG, "dur": dur, "speed": dur / 3.2, "timer": f"{action} pending",
                        "caption": f"Customer clicks {action}... nothing happens for {dur - 0.6:.0f} s: "
                                   "the request hangs until it times out"}
    slow = [e for e in shop if e["kind"] in ("home_loaded", "cart_loaded") and e.get("phase") in ("inject", "fault")]
    if slow:  # 3. no visible failure: show the slowest real page load, honestly captioned
        e = max(slow, key=lambda x: x.get("seconds") or 0)
        start = e["t"] - (e.get("seconds") or 0) - 0.4
        dur = (e.get("seconds") or 0) + 1.0
        page = "home page" if e["kind"] == "home_loaded" else "cart page"
        return {"start": start + SHOP_LAG, "dur": dur, "speed": max(1.0, dur / 3.0),
                "caption": f"Customer view: no visible error, the fault stayed contained; slowest {page} "
                           f"load {e['seconds']:.1f} s"}
    return None


def plan_segments(out: Path) -> list[dict]:
    ev = json.loads((out / "events.json").read_text())["events"]
    dash, shop = str(out / "dashboard.webm"), str(out / "shop.webm")
    segs = []
    hero = first(ev, "hero")
    segs.append({"name": "intro", "kind": "full", "src": dash, "start": hero["t"], "dur": 6, "speed": 1,
                 "title": "AI Chaos Lab",
                 "caption": "AI-driven chaos engineering on a live 25-service microservice shop"})
    pc, pr = first(ev, "plan_click"), first(ev, "plan_ready")
    segs.append({"name": "plan", "kind": "full", "src": dash, "start": pc["t"] - 0.5, "dur": pr["t"] - pc["t"] + 7,
                 "target": 10, "title": "1  AI planner",
                 "caption": "Gemini agent ranks experiments from the dependency map, with a reason for each"})
    runs = [e for e in ev if e["video"] == "dashboard" and e["kind"] == "run_start"]
    for n, rs in enumerate(runs, 1):
        rid = rs["run"]
        rec = json.loads((RUNS / f"{rid}.json").read_text()) if (RUNS / f"{rid}.json").exists() else {}
        ana_path = RUNS / f"{rid}.analysis.json"
        ana = json.loads(ana_path.read_text()) if ana_path.exists() else {}
        title = f"{n + 1}  Experiment {n}/{len(runs)}: {rs['scenario']} on {rs['target']}"
        segs.append({"name": f"r{n}a", "kind": "full", "src": dash, "start": rs["t"], "dur": 3.5, "speed": 1, "title": title,
                     "caption": "Hypothesis: " + short(rec.get("hypothesis"), 120)})
        # Injection time: from the director's log, else from the run's own phase timeline
        # (seconds since the run started, which is when the dashboard saw run_start).
        inj = first(ev, "phase", rid, phase="inject")
        tl = rec.get("timeline") or {}
        inj_t = inj["t"] if inj else (rs["t"] + tl["inject"] - tl.get("baseline", 0) if "inject" in tl else None)
        if inj_t is not None:
            inj_desc = rec.get("injection") or ""
            segs.append({"name": f"r{n}b", "kind": "side", "src": dash, "src2": shop, "start": inj_t - 1, "dur": 15,
                         "target": 10, "title": title,
                         "caption": f"Fault injected ({short(inj_desc, 60)}): live impact in the lab and in the real shop"})
            hold = customer_moment(ev, inj_t, rec, rs["t"])
            if hold:
                segs.append({"name": f"r{n}h", "kind": "side", "src": dash, "src2": shop, "title": title, **hold})
        res = rec.get("resilience") or {}
        blast = rec.get("blast_radius") or {}
        evd = first(ev, "evidence_ready", rid)
        if evd:
            impacted = ", ".join(blast.get("impacted_endpoints", {}).keys()) or "none"
            status = (f"Stopped safely by guardrail: {short(rec.get('abort_reason'), 70)}"
                      if rec.get("status") == "aborted" else
                      f"Recovered in {rec.get('time_to_recover_s')} s · resilience score {res.get('score')}/100")
            segs.append({"name": f"r{n}c", "kind": "full", "src": dash, "start": evd["t"] + 0.5, "dur": 4.5, "speed": 1,
                         "title": title, "caption": f"Blast radius: {short(impacted, 70)}\n{status}"})
        an, fx = first(ev, "analysis_ready", rid), first(ev, "fix_shown", rid)
        if an and fx:
            fix = ana.get("fix") or {}
            head = short((ana.get("report") or {}).get("headline"), 90)
            segs.append({"name": f"r{n}d", "kind": "full", "src": dash, "start": an["t"], "dur": fx["t"] - an["t"] + 1,
                         "target": 8, "title": title,
                         "caption": f"AI analyst: {head}\nCode fix proposed and syntax-checked: {fix.get('file', '-')}"})
    rep = first(ev, "campaign_report")
    if rep:
        # The ranked-weakness table is on screen while the director pans past the last run's findings,
        # a few seconds before the campaign_report marker; end the last run's clip there and reuse it.
        last_fix = [e for e in ev if e["video"] == "dashboard" and e["kind"] == "fix_shown"][-1]["t"]
        if segs[-1]["name"].endswith("d"):
            segs[-1]["dur"] = max(4, last_fix - 7 - segs[-1]["start"])
        segs.append({"name": "outro", "kind": "full", "src": dash, "start": last_fix - 6.5, "dur": 7, "speed": 1,
                     "title": f"{len(runs) + 2}  Resilience report",
                     "caption": "AI campaign report: weaknesses ranked by severity across all experiments, with fixes"})
    matrix = out / "matrix.png"
    if matrix.exists():
        segs.append({"name": "matrix", "kind": "still", "src": str(matrix), "dur": 9, "speed": 1,
                     "title": "Results", "caption": "5 AI-proposed experiments, measured live and auto-restored"})
    return segs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="artifacts/demo-video")
    parser.add_argument("--max", type=float, default=178)
    args = parser.parse_args()
    out = Path(args.out)
    segs = plan_segments(out)
    for s in segs:
        if "target" in s and s["kind"] != "still":
            s["speed"] = max(1.0, s["dur"] / s["target"])
    total = sum(s["dur"] / s["speed"] for s in segs)
    if total > args.max:  # speed everything up evenly to fit the limit
        k = total / args.max
        for s in segs:
            s["speed"] *= k
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        parts = []
        for s in segs:
            path = tmp / f"{s['name']}.mp4"
            render(tmp, s, path)
            parts.append(path)
            print(f"{s['name']:6} {s['kind']:4} {s['dur']:5.1f}s @x{s['speed']:.2f} -> {s['dur'] / s['speed']:4.1f}s", flush=True)
        (tmp / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                        "-c", "copy", str(out / "demo.mp4")], check=True)
    (out / "edit.json").write_text(json.dumps(segs, indent=1))
    print(f"demo.mp4: {sum(s['dur'] / s['speed'] for s in segs):.0f} s", flush=True)


if __name__ == "__main__":
    main()
