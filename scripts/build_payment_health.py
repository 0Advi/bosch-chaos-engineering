"""Build the payment liveness probe for this Docker daemon's architecture."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build():
    """Cross-compile the payment health-check helper with Go for the Docker host's CPU architecture."""
    go = shutil.which("go") or "/usr/local/go/bin/go"
    architecture = subprocess.check_output(
        ["docker", "info", "--format", "{{.Architecture}}"], text=True).strip()
    architectures = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    if architecture not in architectures:
        raise RuntimeError(f"Unsupported Docker architecture: {architecture}")
    destination = ROOT / "deploy" / "health-bin" / "payment-health"
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-trimpath", "-o", str(destination),
                    str(ROOT / "deploy" / "payment_health.go")], check=True,
                   env={**os.environ, "CGO_ENABLED": "0", "GOOS": "linux",
                        "GOARCH": architectures[architecture]})
    destination.chmod(0o755)
    return destination


if __name__ == "__main__":
    print(build())
