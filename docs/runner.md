# Guarded injection and restoration (P2)

## Lifecycle of a live run

1. **Gates (no state yet):** capability enabled in `execution_policy.yaml`,
   approved sandbox, healthy baseline under 24 h old from the same daemon and
   flag file, derived `payment_policy.yaml`, duration within the hard cap.
2. **DRAFT:** the run store (`artifacts/results/controller.sqlite3`) rejects a
   reused `request_id` and any second active run. A detached watchdog starts.
3. **BASELINING → READY:** sandbox, flag file and app health are checked again.
4. **ARMED:** the exact prior file bytes and the expected applied hash are
   journaled before anything is written. Deadlines are persisted.
5. **RUNNING:** one flag is changed atomically; flagd must serve the new value
   within `flag_propagation_timeout_seconds`. Checkout and protected probes run
   every `probe_interval_seconds`.
6. **RESTORING → VERIFYING_RECOVERY:** exact prior bytes are restored (only if
   the file still holds our change), flagd must serve the prior value, recovery
   checkouts and protected probes must pass, and no service may have changed.
7. **RECOVERED / ABORTED / FAILED**, or **NEEDS_ATTENTION**, which blocks all
   new runs until an operator resolves it.

## Stop paths

| Trigger | Stop reason | Who restores |
|---|---|---|
| Duration elapsed | `completed` → RECOVERED | controller |
| `cli abort RUN` or Ctrl-C/SIGTERM | `manual_abort` | controller |
| Protected probes over policy | `protected_path_degraded` | controller |
| Probes cannot be taken | `observation_loss` | controller |
| Flag file edited by someone else | `config_conflict` → NEEDS_ATTENTION | nobody overwrites it |
| flagd never serves the value | `adapter_error` → FAILED | controller |
| App unhealthy before arming | `precondition_failed` | nothing to restore |
| Controller stuck past hard deadline + grace | `deadline` | watchdog |
| Controller killed | `controller_lost` | watchdog, or `cli reconcile` on restart |

Fewer than `min_affected_samples` checkout samples gives `conclusive: false`.

## Operator commands

```bash
python -m controller.cli run plan.json --live
python -m controller.cli status
python -m controller.cli abort RUN_ID
python -m controller.cli events RUN_ID --after 0
python -m controller.cli reconcile          # after a crash/restart
python -m controller.cli restore RUN_ID     # retry a NEEDS_ATTENTION run once fixed
```

## Before enabling a capability

P2-04 is complete only with real-sandbox evidence for each adapter: run the
stop paths above against the dedicated sandbox and keep their event logs.
Tests in `tests/test_runner.py` and `tests/test_stop_paths.py` cover the
logic with a fake sandbox, including a real SIGKILL of the controller process.
