#!/usr/bin/env python3
"""Deploy and inspect the selected demo. No fault execution is exposed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "vendor" / "opentelemetry-demo"
REVISION = "e5552ee25bcede2900d89188f076c464cd73c074"
PROJECT = "bosch-chaos-sandbox"
EXPECTED = {
    "ad", "cart", "checkout", "currency", "email", "frontend",
    "frontend-proxy", "image-provider", "load-generator", "payment",
    "product-catalog", "quote", "recommendation", "shipping", "flagd",
    "flagd-ui", "telemetry-docs", "astronomy-db", "valkey-cart",
    "otel-collector", "jaeger", "grafana", "prometheus", "opensearch",
    "opamp-server",
}


def compose():
    if not (UPSTREAM / "compose.yaml").is_file():
        raise RuntimeError("Upstream source is missing. Run make bootstrap.")
    actual = subprocess.check_output(
        ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != REVISION:
        raise RuntimeError("Upstream revision differs from the reviewed source.")
    return [
        "docker", "compose", "--project-name", PROJECT,
        "--project-directory", str(UPSTREAM),
        "--env-file", str(UPSTREAM / ".env"),
        "--env-file", str(ROOT / "deploy" / "demo.env"),
        "-f", str(UPSTREAM / "compose.yaml"),
        "-f", str(UPSTREAM / "compose.observability.yaml"),
        "-f", str(ROOT / "deploy" / "compose.sandbox.yaml"),
    ]


def validate():
    resolved = json.loads(subprocess.check_output(
        compose() + ["config", "--format", "json"], text=True
    ))
    services = resolved["services"]
    if set(services) != EXPECTED:
        raise RuntimeError("Deployment does not contain the selected 25 services.")
    for name, service in services.items():
        if service.get("container_name"):
            raise RuntimeError(f"Fixed container name remains on {name}.")
        for port in service.get("ports", []):
            if port.get("host_ip") != "127.0.0.1":
                raise RuntimeError(f"Non-loopback port binding on {name}.")
        if name not in {"frontend-proxy", "prometheus"} and service.get("ports"):
            raise RuntimeError(f"Unexpected host ports exposed by {name}.")
        if service.get("privileged") or service.get("network_mode") == "host":
            raise RuntimeError(f"Unexpected host access on {name}.")
        if service.get("build"):
            if service.get("pull_policy") != "build":
                raise RuntimeError(f"Source build policy missing on {name}.")
            if not service["image"].startswith("bosch-local/demo:"):
                raise RuntimeError(f"Application image is not local for {name}.")
    if resolved["networks"]["default"]["name"] != PROJECT:
        raise RuntimeError("Sandbox network name differs from the selected project.")
    static = json.loads((ROOT / "artifacts" / "dependency_map.static.json").read_text())
    if {node["id"] for node in static["nodes"]} != EXPECTED:
        raise RuntimeError("Static service map does not match the deployment.")
    print("Validated: pinned upstream source, 25 services, project network,")
    print("local source builds, and loopback-only application/metrics ports.")
    print("Runtime isolation, app health, and safety behavior remain unverified.")


def host_port(key, default):
    if key in os.environ:
        value = os.environ[key]
    else:
        entries = dict(
            line.split("=", 1) for line in (ROOT / "deploy" / "demo.env").read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        value = entries.get(key, str(default))
    port = int(value)
    if not 1 <= port <= 65535:
        raise RuntimeError(f"Invalid host port for {key}.")
    return port


def smoke():
    app = f"http://127.0.0.1:{host_port('BOSCH_APP_PORT', 8080)}"
    metrics = f"http://127.0.0.1:{host_port('BOSCH_METRICS_PORT', 9090)}"
    for url in [app + "/", app + "/api/products",
                app + "/api/recommendations?currencyCode=USD", metrics + "/-/ready"]:
        with urlopen(url, timeout=10) as response:
            body = response.read()
            if "/api/" in url:
                data = json.loads(body)
                if not data:
                    raise RuntimeError(f"Application API returned empty data: {url}")
            print(f"PASS {response.status} {url}")
    print("Smoke checks passed. Cart/checkout, telemetry freshness, baseline,")
    print("threshold abort, and recovery still need separate verification.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "build", "start", "stop", "status", "smoke"])
    action = parser.parse_args().action
    try:
        if action == "smoke":
            smoke()
            return 0
        validate()
        args = {
            "build": ["build"],
            "start": ["up", "--build", "--detach", "--wait", "--wait-timeout", "300"],
            "stop": ["down"],
            "status": ["ps"],
        }
        if action in args:
            return subprocess.call(compose() + args[action])
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, URLError, subprocess.CalledProcessError) as error:
        print(f"Setup check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
