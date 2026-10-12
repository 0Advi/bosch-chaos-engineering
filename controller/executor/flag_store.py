"""Sandbox-owned flagd configuration (P0-03).

deploy/flagd/ is mounted into flagd (read by it) and flagd-ui (read-only), so
this module is the only writer. Every write is a whole-file atomic replace in
the same directory, guarded by the hash of the content we expect to replace.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FLAG_FILE = ROOT / "deploy" / "flagd" / "demo.flagd.json"
UPSTREAM_FLAG_FILE = ROOT / "vendor" / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
# Flags the runner may change. Everything else is read-only to the controller.
WRITABLE_FLAGS = frozenset({"paymentFailure", "paymentUnreachable"})


class FlagConflict(RuntimeError):
    """The file changed since it was read; never overwrite external edits."""


@dataclass(frozen=True)
class FlagSnapshot:
    """One flag's state at a moment: file hash, default variant, value and enabled state."""
    sha256: str
    flag: str
    default_variant: str
    value: object
    state: str = "ENABLED"


@dataclass(frozen=True)
class FlagChange:
    """A prepared single-flag change with the exact bytes needed to undo it (P2-02).

    It is persisted in the recovery journal before the file is written, so a
    watchdog can restore the exact prior file even if the controller dies
    between writing and recording the write.
    """
    flag: str
    prior_variant: str
    prior_value: object
    variant: str
    value: object
    prior_sha256: str
    applied_sha256: str
    prior_text: str
    applied_text: str

    def as_journal(self) -> dict:
        """The change as a journal entry (so a watchdog can restore the exact prior bytes)."""
        return {"kind": "flag", **self.__dict__}

    @classmethod
    def from_journal(cls, data: dict) -> "FlagChange":
        """Rebuild a change from its journal entry."""
        return cls(**{k: v for k, v in data.items() if k != "kind"})


def _sha(data: bytes) -> str:
    """SHA-256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


class FlagStore:
    """The only writer of the sandbox-owned flag file: hash-guarded, atomic, exactly reversible
    changes.
    """
    def __init__(self, path: Path = FLAG_FILE):
        """Use the given flag file (deploy/flagd/demo.flagd.json by default)."""
        self.path = path

    def read(self) -> tuple[dict, str]:
        """The parsed flag file and its hash."""
        raw = self.path.read_bytes()
        return json.loads(raw), _sha(raw)

    def snapshot(self, flag: str) -> FlagSnapshot:
        """The current default variant, value and state of one flag."""
        doc, digest = self.read()
        entry = doc["flags"][flag]
        variant = entry["defaultVariant"]
        return FlagSnapshot(digest, flag, variant, entry["variants"][variant], entry["state"])

    def set_variant(self, flag: str, variant: str, expected_sha256: str,
                    state: str = "ENABLED") -> FlagSnapshot:
        """Change one flag's defaultVariant; return the snapshot after writing."""
        if flag not in WRITABLE_FLAGS:
            raise PermissionError(f"flag {flag} is not writable by the controller")
        if state not in ("ENABLED", "DISABLED"):
            raise ValueError("flag state must be ENABLED or DISABLED")
        doc, digest = self.read()
        if digest != expected_sha256:
            raise FlagConflict(f"{self.path.name} changed externally (expected {expected_sha256[:12]})")
        entry = doc["flags"][flag]
        if variant not in entry["variants"]:
            raise ValueError(f"{flag} has no variant {variant!r}")
        if entry.get("targeting"):
            raise ValueError(f"{flag} has targeting rules; defaultVariant would not decide evaluation")
        entry["defaultVariant"] = variant
        entry["state"] = state
        self._atomic_write(json.dumps(doc, indent=2) + "\n")
        return self.snapshot(flag)

    def restore(self, prior: FlagSnapshot, applied_sha256: str) -> FlagSnapshot:
        """Put back the exact prior variant if nobody else edited the file."""
        restored = self.set_variant(prior.flag, prior.default_variant, applied_sha256,
                                    state=prior.state)
        if restored.value != prior.value:
            raise RuntimeError(f"{prior.flag} restored to {restored.value!r}, expected {prior.value!r}")
        return restored

    # ------------------------------------------------ journaled change API
    def prepare(self, flag: str, variant: str) -> FlagChange:
        """Compute the exact applied file without writing anything."""
        if flag not in WRITABLE_FLAGS:
            raise PermissionError(f"flag {flag} is not writable by the controller")
        raw = self.path.read_bytes()
        doc = json.loads(raw)
        entry = doc["flags"][flag]
        if variant not in entry["variants"]:
            raise ValueError(f"{flag} has no variant {variant!r}")
        if entry.get("targeting"):
            raise ValueError(f"{flag} has targeting rules; defaultVariant would not decide evaluation")
        prior_variant = entry["defaultVariant"]
        if prior_variant == variant:
            raise ValueError(f"{flag} is already {variant!r}; nothing to inject")
        entry["defaultVariant"] = variant
        entry["state"] = "ENABLED"
        applied = json.dumps(doc, indent=2) + "\n"
        return FlagChange(flag, prior_variant, entry["variants"][prior_variant], variant,
                          entry["variants"][variant], _sha(raw), _sha(applied.encode()),
                          raw.decode(), applied)

    def state_of(self, change: FlagChange) -> str:
        """'prior', 'applied' or 'conflict' for the file as it is now."""
        digest = _sha(self.path.read_bytes())
        return {change.prior_sha256: "prior", change.applied_sha256: "applied"}.get(digest, "conflict")

    def apply(self, change: FlagChange) -> None:
        """Write a prepared change, refusing if the file changed since it was prepared."""
        if self.state_of(change) != "prior":
            raise FlagConflict(f"{self.path.name} changed since the change was prepared")
        self._atomic_write(change.applied_text)

    def undo(self, change: FlagChange) -> str:
        """Idempotent restore of the exact prior bytes; never overwrites external edits."""
        state = self.state_of(change)
        if state == "prior":
            return "already_prior"
        if state == "conflict":
            raise FlagConflict(f"{self.path.name} was edited externally during the run; "
                               "refusing to overwrite it")
        self._atomic_write(change.prior_text)
        if self.state_of(change) != "prior":
            raise FlagConflict(f"{self.path.name} did not return to its prior bytes")
        return "restored"

    def _atomic_write(self, text: str) -> None:
        """Write the whole file via a temp file and rename in the same directory, with exact bytes
        and no newline translation.
        """
        # Same directory => rename is atomic and visible through the dir bind mount.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".flagd-", suffix=".json")
        try:
            # utf-8 and no newline translation, so restored bytes equal the prior bytes.
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def baseline_problems(path: Path = FLAG_FILE) -> list[str]:
    """Baseline = every flag on its 'off' variant, matching the pinned upstream file."""
    doc = json.loads(path.read_text())
    problems = [f"{name} defaultVariant={entry['defaultVariant']}"
                for name, entry in doc["flags"].items() if entry["defaultVariant"] != "off"]
    if UPSTREAM_FLAG_FILE.exists():
        upstream = json.loads(UPSTREAM_FLAG_FILE.read_text())
        if set(upstream["flags"]) != set(doc["flags"]):
            problems.append("flag names differ from the pinned upstream definitions")
        for name, entry in doc["flags"].items():
            if name in upstream["flags"] and entry != upstream["flags"][name]:
                problems.append(f"{name} differs from the pinned healthy definition")
    return problems
