"""P0-01: positive and negative sandbox scope fixtures (no Docker needed)."""
import copy

import pytest

from controller.safety import sandbox

SPEC = sandbox.load_spec()


def make_snapshot(root):
    containers = [{
        "id": f"{name}-id", "name": f"bosch-chaos-sandbox-{name}-1",
        "project": SPEC["compose_project"], "service": name, "state": "running",
        "privileged": False, "network_mode": SPEC["network"], "networks": [SPEC["network"]],
        "mounts": [{"type": "bind", "source": str(root / "deploy" / "flagd"),
                    "destination": "/etc/flagd", "rw": False}] if name == "flagd" else [],
    } for name in SPEC["expected_services"]]
    return {"docker_context": "dedicated", "daemon_id": "DAEMON-1", "daemon_name": "lab",
            "containers": containers}


@pytest.fixture
def approved_spec():
    spec = copy.deepcopy(SPEC)
    spec.update(approved=True, docker_context="dedicated", daemon_id="DAEMON-1")
    return spec


def test_dedicated_sandbox_is_approved(tmp_path, approved_spec):
    report = sandbox.evaluate(make_snapshot(tmp_path), approved_spec, tmp_path)
    assert report.isolation_approved, report.errors
    assert sandbox.resolve_target(report, "payment") == "payment-id"


def test_checked_in_spec_is_never_approved(tmp_path):
    report = sandbox.evaluate(make_snapshot(tmp_path), SPEC, tmp_path)
    assert not report.isolation_approved
    assert any("not approved" in e for e in report.errors)
    with pytest.raises(PermissionError):
        sandbox.resolve_target(report, "payment")


@pytest.mark.parametrize("mutate, expected", [
    (lambda s: s.update(daemon_id="OTHER"), "daemon ID differs"),
    (lambda s: s.update(docker_context="desktop-linux"), "docker context"),
    (lambda s: s["containers"].append({**s["containers"][0], "id": "x", "project": "other",
                                       "name": "unrelated"}), "shared with unrelated"),
    (lambda s: s["containers"].append({**s["containers"][-1], "id": "dup"}), "exactly one container"),
    (lambda s: s["containers"][0].update(privileged=True), "privileged"),
    (lambda s: s["containers"][0].update(network_mode="host"), "host network"),
    (lambda s: s["containers"][0].update(networks=["bridge"]), "not attached"),
    (lambda s: s["containers"][0]["mounts"].append(
        {"type": "bind", "source": "/Users/me/.ssh", "destination": "/x", "rw": True}), "outside allowed"),
    (lambda s: s["containers"].pop(), "missing"),
])
def test_scope_violations_are_rejected(tmp_path, approved_spec, mutate, expected):
    snapshot = make_snapshot(tmp_path)
    mutate(snapshot)
    report = sandbox.evaluate(snapshot, approved_spec, tmp_path)
    assert not report.isolation_approved
    assert any(expected in e for e in report.errors), report.errors


def test_non_mutable_service_cannot_be_targeted(tmp_path, approved_spec):
    report = sandbox.evaluate(make_snapshot(tmp_path), approved_spec, tmp_path)
    with pytest.raises(PermissionError):
        sandbox.resolve_target(report, "frontend")


def test_collector_host_mounts_are_warnings_not_silent(tmp_path, approved_spec):
    snapshot = make_snapshot(tmp_path)
    collector = next(c for c in snapshot["containers"] if c["service"] == "otel-collector")
    collector["mounts"] = [{"type": "bind", "source": "/var/run/docker.sock",
                            "destination": "/var/run/docker.sock", "rw": False}]
    report = sandbox.evaluate(snapshot, approved_spec, tmp_path)
    assert report.isolation_approved
    assert any("docker.sock" in w for w in report.warnings)
