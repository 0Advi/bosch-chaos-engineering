"""P0-03: sandbox-owned flag file, hash-guarded writes and exact restoration."""
import json
import shutil
from pathlib import Path

import pytest

from controller.executor.flag_store import FLAG_FILE, FlagConflict, FlagStore, baseline_problems


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "demo.flagd.json"
    shutil.copy(FLAG_FILE, path)
    return FlagStore(path)


def test_committed_flag_file_is_at_baseline():
    assert baseline_problems() == []


def test_set_and_restore_exact_prior(store):
    original = store.path.read_bytes()
    prior = store.snapshot("paymentUnreachable")
    applied = store.set_variant("paymentUnreachable", "on", prior.sha256)
    assert applied.value is True
    store.restore(prior, applied.sha256)
    assert json.loads(store.path.read_bytes()) == json.loads(original)


def test_external_edit_is_a_conflict_not_overwritten(store):
    prior = store.snapshot("paymentFailure")
    applied = store.set_variant("paymentFailure", "100%", prior.sha256)
    doc = json.loads(store.path.read_text())
    doc["flags"]["cartFailure"]["defaultVariant"] = "10%"      # someone else edits
    store.path.write_text(json.dumps(doc))
    with pytest.raises(FlagConflict):
        store.restore(prior, applied.sha256)
    assert json.loads(store.path.read_text())["flags"]["cartFailure"]["defaultVariant"] == "10%"


@pytest.mark.parametrize("flag, variant, error", [
    ("cartFailure", "100%", PermissionError),
    ("adFailure", "on", PermissionError),
    ("paymentFailure", "200%", ValueError),
])
def test_only_allowlisted_flags_and_variants(store, flag, variant, error):
    with pytest.raises(error):
        store.set_variant(flag, variant, store.snapshot("paymentFailure").sha256)


def test_no_temp_files_left_behind(store):
    prior = store.snapshot("paymentUnreachable")
    store.set_variant("paymentUnreachable", "on", prior.sha256)
    assert [p.name for p in store.path.parent.iterdir()] == ["demo.flagd.json"]


def test_restore_preserves_disabled_state(store):
    doc = json.loads(store.path.read_text())
    doc["flags"]["paymentFailure"]["state"] = "DISABLED"
    store.path.write_text(json.dumps(doc))
    original = json.loads(store.path.read_text())
    prior = store.snapshot("paymentFailure")
    applied = store.set_variant("paymentFailure", "100%", prior.sha256)
    assert applied.state == "ENABLED"
    store.restore(prior, applied.sha256)
    assert json.loads(store.path.read_text()) == original


def test_off_label_with_changed_value_is_not_a_healthy_baseline(store):
    doc = json.loads(store.path.read_text())
    doc["flags"]["emitRawPii"]["variants"]["off"] = True
    store.path.write_text(json.dumps(doc))
    assert any("emitRawPii differs" in p for p in baseline_problems(store.path))
