"""Reasoning-graph recovery IR and local experience retrieval.

The recovery tools still use simulation/CEC as authority.  This module records
only compact problem-solving nodes: failures, small experiments, observations,
relations, validations, and promoted experiences.  It deliberately does not
persist raw tool logs, full reports, transcripts, or bit-level patterns.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .models import ExpressionCandidate, RecoveryPlan, RecoveryReport, VerificationResult

if TYPE_CHECKING:
    from ..circuit import Circuit


SCHEMA_VERSION = 2
DEFAULT_RUN_BASE = Path(".gate_spy") / "runs"
DEFAULT_KB_DIR = Path("data") / "recovery_kb"
GRAPH_NODE_TYPES = {
    "problem",
    "experiment",
    "observation",
    "relation",
    "validation",
    "experience",
}
_MAX_TEXT = 600
_MAX_LIST = 8
_MAX_DICT = 16


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _stable_hash(data: Any, length: int = 16) -> str:
    payload = json.dumps(data, sort_keys=True, default=_json_default).encode()
    return hashlib.sha256(payload).hexdigest()[:length]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=_json_default) + "\n")


_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|\d+'[sdhboSDHBO][0-9a-fA-F_xXzZ]+|"
    r"\d+|==|!=|<=|>=|&&|\|\||[+\-*/%&|^~?:<>]"
)


def _base_name(ref: str) -> str:
    return ref.split("[", 1)[0]


def _tokens_from_text(text: str) -> list[str]:
    return sorted({tok.lower() for tok in _TOKEN_RE.findall(text or "")})


def _accepted_status(status: str) -> bool:
    return status in {
        "exhaustive-exact",
        "exhaustive-masked",
        "sample-exact",
        "sample-masked",
        "cec-proved",
        "cec-timeout-assumed",
    }


def _truncate(value: str, limit: int = _MAX_TEXT) -> str:
    text = value.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Keep graph data small and word-level; never preserve raw transcripts."""
    if depth > 4:
        return _truncate(str(value), 120)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return _sanitize(value.to_dict(), depth=depth + 1)
    if isinstance(value, (list, tuple)):
        out = [_sanitize(item, depth=depth + 1) for item in list(value)[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            out.append({"truncated_items": len(value) - _MAX_LIST})
        return out
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= _MAX_DICT:
                out["truncated_keys"] = len(value) - _MAX_DICT
                break
            key_s = str(key)
            if key_s in {"text", "text_preview", "report", "plan", "candidate_preview"}:
                continue
            if key_s in {"pattern", "bit_values", "bits"}:
                continue
            out[key_s] = _sanitize(item, depth=depth + 1)
        return out
    return _truncate(str(value), 160)


def _trim_counterexample(counterexample: dict[str, Any]) -> dict[str, Any]:
    """Keep only compact word-level CEX fields."""
    if not isinstance(counterexample, dict):
        return {}
    keep: dict[str, Any] = {}
    for key in (
        "mismatch",
        "mismatching_output",
        "output",
        "target",
        "word_values",
        "inputs",
        "outputs",
    ):
        if key in counterexample:
            keep[key] = counterexample[key]
    if not keep:
        for key, value in counterexample.items():
            if "word" in str(key).lower() or "mismatch" in str(key).lower():
                keep[str(key)] = value
    return _sanitize(keep)


@dataclass(frozen=True)
class CircuitFingerprint:
    """Stable identifiers for the loaded circuit and source text."""

    circuit_name: str
    source_path: str = ""
    source_sha256: str = ""
    structural_hash: str = ""
    input_count: int = 0
    output_count: int = 0
    gate_count: int = 0
    gate_histogram: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_circuit(cls, circuit: Circuit, source_path: str | None = None) -> "CircuitFingerprint":
        source_sha = ""
        if source_path and os.path.isfile(source_path):
            source_sha = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
        structural_payload = {
            "name": circuit.name,
            "inputs": list(circuit.input_nets),
            "outputs": list(circuit.output_nets),
            "hist": circuit.gate_histogram(),
            "node_count": len(circuit.nodes),
        }
        return cls(
            circuit_name=circuit.name,
            source_path=source_path or "",
            source_sha256=source_sha,
            structural_hash=_stable_hash(structural_payload, length=32),
            input_count=len(circuit.input_nets),
            output_count=len(circuit.output_nets),
            gate_count=len(circuit.gate_nodes),
            gate_histogram=dict(sorted(circuit.gate_histogram().items())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit_name": self.circuit_name,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "structural_hash": self.structural_hash,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "gate_count": self.gate_count,
            "gate_histogram": self.gate_histogram,
        }


@dataclass(frozen=True)
class ReasoningGraphNode:
    node_id: str
    node_type: str
    target: str = ""
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    links: tuple[str, ...] = ()
    features: dict[str, Any] = field(default_factory=dict)
    promotable: bool = False
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "target": self.target,
            "summary": self.summary,
            "data": self.data,
            "links": list(self.links),
            "features": self.features,
            "promotable": self.promotable,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: str
    target: str = ""
    tool: str = ""
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "target": self.target,
            "tool": self.tool,
            "summary": self.summary,
            "data": self.data,
            "verified": self.verified,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class HypothesisIR:
    hypothesis_id: str
    target: str
    expression: str = ""
    status: str = "unverified"
    confidence: float = 0.0
    provenance: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "target": self.target,
            "expression": self.expression,
            "status": self.status,
            "confidence": self.confidence,
            "provenance": list(self.provenance),
            "data": self.data,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CandidateIR:
    candidate_id: str
    target: str
    expression: str
    method: str
    cost: int
    width: int
    inputs: tuple[str, ...]
    verification: dict[str, Any]
    case_condition: str = ""
    evidence: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    created_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_candidate(cls, candidate: ExpressionCandidate,
                       provenance: tuple[str, ...] = ()) -> "CandidateIR":
        return cls(
            candidate_id=candidate.candidate_id,
            target=candidate.target,
            expression=candidate.expression,
            method=candidate.method,
            cost=candidate.cost,
            width=candidate.width,
            inputs=tuple(candidate.inputs),
            verification=candidate.verification.to_dict(),
            case_condition=candidate.case_condition,
            evidence=tuple(candidate.evidence),
            provenance=provenance,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "target": self.target,
            "expression": self.expression,
            "method": self.method,
            "cost": self.cost,
            "width": self.width,
            "inputs": list(self.inputs),
            "verification": self.verification,
            "case_condition": self.case_condition,
            "evidence": list(self.evidence),
            "provenance": list(self.provenance),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CexIR:
    cex_id: str
    source: str
    target: str = ""
    counterexample: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cex_id": self.cex_id,
            "source": self.source,
            "target": self.target,
            "counterexample": self.counterexample,
            "summary": self.summary,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ToolCallIR:
    event_id: str
    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    target: str = ""
    status: str = "ok"
    result_kind: str = "text"
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tool": self.tool,
            "params": self.params,
            "target": self.target,
            "status": self.status,
            "result_kind": self.result_kind,
            "data": self.data,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LearningEvent:
    event_id: str
    kind: str
    target: str = ""
    source_candidate_id: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    promotable: bool = False
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "target": self.target,
            "source_candidate_id": self.source_candidate_id,
            "features": self.features,
            "payload": self.payload,
            "promotable": self.promotable,
            "created_at": self.created_at,
        }


@dataclass
class RecoveryRunIR:
    run_id: str
    case_id: str
    circuit: CircuitFingerprint
    run_dir: str
    started_at: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "circuit": self.circuit.to_dict(),
            "run_dir": self.run_dir,
            "started_at": self.started_at,
            "node_count": len(self.nodes),
            "nodes": self.nodes,
        }


def recovery_feature_tokens(circuit: Circuit | None = None,
                            target: str = "",
                            inputs: list[str] | tuple[str, ...] | None = None,
                            expression: str = "",
                            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build deterministic retrieval features without external embeddings."""
    tokens: set[str] = set()
    features: dict[str, Any] = {
        "target": target,
        "inputs": list(inputs or []),
        "tokens": [],
    }
    if circuit is not None:
        hist = circuit.gate_histogram()
        features.update({
            "circuit_name": circuit.name,
            "gate_count": len(circuit.gate_nodes),
            "input_count": len(circuit.input_nets),
            "output_count": len(circuit.output_nets),
            "gate_histogram": dict(sorted(hist.items())),
        })
        input_bases = {_base_name(ref).lower() for ref in circuit.input_nets}
        output_bases = {_base_name(ref).lower() for ref in circuit.output_nets}
        tokens.update(f"gate:{kind.lower()}" for kind in hist)
        tokens.update(f"input:{base}" for base in input_bases)
        tokens.update(f"output:{base}" for base in output_bases)
        tokens.update(input_bases)
        tokens.update(output_bases)
    if target:
        tokens.add(f"target:{_base_name(target).lower()}")
        tokens.update(_tokens_from_text(target))
    for ref in inputs or []:
        base = _base_name(ref).lower()
        tokens.add(f"input:{base}")
        tokens.add(base)
        tokens.update(_tokens_from_text(ref))
    if expression:
        tokens.update(f"expr:{tok}" for tok in _tokens_from_text(expression))
    if extra:
        features.update(extra)
        tokens.update(_tokens_from_text(json.dumps(extra, sort_keys=True, default=_json_default)))
    features["tokens"] = sorted(tokens)
    return features


def _record_tokens(record: dict[str, Any]) -> set[str]:
    tokens = set(str(tok).lower() for tok in record.get("tokens", []) if tok)
    features = record.get("features") if isinstance(record.get("features"), dict) else {}
    tokens.update(str(tok).lower() for tok in features.get("tokens", []) if tok)
    for key in ("target", "relation", "trigger", "suggested_experiment", "summary", "kind"):
        tokens.update(_tokens_from_text(str(record.get(key, ""))))
    return tokens


class LocalRecoveryKB:
    """Small deterministic experience retrieval store backed by JSONL files."""

    def __init__(self, kb_dir: str | Path = DEFAULT_KB_DIR) -> None:
        self.kb_dir = Path(kb_dir)

    @property
    def experience_path(self) -> Path:
        return self.kb_dir / "experiences.jsonl"

    @property
    def relation_path(self) -> Path:
        return self.kb_dir / "relations.jsonl"

    def query(self, *, circuit: Circuit | None = None,
              target: str = "",
              inputs: list[str] | tuple[str, ...] | None = None,
              features: dict[str, Any] | None = None,
              limit: int = 5) -> list[dict[str, Any]]:
        query_features = recovery_feature_tokens(
            circuit, target=target, inputs=inputs,
            extra=features or {})
        query_tokens = set(query_features["tokens"])
        records: list[dict[str, Any]] = []
        for kind, path in (
            ("experience", self.experience_path),
            ("relation", self.relation_path),
        ):
            for record in _read_jsonl(path):
                rec_tokens = _record_tokens(record)
                overlap = query_tokens & rec_tokens
                score = len(overlap) * 3
                if target and _base_name(str(record.get("target", ""))) == _base_name(target):
                    score += 6
                if circuit is not None:
                    rec_features = record.get("features")
                    if isinstance(rec_features, dict) and rec_features.get("gate_histogram") == circuit.gate_histogram():
                        score += 5
                if score <= 0:
                    continue
                records.append({
                    "kind": kind,
                    "negative": bool(record.get("negative")),
                    "score": score,
                    "knowledge_id": record.get("knowledge_id", ""),
                    "target": record.get("target", ""),
                    "relation": record.get("relation", ""),
                    "trigger": record.get("trigger", ""),
                    "suggested_experiment": record.get("suggested_experiment", ""),
                    "validation": record.get("validation", {}),
                    "summary": record.get("summary", ""),
                    "provenance": record.get("provenance", {}),
                    "matched_tokens": sorted(overlap)[:24],
                    "expression": "",
                    "method": record.get("relation", ""),
                    "verification": record.get("validation", {}),
                })
        records.sort(key=lambda item: (-int(item["score"]), item["kind"], item["knowledge_id"]))
        return records[:max(0, limit)]

    def promote_run(self, run_id_or_path: str | Path,
                    *,
                    run_base: str | Path = DEFAULT_RUN_BASE,
                    candidate_ids: list[str] | None = None,
                    failure_ids: list[str] | None = None,
                    dry_run: bool = True) -> dict[str, Any]:
        run_path = Path(run_id_or_path)
        if not run_path.exists():
            run_path = Path(run_base) / str(run_id_or_path)
        graph_json = run_path / "graph.json"
        graph_jsonl = run_path / "graph.jsonl"
        if graph_json.exists():
            run_data = json.loads(graph_json.read_text(encoding="utf-8"))
            nodes = run_data.get("nodes", [])
        else:
            nodes = _read_jsonl(graph_jsonl)
            run_data = {"run_id": run_path.name, "case_id": "", "nodes": nodes}

        selected_positive = set(candidate_ids or [])
        selected_negative = set(failure_ids or [])
        promote_all = not selected_positive and not selected_negative
        relation_nodes = [node for node in nodes if node.get("node_type") == "relation"]
        validation_nodes = [
            node for node in nodes
            if node.get("node_type") == "validation"
            and _accepted_status(str(node.get("data", {}).get("status", "")))
            and not str(node.get("data", {}).get("status", "")).startswith("slice-")
        ]
        problem_nodes = [node for node in nodes if node.get("node_type") == "problem"]

        relation_records: list[dict[str, Any]] = []
        experience_records: list[dict[str, Any]] = []
        for relation in relation_nodes:
            node_id = str(relation.get("node_id", ""))
            if selected_positive and node_id not in selected_positive:
                continue
            validation = self._matching_validation(relation, validation_nodes)
            if validation is None:
                continue
            relation_records.append(self._relation_record(run_data, relation, validation))
            experience_records.append(self._experience_record(
                run_data, relation, validation, negative=False))

        for problem in problem_nodes:
            node_id = str(problem.get("node_id", ""))
            if not (problem.get("promotable") or node_id in selected_negative):
                continue
            if not (promote_all or node_id in selected_negative):
                continue
            experience_records.append(self._experience_record(
                run_data, problem, None, negative=True))

        result = {
            "dry_run": dry_run,
            "run": str(run_path),
            "templates": 0,
            "failures": sum(1 for record in experience_records if record.get("negative")),
            "relations": len(relation_records),
            "experiences": len(experience_records),
            "records": relation_records + experience_records,
        }
        if dry_run:
            return result

        self.kb_dir.mkdir(parents=True, exist_ok=True)
        existing_relations = self._existing_ids(self.relation_path)
        existing_experiences = self._existing_ids(self.experience_path)
        written = {"relations": 0, "experiences": 0, "templates": 0, "failures": 0}
        for record in relation_records:
            if record["knowledge_id"] in existing_relations:
                continue
            _append_jsonl(self.relation_path, record)
            existing_relations.add(record["knowledge_id"])
            written["relations"] += 1
        for record in experience_records:
            if record["knowledge_id"] in existing_experiences:
                continue
            _append_jsonl(self.experience_path, record)
            existing_experiences.add(record["knowledge_id"])
            written["experiences"] += 1
            if record.get("negative"):
                written["failures"] += 1
        result["written"] = written
        return result

    @staticmethod
    def _matching_validation(relation: dict[str, Any],
                             validations: list[dict[str, Any]]) -> dict[str, Any] | None:
        target = relation.get("target", "")
        linked = set(relation.get("links", []) or [])
        relation_expr = str(relation.get("data", {}).get("expression", ""))
        for validation in validations:
            if validation.get("node_id") in linked:
                return validation
            if target and validation.get("target") != target:
                continue
            validation_expr = str(validation.get("data", {}).get("expression", ""))
            if relation_expr and validation_expr and relation_expr != validation_expr:
                continue
            return validation
        return None

    def _relation_record(self, run_data: dict[str, Any],
                         relation: dict[str, Any],
                         validation: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "kind": "relation",
            "relation": relation.get("data", {}).get("relation", relation.get("summary", "")),
            "target": relation.get("target", ""),
            "source_run_id": run_data.get("run_id", ""),
            "source_node_id": relation.get("node_id", ""),
        }
        knowledge_id = _stable_hash(payload, length=24)
        features = relation.get("features") or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "knowledge_id": knowledge_id,
            "kind": "relation",
            "target": relation.get("target", ""),
            "relation": relation.get("data", {}).get("relation", relation.get("summary", "")),
            "trigger": relation.get("data", {}).get("trigger", ""),
            "suggested_experiment": relation.get("data", {}).get("suggested_experiment", ""),
            "validation": validation.get("data", {}),
            "summary": relation.get("summary", ""),
            "negative": False,
            "features": features,
            "tokens": features.get("tokens", []),
            "provenance": self._provenance(run_data, relation, validation),
            "created_at": _utc_now(),
        }

    def _experience_record(self, run_data: dict[str, Any],
                           source: dict[str, Any],
                           validation: dict[str, Any] | None,
                           *, negative: bool) -> dict[str, Any]:
        data = source.get("data", {}) if isinstance(source.get("data"), dict) else {}
        relation = data.get("relation", source.get("summary", ""))
        trigger = data.get("trigger") or data.get("failure_type", "")
        suggested = data.get("suggested_experiment", "")
        payload = {
            "kind": "experience",
            "target": source.get("target", ""),
            "relation": relation,
            "trigger": trigger,
            "negative": negative,
            "source_run_id": run_data.get("run_id", ""),
            "source_node_id": source.get("node_id", ""),
        }
        knowledge_id = _stable_hash(payload, length=24)
        features = source.get("features") or recovery_feature_tokens(
            target=str(source.get("target", "")),
            extra={"relation": relation, "trigger": trigger, "suggested_experiment": suggested})
        summary = source.get("summary", "")
        if negative and not summary:
            summary = "negative experience"
        return {
            "schema_version": SCHEMA_VERSION,
            "knowledge_id": knowledge_id,
            "kind": "experience",
            "target": source.get("target", ""),
            "relation": relation,
            "trigger": trigger,
            "suggested_experiment": suggested,
            "validation": validation.get("data", {}) if validation else {},
            "summary": summary,
            "negative": negative,
            "features": features,
            "tokens": features.get("tokens", []),
            "provenance": self._provenance(run_data, source, validation),
            "created_at": _utc_now(),
        }

    @staticmethod
    def _provenance(run_data: dict[str, Any],
                    source: dict[str, Any],
                    validation: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "run_id": run_data.get("run_id", ""),
            "case_id": run_data.get("case_id", ""),
            "source_node_id": source.get("node_id", ""),
            "validation_node_id": validation.get("node_id", "") if validation else "",
            "circuit": run_data.get("circuit", {}),
        }

    @staticmethod
    def _existing_ids(path: Path) -> set[str]:
        return {
            str(record.get("knowledge_id"))
            for record in _read_jsonl(path)
            if record.get("knowledge_id")
        }


class RecoveryIRRecorder:
    """Graph-only recorder for one recovery run."""

    def __init__(self, *,
                 run_base: str | Path = DEFAULT_RUN_BASE,
                 kb_dir: str | Path = DEFAULT_KB_DIR) -> None:
        self.run_base = Path(run_base)
        self.kb = LocalRecoveryKB(kb_dir)
        self.run: RecoveryRunIR | None = None
        self._node_counter = 0

    @property
    def active(self) -> bool:
        return self.run is not None

    @property
    def run_id(self) -> str:
        return self.run.run_id if self.run else ""

    @property
    def run_dir(self) -> Path | None:
        return Path(self.run.run_dir) if self.run else None

    def reset(self) -> None:
        self.run = None
        self._node_counter = 0

    def start(self, circuit: Circuit, *,
              source_path: str | None = None,
              case_id: str | None = None,
              out_dir: str | Path | None = None) -> RecoveryRunIR:
        run_id = f"run-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        base = Path(out_dir) if out_dir else self.run_base
        run_dir = base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        inferred_case = case_id or self._infer_case_id(source_path)
        self.run = RecoveryRunIR(
            run_id=run_id,
            case_id=inferred_case,
            circuit=CircuitFingerprint.from_circuit(circuit, source_path),
            run_dir=str(run_dir),
            started_at=_utc_now(),
        )
        self._node_counter = 0
        self._write_snapshot()
        return self.run

    def add_graph_node(self, node_type: str, *,
                       target: str = "",
                       summary: str = "",
                       data: dict[str, Any] | None = None,
                       links: list[str] | tuple[str, ...] | None = None,
                       features: dict[str, Any] | None = None,
                       promotable: bool = False) -> dict[str, Any] | None:
        if self.run is None:
            return None
        if node_type not in GRAPH_NODE_TYPES:
            raise ValueError(
                f"node_type must be one of {sorted(GRAPH_NODE_TYPES)}, got {node_type!r}")
        self._node_counter += 1
        sanitized = _sanitize(data or {})
        feature_extra = sanitized if isinstance(sanitized, dict) else {}
        if features is not None:
            safe_features = _sanitize(features)
            if not isinstance(safe_features, dict):
                safe_features = {"value": safe_features}
        else:
            safe_features = recovery_feature_tokens(
                target=target,
                extra={"node_type": node_type, "summary": summary, **feature_extra})
        node = ReasoningGraphNode(
            node_id=f"G{self._node_counter:04d}",
            node_type=node_type,
            target=target,
            summary=_truncate(summary),
            data=sanitized if isinstance(sanitized, dict) else {"value": sanitized},
            links=tuple(str(link) for link in (links or ())),
            features=safe_features,
            promotable=promotable,
        ).to_dict()
        self.run.nodes.append(node)
        run_dir = Path(self.run.run_dir)
        _append_jsonl(run_dir / "graph.jsonl", node)
        self._write_snapshot()
        return node

    def log_event(self, event_type: str, *,
                  payload: dict[str, Any] | None = None,
                  tool: str = "",
                  target: str = "",
                  status: str = "ok",
                  features: dict[str, Any] | None = None) -> dict[str, Any] | None:
        payload = payload or {}
        if event_type == "tool_deferred":
            return self.add_graph_node(
                "problem",
                target=target,
                summary=str(payload.get("reason") or f"{tool} deferred"),
                data={"failure_type": "tool_deferred", "tool": tool, **payload},
                features=features,
            )
        if event_type == "rtl_verification":
            verification = payload.get("verification", {})
            return self.add_graph_node(
                "validation",
                target=target,
                summary=f"{tool} {status}",
                data={"tool": tool, "status": status, "verification": verification},
                features=features,
            )
        if event_type == "cex":
            return self.add_graph_node(
                "problem",
                target=target,
                summary=str(payload.get("summary") or "counterexample"),
                data={
                    "failure_type": "cex",
                    "tool": tool,
                    "counterexample": _trim_counterexample(payload.get("counterexample", {})),
                },
                features=features,
            )
        return None

    def record_tool_text(self, tool: str, params: dict[str, Any], text: str,
                         *, target: str = "", status: str = "ok") -> None:
        if tool not in {"split_control_cases", "influence_profile"}:
            return
        self.add_graph_node(
            "experiment",
            target=target,
            summary=f"ran {tool}",
            data={"tool": tool, "params": params, "status": status},
        )

    def record_plan(self, tool: str, params: dict[str, Any],
                    plan: RecoveryPlan) -> None:
        return None

    def record_report(self, tool: str, params: dict[str, Any],
                      report: RecoveryReport,
                      circuit: Circuit | None = None) -> None:
        if self.run and report.candidate_rtl and tool == "assemble_rtl":
            path = Path(self.run.run_dir) / "final_candidate.v"
            path.write_text(report.candidate_rtl, encoding="utf-8")
        for cand in report.candidates:
            self.record_candidate(cand, tool=tool, circuit=circuit)
        if not report.candidates and tool in {
            "validate_expr",
            "fit_template",
            "infer_native_expr",
            "check_polynomial_lowbits",
        }:
            self.add_graph_node(
                "problem",
                target=str(params.get("target", "")),
                summary=f"{tool} produced no accepted candidate",
                data={
                    "failure_type": "no_candidate",
                    "tool": tool,
                    "params": params,
                    "notes": list(report.notes)[:_MAX_LIST],
                },
            )
        if report.verification:
            self.add_graph_node(
                "validation",
                target=",".join(c.target for c in report.candidates[:_MAX_LIST]),
                summary=f"{tool} {report.verification.status}",
                data={
                    "tool": tool,
                    "status": report.verification.status,
                    "reason": report.verification.reason,
                    "elapsed": report.verification.elapsed,
                },
            )
            if report.verification.counterexample:
                self.record_cex(report.verification, source=tool)

    def record_candidate(self, candidate: ExpressionCandidate, *,
                         tool: str = "",
                         circuit: Circuit | None = None) -> None:
        verification = candidate.verification.status
        features = recovery_feature_tokens(
            circuit,
            target=candidate.target,
            inputs=list(candidate.inputs),
            expression=candidate.expression,
            extra={
                "method": candidate.method,
                "verification": verification,
                "case_condition": candidate.case_condition,
            })
        data = {
            "tool": tool,
            "candidate_id": candidate.candidate_id,
            "expression": candidate.expression,
            "method": candidate.method,
            "cost": candidate.cost,
            "width": candidate.width,
            "inputs": list(candidate.inputs),
            "case_condition": candidate.case_condition,
            "status": verification,
            "reason": candidate.verification.reason,
        }
        if _accepted_status(verification) and not verification.startswith("slice-"):
            self.add_graph_node(
                "validation",
                target=candidate.target,
                summary=f"validated {candidate.target} with {verification}",
                data=data,
                features=features,
            )
        else:
            self.add_graph_node(
                "problem",
                target=candidate.target,
                summary=f"{candidate.target} hypothesis failed with {verification}",
                data={"failure_type": "failed_candidate", **data},
                features=features,
            )
        if candidate.verification.counterexample:
            self.record_cex(candidate.verification, source=tool, target=candidate.target)

    def record_cex(self, verification: VerificationResult, *,
                   source: str,
                   target: str = "") -> None:
        if not verification.counterexample:
            return
        self.add_graph_node(
            "problem",
            target=target,
            summary=verification.reason or "counterexample",
            data={
                "failure_type": "cex",
                "source": source,
                "status": verification.status,
                "counterexample": _trim_counterexample(verification.counterexample),
            },
        )

    def record_note(self, *,
                    kind: str,
                    target: str = "",
                    summary: str = "",
                    refs: list[str] | None = None) -> dict[str, Any] | None:
        node_type = kind if kind in GRAPH_NODE_TYPES else "observation"
        return self.add_graph_node(
            node_type,
            target=target,
            summary=summary,
            data={"note_kind": kind, "refs": refs or [], "verified": False},
            promotable=False,
        )

    def query_memory(self, *, circuit: Circuit | None = None,
                     target: str = "",
                     inputs: list[str] | tuple[str, ...] | None = None,
                     features: dict[str, Any] | None = None,
                     limit: int = 5) -> list[dict[str, Any]]:
        return self.kb.query(
            circuit=circuit,
            target=target,
            inputs=inputs,
            features=features,
            limit=limit)

    def promote_memory(self, *,
                       run_id: str | Path | None = None,
                       candidate_ids: list[str] | None = None,
                       failure_ids: list[str] | None = None,
                       dry_run: bool = True) -> dict[str, Any]:
        run_ref: str | Path
        if run_id is not None:
            run_ref = run_id
        elif self.run is not None:
            run_ref = self.run.run_dir
        else:
            raise ValueError("no active recovery run and no run_id was provided")
        return self.kb.promote_run(
            run_ref,
            run_base=self.run_base,
            candidate_ids=candidate_ids,
            failure_ids=failure_ids,
            dry_run=dry_run)

    def export(self) -> dict[str, Any]:
        return self.run.to_dict() if self.run else {
            "schema_version": SCHEMA_VERSION,
            "status": "no_active_recovery_run",
        }

    def _write_snapshot(self) -> None:
        if self.run is None:
            return
        run_dir = Path(self.run.run_dir)
        graph_path = run_dir / "graph.json"
        graph_path.write_text(
            json.dumps(self.run.to_dict(), indent=2, sort_keys=True,
                       default=_json_default),
            encoding="utf-8")
        summary = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run.run_id,
            "case_id": self.run.case_id,
            "circuit": self.run.circuit.to_dict(),
            "node_count": len(self.run.nodes),
            "node_types": self._node_type_counts(),
            "run_dir": self.run.run_dir,
            "started_at": self.run.started_at,
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=_json_default),
            encoding="utf-8")

    def _node_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        if self.run is None:
            return counts
        for node in self.run.nodes:
            node_type = str(node.get("node_type", ""))
            counts[node_type] = counts.get(node_type, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _infer_case_id(source_path: str | None) -> str:
        if not source_path:
            return ""
        path = Path(source_path)
        if path.name == "top_primitive.v" and path.parent.name:
            return path.parent.name
        return path.stem
