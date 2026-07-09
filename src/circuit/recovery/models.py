"""Data models for word-level RTL recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecoveredWord:
    """A recovered or hypothesized word-level signal."""

    name: str
    bits: tuple[str, ...]
    direction: str
    width: int
    signed_state: str = "undetermined"
    confidence: float = 1.0
    evidence: tuple[str, ...] = ()

    def compact(self) -> str:
        if self.width == 1:
            return self.name
        return f"{self.name}[{self.width}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bits": list(self.bits),
            "direction": self.direction,
            "width": self.width,
            "signed_state": self.signed_state,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class SupportCut:
    """The expression boundary used for a target word."""

    target: str
    support_words: tuple[str, ...]
    excluded_inputs: tuple[str, ...] = ()
    cone_size: int = 0
    reason: str = "transitive-fanin primary-input support"
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "support_words": list(self.support_words),
            "excluded_inputs": list(self.excluded_inputs),
            "cone_size": self.cone_size,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


@dataclass
class VerificationResult:
    """Verification status for one candidate or a whole RTL candidate."""

    status: str
    reason: str = ""
    elapsed: float = 0.0
    counterexample: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.status in {
            "exhaustive-exact",
            "exhaustive-masked",
            "sample-exact",
            "sample-masked",
            "cec-proved",
            "cec-timeout-assumed",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "elapsed": self.elapsed,
            "counterexample": self.counterexample,
        }


@dataclass
class ExpressionCandidate:
    """A candidate word-level expression for a target."""

    target: str
    expression: str
    method: str
    cost: int
    width: int
    inputs: tuple[str, ...]
    verification: VerificationResult
    candidate_id: str = ""
    case_condition: str = ""
    evidence: tuple[str, ...] = ()
    expr_obj: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "target": self.target,
            "expression": self.expression,
            "method": self.method,
            "cost": self.cost,
            "width": self.width,
            "inputs": list(self.inputs),
            "verification": self.verification.to_dict(),
            "case_condition": self.case_condition,
            "evidence": list(self.evidence),
        }


@dataclass
class WordAnalysis:
    """Recovered word boundaries and support cuts for the loaded circuit."""

    circuit_name: str
    input_words: tuple[RecoveredWord, ...]
    output_words: tuple[RecoveredWord, ...]
    support_cuts: tuple[SupportCut, ...]
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit_name": self.circuit_name,
            "input_words": [w.to_dict() for w in self.input_words],
            "output_words": [w.to_dict() for w in self.output_words],
            "support_cuts": [c.to_dict() for c in self.support_cuts],
            "evidence": list(self.evidence),
        }


@dataclass
class RecoveryReport:
    """A complete recovery attempt for one target or a full module."""

    circuit_name: str
    word_analysis: WordAnalysis
    candidates: tuple[ExpressionCandidate, ...]
    candidate_rtl: str = ""
    verification: VerificationResult | None = None
    unrecovered: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit_name": self.circuit_name,
            "word_analysis": self.word_analysis.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
            "candidate_rtl": self.candidate_rtl,
            "verification": self.verification.to_dict()
            if self.verification else None,
            "unrecovered": list(self.unrecovered),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ToolProfile:
    """Cost/usage metadata for an agent-facing recovery tool."""

    tool: str
    effort: str
    search_space: str
    risk: str
    requires_hypothesis: bool = False
    prerequisites: tuple[str, ...] = ()
    fallback_after: tuple[str, ...] = ()
    guidance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "effort": self.effort,
            "search_space": self.search_space,
            "risk": self.risk,
            "requires_hypothesis": self.requires_hypothesis,
            "prerequisites": list(self.prerequisites),
            "fallback_after": list(self.fallback_after),
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class MethodRecommendation:
    """One recommended next action in the agent recovery plan."""

    tool: str
    target: str
    reason: str
    priority: int = 1
    suggested_parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "target": self.target,
            "reason": self.reason,
            "priority": self.priority,
            "suggested_parameters": self.suggested_parameters,
        }


@dataclass(frozen=True)
class OutputCluster:
    """A group of output words that should be recovered together or in sequence."""

    name: str
    outputs: tuple[str, ...]
    support_words: tuple[str, ...]
    cone_nodes: int
    suspected_role: str
    evidence: tuple[str, ...] = ()
    recommendations: tuple[MethodRecommendation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outputs": list(self.outputs),
            "support_words": list(self.support_words),
            "cone_nodes": self.cone_nodes,
            "suspected_role": self.suspected_role,
            "evidence": list(self.evidence),
            "recommendations": [
                recommendation.to_dict()
                for recommendation in self.recommendations
            ],
        }


@dataclass(frozen=True)
class PlanStep:
    """A concrete recovery step for the agent to execute or revise."""

    step_id: str
    target: str
    tool: str
    method: str
    expected_evidence: str
    acceptance: str
    status: str = "pending"
    parameters: dict[str, Any] = field(default_factory=dict)
    effort: str = ""
    search_space: str = ""
    risk: str = ""
    prerequisites: tuple[str, ...] = ()
    why_cheapest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "target": self.target,
            "tool": self.tool,
            "method": self.method,
            "parameters": self.parameters,
            "expected_evidence": self.expected_evidence,
            "acceptance": self.acceptance,
            "status": self.status,
            "effort": self.effort,
            "search_space": self.search_space,
            "risk": self.risk,
            "prerequisites": list(self.prerequisites),
            "why_cheapest": self.why_cheapest,
        }


@dataclass(frozen=True)
class RecoveryFeedback:
    """Structured feedback from a failed or inconclusive recovery attempt."""

    source: str
    failure_type: str
    target: str = ""
    summary: str = ""
    counterexample: dict[str, Any] | None = None
    mismatch: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "failure_type": self.failure_type,
            "target": self.target,
            "summary": self.summary,
            "counterexample": self.counterexample,
            "mismatch": self.mismatch,
        }


@dataclass(frozen=True)
class PlanRevision:
    """A change to the recovery plan caused by feedback."""

    original_step: str
    problem: str
    revised_step: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_step": self.original_step,
            "problem": self.problem,
            "revised_step": self.revised_step,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    """An agent-facing plan for iterative RTL recovery."""

    circuit_name: str
    word_analysis: WordAnalysis
    clusters: tuple[OutputCluster, ...]
    steps: tuple[PlanStep, ...]
    recommended_sequence: tuple[str, ...]
    feedback: RecoveryFeedback | None = None
    revisions: tuple[PlanRevision, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit_name": self.circuit_name,
            "word_analysis": self.word_analysis.to_dict(),
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "steps": [step.to_dict() for step in self.steps],
            "recommended_sequence": list(self.recommended_sequence),
            "feedback": self.feedback.to_dict() if self.feedback else None,
            "revisions": [revision.to_dict() for revision in self.revisions],
            "warnings": list(self.warnings),
        }
