"""The P0 verification command must not expose an alternate fault injector."""
import sys

from scripts import verify_flags


def test_roundtrip_refused_before_docker_or_flag_access(monkeypatch, capsys):
    """The flag round-trip check is refused before touching Docker or the flag file."""
    monkeypatch.setattr(sys, "argv", ["verify_flags.py", "--roundtrip"])

    def forbidden(*args, **kwargs):
        """Fail if Docker or the flag file is touched."""
        raise AssertionError("roundtrip refusal must precede environment or flag access")

    monkeypatch.setattr(verify_flags, "docker", forbidden)
    monkeypatch.setattr(verify_flags, "baseline_problems", forbidden)
    assert verify_flags.main() == 2
    assert "P2 runner and watchdog" in capsys.readouterr().err
