"""Pinned source context, observed dependencies and payment semantics (P4-02).

The context block is stable across requests, so it sits before the per-run
evidence in the system prompt and can be cached. Source code and evidence are
data for the model, never instructions.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONFIG = ROOT / "config" / "source_context.yaml"
SEMANTICS = ROOT / "config" / "payment_semantics.yaml"
DEPENDENCY_MAP = ROOT / "artifacts" / "dependency_map.static.json"
PAYMENT_PATH = {"frontend-proxy", "frontend", "checkout", "payment", "currency", "cart", "shipping",
                "email", "product-catalog"}


@dataclass(frozen=True)
class Excerpt:
    id: str
    path: str
    text: Optional[str]
    status: str          # verified | missing | hash_mismatch | anchor_not_found


def extract(text: str, start: str, end: Optional[str]) -> str:
    i = text.index(start)
    j = text.index(end, i) if end else len(text)
    return text[i:j].rstrip() + "\n"


def load_excerpts(config: Path = SOURCE_CONFIG, root: Path = ROOT) -> list[Excerpt]:
    spec = yaml.safe_load(config.read_text())
    base = root / spec["root"]
    out = []
    for item in spec["excerpts"]:
        path = base / item["path"]
        if not path.is_file():
            out.append(Excerpt(item["id"], item["path"], None, "missing"))
            continue
        try:
            text = extract(path.read_text(), item["start"], item["end"])
        except ValueError:
            out.append(Excerpt(item["id"], item["path"], None, "anchor_not_found"))
            continue
        ok = hashlib.sha256(text.encode()).hexdigest() == item["sha256"]
        out.append(Excerpt(item["id"], item["path"], text if ok else None, "verified" if ok else "hash_mismatch"))
    return out


def payment_edges(path: Path = DEPENDENCY_MAP) -> list[str]:
    doc = json.loads(path.read_text())
    edges = {f"{e['from']}->{e['to']} ({e['relationship']}, {e['evidence_type']}"
             f"{', runtime-observed' if e.get('runtime_observed') else ''})"
             for e in doc["edges"] if e["from"] in PAYMENT_PATH and e["to"] in PAYMENT_PATH}
    return sorted(edges)


@dataclass(frozen=True)
class SourceContext:
    text: str
    sha256: str
    excerpt_status: dict

    @property
    def complete(self) -> bool:
        return all(s == "verified" for s in self.excerpt_status.values())


def build_context(root: Path = ROOT) -> SourceContext:
    excerpts = load_excerpts(root=root)
    semantics = yaml.safe_load(SEMANTICS.read_text())
    parts = ["<payment_semantics source=\"config/payment_semantics.yaml\" kind=\"source-derived expectations\">",
             yaml.safe_dump(semantics, sort_keys=False).strip(), "</payment_semantics>",
             "<dependency_edges source=\"artifacts/dependency_map.static.json\">",
             *payment_edges(), "</dependency_edges>"]
    for e in excerpts:
        if e.text is None:
            parts.append(f"<source id=\"{e.id}\" path=\"{e.path}\" status=\"{e.status}\"/>")
        else:
            parts += [f"<source id=\"{e.id}\" path=\"{e.path}\" status=\"verified\">", e.text.rstrip(), "</source>"]
    text = "\n".join(parts)
    return SourceContext(text, hashlib.sha256(text.encode()).hexdigest(), {e.id: e.status for e in excerpts})
