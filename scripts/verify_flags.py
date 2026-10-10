#!/usr/bin/env python3
"""Verify the sandbox-owned flag configuration (P0-03).

Static: deploy/flagd matches the pinned upstream flag names and is at baseline.
Runtime (--runtime): flagd reads deploy/flagd, flagd-ui mounts it read-only, and
an OFREP evaluation inside the sandbox network returns the file's value. With
--roundtrip it also flips paymentUnreachable on and back (sandbox only).
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from controller.executor.flag_store import FLAG_FILE, FlagStore, baseline_problems  # noqa: E402
from controller.safety.sandbox import load_spec  # noqa: E402

CURL_IMAGE = "curlimages/curl:8.10.1"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=60).stdout


def container(service, project):
    ids = docker("ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", f"label=com.docker.compose.service={service}").split()
    if len(ids) != 1:
        raise RuntimeError(f"expected one running {service} container, found {len(ids)}")
    return json.loads(docker("inspect", ids[0]))[0]


def check_mount(info, destination, want_rw):
    expected = FLAG_FILE.parent.resolve()
    for m in info["Mounts"]:
        if m["Destination"] == destination:
            if Path(m["Source"]).resolve() != expected:
                raise RuntimeError(f"{destination} is mounted from {m['Source']}, not {expected}")
            if want_rw is False and m["RW"]:
                raise RuntimeError(f"{destination} must be read-only")
            return
    raise RuntimeError(f"no mount at {destination}")


def evaluate(flag, network):
    out = docker("run", "--rm", "--network", network, CURL_IMAGE, "-s", "-X", "POST",
                 "-H", "Content-Type: application/json", "-d", '{"context":{}}',
                 f"http://flagd:8016/ofrep/v1/evaluate/flags/{flag}")
    return json.loads(out)


def wait_for(flag, network, expected, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = evaluate(flag, network)
        if result.get("value") == expected:
            return result
        time.sleep(1)
    raise RuntimeError(f"flagd did not report {flag}={expected!r} within {timeout}s: {result}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--roundtrip", action="store_true")
    args = parser.parse_args()

    problems = baseline_problems()
    if problems:
        print("Flag file is not at baseline:", *problems, sep="\n  ")
        return 1
    print(f"PASS static: {FLAG_FILE.relative_to(ROOT)} at baseline, names match pinned upstream")
    if not (args.runtime or args.roundtrip):
        return 0

    spec = load_spec()
    check_mount(container("flagd", spec["compose_project"]), "/etc/flagd", want_rw=None)
    check_mount(container("flagd-ui", spec["compose_project"]), "/app/data", want_rw=False)
    print("PASS mounts: flagd reads deploy/flagd; flagd-ui mounts it read-only")
    for flag in ("paymentFailure", "paymentUnreachable"):
        print(f"PASS runtime: {flag} = {wait_for(flag, spec['network'], {'paymentFailure': 0, 'paymentUnreachable': False}[flag])['value']!r}")

    if args.roundtrip:
        if not spec.get("approved"):
            print("Refusing --roundtrip: config/sandbox.yaml is not approved", file=sys.stderr)
            return 2
        store = FlagStore()
        prior = store.snapshot("paymentUnreachable")
        applied = store.set_variant("paymentUnreachable", "on", prior.sha256)
        try:
            wait_for("paymentUnreachable", spec["network"], True)
            print("PASS replacement visible in flagd: paymentUnreachable = True")
        finally:
            store.restore(prior, applied.sha256)
        wait_for("paymentUnreachable", spec["network"], False)
        print("PASS restored: paymentUnreachable = False")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Flag verification failed: {error}", file=sys.stderr)
        sys.exit(1)
