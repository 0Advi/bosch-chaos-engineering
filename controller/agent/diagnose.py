"""Evidence-grounded diagnosis and suggestions (P4-03).

The model (or the rule baseline) proposes; deterministic grounding decides:
- every cited id must exist in the blind bundle, and a conclusion needs one;
- low uncertainty is not accepted while signals are missing;
- payment retries are not accepted without idempotency/ledger evidence;
- insufficient_evidence must say which observations are needed.
The model's own confidence is never used as a readiness measure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Optional
from uuid import uuid4

from pydantic import ValidationError

from ..schemas import Diagnosis, DiagnosisCategory, DiagnosisResult, Suggestion
from .context import SourceContext, build_context
from .evidence import EvidenceBundle, assert_blind
from .providers import ModelProvider, ModelRequest, ProviderError

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "diagnosis_v1.md"
CATEGORIES = [c.value for c in DiagnosisCategory]
SUGGESTION_KINDS = ["observe", "configuration", "code_fix", "retry_policy", "none"]
_STR_LIST = {"type": "array", "items": {"type": "string"}}
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "service", "edge", "affected_paths", "cited_evidence", "alternatives",
                 "uncertainty", "next_action", "requested_observations", "suggestions"],
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "service": {"type": "string"},
        "edge": {"type": "string"},
        "affected_paths": _STR_LIST,
        "cited_evidence": _STR_LIST,
        "alternatives": _STR_LIST,
        "uncertainty": {"type": "string", "enum": ["low", "medium", "high"]},
        "next_action": {"type": "string"},
        "requested_observations": _STR_LIST,
        "suggestions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "action", "rationale"],
            "properties": {"kind": {"type": "string", "enum": SUGGESTION_KINDS},
                           "action": {"type": "string"}, "rationale": {"type": "string"}}}},
    },
}
RETRY_RE = re.compile(r"\bre-?(try|tries|trying|submit|charge)\b", re.I)
NEGATION_RE = re.compile(r"\b(not|never|avoid|no|don't|cannot|unsafe|before)\b", re.I)
EDGE_RE = re.compile(r"^[a-z-]+->[a-z-]+$")


def prompt_text() -> str:
    """The diagnosis system prompt (prompts/diagnosis_v1.md)."""
    return PROMPT_PATH.read_text()


def prompt_version() -> str:
    """A version id for the prompt plus output schema, so results record exactly which prompt
    produced them.
    """
    digest = hashlib.sha256((prompt_text() + json.dumps(OUTPUT_SCHEMA, sort_keys=True)).encode()).hexdigest()
    return f"diagnosis-v1-{digest[:8]}"


def recommends_retry(text: str) -> bool:
    """True when a sentence recommends retrying/resubmitting a payment (negations excluded)."""
    return any(RETRY_RE.search(sentence) and not NEGATION_RE.search(sentence)
               for sentence in re.split(r"(?<=[.;!?])\s+", text or ""))


def _has_idempotency_evidence(bundle: EvidenceBundle) -> bool:
    """Whether trusted evidence shows payment retries are safe from double charging; always False in
    this release (logs and model text cannot prove it).
    """
    # Logs and model-supplied text cannot prove duplicate-charge safety.
    # No trusted ledger/idempotency verifier is implemented in this release.
    return False


def ground(run_id: str, proposal: dict, bundle: EvidenceBundle, diagnoser: str, **provenance) -> DiagnosisResult:
    """Validate a proposal against the bundle; never upgrades an unsupported claim."""
    problems: list[str] = []
    ids = bundle.ids()
    cited = [c for c in proposal.get("cited_evidence", []) if isinstance(c, str)]
    unknown = sorted(set(cited) - ids)
    if unknown:
        problems.append(f"cites evidence that does not exist: {unknown}")
    valid_cited = [c for c in dict.fromkeys(cited) if c in ids]
    category = proposal.get("category")
    uncertainty = proposal.get("uncertainty", "high")
    edge = proposal.get("edge") or None
    if edge and not EDGE_RE.match(edge):
        problems.append(f"edge {edge!r} is not caller->callee")
        edge = None
    if bundle.missing and uncertainty == "low" and category != DiagnosisCategory.INSUFFICIENT_EVIDENCE.value:
        problems.append("claims low uncertainty although signals are missing: " + "; ".join(bundle.missing))
    if category == DiagnosisCategory.INSUFFICIENT_EVIDENCE.value and not proposal.get("requested_observations"):
        problems.append("insufficient_evidence without the observations needed to decide")
    suggestions = []
    for raw in proposal.get("suggestions", []):
        try:
            suggestions.append(Suggestion.model_validate(raw))
        except ValidationError:
            problems.append("a suggestion does not match the contract")
    actions = [proposal.get("next_action", "")] + [s.action for s in suggestions]
    if any(recommends_retry(a) for a in actions) and not _has_idempotency_evidence(bundle):
        problems.append("suggests retrying payments without idempotency or ledger evidence")

    diagnosis = None
    try:
        diagnosis = Diagnosis(run_id=run_id, service=proposal.get("service") or None, edge=edge,
                              category=category, affected_paths=proposal.get("affected_paths", []),
                              cited_observations=valid_cited, alternatives=proposal.get("alternatives", []),
                              uncertainty=uncertainty, next_action=proposal.get("next_action", ""))
    except ValidationError as error:
        problems.append("rejected: " + "; ".join(e["msg"] for e in error.errors())[:300])
    status = "rejected" if diagnosis is None else ("unverified" if problems else "grounded")
    return DiagnosisResult(
        result_id=f"dx-{uuid4().hex[:12]}", run_id=run_id, diagnoser=diagnoser, status=status,
        diagnosis=diagnosis, suggestions=suggestions,
        requested_observations=proposal.get("requested_observations", []), problems=problems,
        evidence_sha256=bundle.sha256, evidence_items=len(bundle.items), **provenance)


def diagnose_with_model(run_id: str, bundle: EvidenceBundle, provider: ModelProvider,
                        context: Optional[SourceContext] = None) -> DiagnosisResult:
    """Diagnose a run with the model: check the bundle is blind, send the context and evidence, then
    ground the proposal; provider failures become a recorded error result.
    """
    assert_blind(bundle)
    context = context or build_context()
    version = prompt_version()
    system = prompt_text() + "\n\n" + context.text
    user = bundle.as_prompt() + "\n\nDiagnose this run."
    base = dict(run_id=run_id, diagnoser="model", evidence_sha256=bundle.sha256,
                evidence_items=len(bundle.items), model_requested=getattr(provider, "model", None),
                prompt_version=version)
    try:
        response = provider.complete(ModelRequest(system=system, messages=[{"role": "user", "content": user}],
                                                  max_tokens=16000, output_schema=OUTPUT_SCHEMA))
        proposal = response.json()
    except ProviderError as error:
        return DiagnosisResult(result_id=f"dx-{uuid4().hex[:12]}", status="failed",
                               provider_error=error.as_dict(), problems=[f"provider {error.kind}"], **base)
    result = ground(run_id, proposal, bundle, "model", model_requested=response.model_requested,
                    model_served=response.model_served, prompt_version=version, usage=response.usage,
                    latency_s=response.latency_s)
    if not context.complete:
        missing = [k for k, v in context.excerpt_status.items() if v != "verified"]
        result = result.model_copy(update={"problems": [*result.problems, f"source context incomplete: {missing}"],
                                           "status": "unverified" if result.status == "grounded" else result.status})
    return result
