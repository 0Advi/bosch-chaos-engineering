"""Diagnose a finished run and store the result with its evidence (P4-03)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..report.evidence import export_run, run_dir
from ..schemas import DiagnosisResult
from ..store import RunStore
from .baseline import diagnose_with_rules
from .diagnose import diagnose_with_model
from .evidence import from_run
from .providers import ModelProvider


def diagnose_run(store: RunStore, run_id: str, results_dir: Path, diagnoser: str = "model",
                 provider: Optional[ModelProvider] = None, telemetry=None) -> DiagnosisResult:
    record, events = store.get(run_id), store.events(run_id)
    bundle = from_run(record, events, telemetry)
    if diagnoser == "baseline":
        result = diagnose_with_rules(run_id, bundle)
    else:
        if provider is None:
            raise ValueError("a model provider is required for model diagnosis")
        result = diagnose_with_model(run_id, bundle, provider)
    out = run_dir(results_dir, run_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "diagnosis.json").write_text(result.model_dump_json(indent=2) + "\n")
    export_run(store, run_id, results_dir)     # refresh the manifest to include the diagnosis
    return result
