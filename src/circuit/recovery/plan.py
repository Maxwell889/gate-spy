"""Agent-facing planning for iterative RTL recovery."""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..symbolic.sampler import EXHAUSTIVE_MAX_BITS
from .models import (
    MethodRecommendation,
    OutputCluster,
    PlanRevision,
    PlanStep,
    RecoveryFeedback,
    RecoveryPlan,
    RecoveredWord,
    SupportCut,
)
from .tool_profiles import get_tool_profile
from .word import analyze_words

if TYPE_CHECKING:
    from ..circuit import Circuit


def _feedback_from_any(feedback: str | dict[str, Any] | None) -> RecoveryFeedback | None:
    if feedback is None or feedback == "":
        return None
    if isinstance(feedback, str):
        text = feedback.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return _feedback_from_any(data)
        except json.JSONDecodeError:
            pass
        lower = text.lower()
        if "timeout" in lower:
            failure_type = "timeout"
        elif "cex" in lower or "counterexample" in lower or "mismatch" in lower:
            failure_type = "cex"
        elif "control" in lower or "mux" in lower:
            failure_type = "control-failed"
        elif "no candidate" in lower or "failed validation" in lower:
            failure_type = "no-candidate"
        else:
            failure_type = "unknown"
        return RecoveryFeedback(
            source="text-feedback",
            failure_type=failure_type,
            summary=text,
        )

    cex = feedback.get("counterexample")
    failure_type = str(feedback.get("failure_type") or feedback.get("type") or "")
    if not failure_type:
        if cex or feedback.get("mismatch"):
            failure_type = "cex"
        else:
            failure_type = "unknown"
    return RecoveryFeedback(
        source=str(feedback.get("source") or "tool-feedback"),
        failure_type=failure_type,
        target=str(feedback.get("target") or ""),
        summary=str(feedback.get("summary") or feedback.get("reason") or ""),
        counterexample=cex if isinstance(cex, dict) else None,
        mismatch=feedback.get("mismatch")
        if isinstance(feedback.get("mismatch"), dict) else None,
    )


def _word_by_compact(words: tuple[RecoveredWord, ...]) -> dict[str, RecoveredWord]:
    out: dict[str, RecoveredWord] = {}
    for word in words:
        out[word.name] = word
        out[word.compact()] = word
    return out


def _cut_output_name(cut: SupportCut) -> str:
    return cut.target.split("[", 1)[0]


def _support_bit_count(words: tuple[str, ...],
                       input_words: dict[str, RecoveredWord]) -> int:
    count = 0
    for ref in words:
        name = ref.split("[", 1)[0]
        word = input_words.get(name) or input_words.get(ref)
        count += word.width if word else 1
    return count


def _has_scalar_control(words: tuple[str, ...],
                        input_words: dict[str, RecoveredWord]) -> bool:
    widths = []
    for ref in words:
        name = ref.split("[", 1)[0]
        word = input_words.get(name) or input_words.get(ref)
        widths.append(word.width if word else 1)
    return any(width == 1 for width in widths) and any(width > 1 for width in widths)


def _role(word: RecoveredWord, cut: SupportCut,
          input_words: dict[str, RecoveredWord]) -> str:
    if word.width == 1:
        return "predicate/control"
    if _has_scalar_control(cut.support_words, input_words):
        return "control/mux datapath"
    if word.width >= 8 or cut.cone_size >= 200:
        return "wide datapath"
    if len(cut.support_words) >= 2:
        return "shared arithmetic"
    return "unknown"


def _recommendations(cluster_name: str, outputs: tuple[str, ...],
                     support_words: tuple[str, ...], role: str,
                     support_bits: int, cone_nodes: int
                     ) -> tuple[MethodRecommendation, ...]:
    target = outputs[0] if len(outputs) == 1 else cluster_name
    output_ref = outputs[0]
    support_inputs = [w.split("[", 1)[0] for w in support_words]
    recs: list[MethodRecommendation] = []
    recs.append(MethodRecommendation(
        tool="probe_target",
        target=target,
        reason="first profile support, cone size, sample activity, and control hints without formula search",
        priority=1,
        suggested_parameters={
            "target": output_ref,
            "inputs": support_inputs,
            "pattern_num": 32,
            "detail": True,
        },
    ))
    recs.append(MethodRecommendation(
        tool="validate_expr",
        target=target,
        reason="if probing or manual reasoning yields an explicit formula, verify that hypothesis instead of searching",
        priority=2,
        suggested_parameters={
            "target": output_ref,
            "expression": "<explicit hypothesis>",
            "inputs": support_inputs,
            "exhaustive": "auto",
        },
    ))
    recs.append(MethodRecommendation(
        tool="list_candidates",
        target=target,
        reason="review accepted predicates and branch evidence before choosing the next target or assembly step",
        priority=2,
        suggested_parameters={},
    ))
    if support_bits <= EXHAUSTIVE_MAX_BITS:
        recs.append(MethodRecommendation(
            tool="infer_native_expr",
            target=target,
            reason="small support can be exhaustively validated with native TABLE-I candidates",
            priority=2,
            suggested_parameters={
                "target": output_ref,
                "inputs": support_inputs,
                "pattern_num": 512,
                "validation_num": 2048,
            },
        ))
    if role in {"wide datapath", "shared arithmetic"}:
        recs.append(MethodRecommendation(
            tool="print_adder_stats",
            target=target,
            reason="wide/shared arithmetic should be checked for adder trees before formula recovery",
            priority=1,
            suggested_parameters={"detail": True},
        ))
        recs.append(MethodRecommendation(
            tool="fit_template",
            target=target,
            reason="try restricted arithmetic templates after support and structure are known",
            priority=2,
            suggested_parameters={
                "target": output_ref,
                "inputs": support_inputs,
                "templates": "linear,product",
            },
        ))
        recs.append(MethodRecommendation(
            tool="check_polynomial_lowbits",
            target=target,
            reason="collect bounded low-bit polynomial evidence without expanding a full search",
            priority=3,
            suggested_parameters={
                "target": output_ref,
                "inputs": support_inputs,
                "max_low_bits": 8,
            },
        ))
    if role == "predicate/control":
        recs.append(MethodRecommendation(
            tool="simulate",
            target=target,
            reason="single-bit outputs need directed patterns to distinguish comparator, equality, and control behavior",
            priority=1,
            suggested_parameters={"pattern_num": 16, "watch": list(outputs)},
        ))
        recs.append(MethodRecommendation(
            tool="fit_template",
            target=target,
            reason=(
                "predicate outputs should prefer comparator/template candidates, "
                "including signed word comparators and signed affine-difference comparators"),
            priority=2,
            suggested_parameters={
                "target": output_ref,
                "inputs": support_inputs,
                "templates": "comparator,signed-comparator,signed-affine,linear",
            },
        ))
    if role == "control/mux datapath":
        recs.append(MethodRecommendation(
            tool="split_control_cases",
            target=target,
            reason="scalar support suggests mux/control decomposition before expression fitting",
            priority=1,
            suggested_parameters={
                "target": output_ref,
                "inputs": support_inputs,
                "detail": True,
            },
        ))
        recs.append(MethodRecommendation(
            tool="fit_template",
            target=target,
            reason=(
                "after each full control assignment is identified, try branch-local "
                "signed comparator or signed affine-difference templates with fixed_inputs"),
            priority=2,
            suggested_parameters={
                "target": output_ref,
                "inputs": support_inputs,
                "fixed_inputs": "<one full control assignment>",
                "templates": "comparator,signed-comparator,signed-affine",
            },
        ))
        recs.append(MethodRecommendation(
            tool="combine_case_expr",
            target=target,
            reason="after branch expressions are validated, combine them into one full conditional candidate",
            priority=3,
            suggested_parameters={
                "target": output_ref,
                "controls": [w.split("[", 1)[0] for w in support_words],
                "cases": [{"fixed_inputs": {}, "expression": "<branch expression>"}],
                "inputs": support_inputs,
            },
        ))
    if cone_nodes > 400 or not recs:
        recs.append(MethodRecommendation(
            tool="find_cone",
            target=target,
            reason="large or unclear cone should be inspected before full RTL assembly",
            priority=1,
            suggested_parameters={
                "signals": list(outputs),
                "direction": "backward",
                "detail": True,
            },
        ))
    return tuple(sorted(recs, key=lambda rec: rec.priority))


def _build_clusters(analysis, circuit: Circuit) -> tuple[OutputCluster, ...]:
    output_words = _word_by_compact(analysis.output_words)
    input_words = _word_by_compact(analysis.input_words)
    groups: dict[tuple[tuple[str, ...], str], list[SupportCut]] = {}
    for cut in analysis.support_cuts:
        word = output_words.get(_cut_output_name(cut)) or output_words.get(cut.target)
        if word is None:
            continue
        role = _role(word, cut, input_words)
        groups.setdefault((tuple(cut.support_words), role), []).append(cut)

    clusters: list[OutputCluster] = []
    for idx, ((support, role), cuts) in enumerate(groups.items(), 1):
        outputs = tuple(_cut_output_name(cut) for cut in cuts)
        cone_nodes = sum(cut.cone_size for cut in cuts)
        support_bits = _support_bit_count(support, input_words)
        evidence = [
            f"support words: {', '.join(support) or '(none)'}",
            f"support bits: {support_bits}",
            f"total cone nodes: {cone_nodes}",
            f"gate histogram keys: {', '.join(sorted(circuit.gate_histogram())[:8]) or '(none)'}",
        ]
        if len(outputs) > 1:
            evidence.append("outputs grouped by identical support and role")
        if any((input_words.get(w.split('[', 1)[0]) or input_words.get(w) or RecoveredWord(w, (), '', 1)).width == 1
               for w in support):
            evidence.append("scalar support present; treat as possible control")
        name = f"cluster{idx}"
        clusters.append(OutputCluster(
            name=name,
            outputs=outputs,
            support_words=support,
            cone_nodes=cone_nodes,
            suspected_role=role,
            evidence=tuple(evidence),
            recommendations=_recommendations(
                name, outputs, support, role, support_bits, cone_nodes),
        ))
    return tuple(clusters)


def _steps_from_clusters(clusters: tuple[OutputCluster, ...]) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    seq = 1
    for cluster in clusters:
        for rec in cluster.recommendations:
            profile = get_tool_profile(rec.tool)
            steps.append(PlanStep(
                step_id=f"S{seq}",
                target=rec.target,
                tool=rec.tool,
                method=cluster.suspected_role,
                parameters=rec.suggested_parameters,
                expected_evidence=rec.reason,
                acceptance=(
                    "produce structural evidence or an exhaustive/sample/CEC "
                    "validated local candidate before final RTL assembly"),
                effort=profile.effort,
                search_space=profile.search_space,
                risk=profile.risk,
                prerequisites=profile.prerequisites,
                why_cheapest=profile.guidance,
            ))
            seq += 1
    return tuple(steps)


def _feedback_revisions(feedback: RecoveryFeedback | None) -> tuple[PlanRevision, ...]:
    if feedback is None:
        return ()
    target = feedback.target or "(feedback target)"
    if feedback.failure_type in {"cex", "counterexample"}:
        return (PlanRevision(
            original_step=f"candidate for {target}",
            problem="CEC/counterexample mismatch",
            revised_step=(
                "summarize mismatching output and input word values, then "
                "probe the activated cone/control path before retrying a narrow local tool"),
            reason="sample-valid candidates can still miss CEX-specific control or corner cases",
        ),)
    if feedback.failure_type in {"no-candidate", "failed-validation"}:
        return (PlanRevision(
            original_step=f"local candidate attempt for {target}",
            problem="no candidate passed validation",
            revised_step=(
                "shrink target to bit/part-select, make inputs explicit, run "
                "probe_target/find_cone(detail=True), then try fit_template or "
                "infer_native_expr; use infer_pysr_expr(force=True) only after that narrowed retry fails"),
            reason="the original boundary or method class is probably too broad",
        ),)
    if feedback.failure_type == "timeout":
        return (PlanRevision(
            original_step=f"long-running step for {target}",
            problem="timeout",
            revised_step="split the cluster or reduce target width before retrying",
            reason="timeout is a planning failure, not evidence of successful understanding",
        ),)
    if feedback.failure_type == "control-failed":
        return (PlanRevision(
            original_step=f"control recovery for {target}",
            problem="control/mux hypothesis failed",
            revised_step=(
                "run split_control_cases, validate branch expressions with "
                "validate_expr(fixed_inputs=...), then combine them with combine_case_expr"),
            reason="mixed datapath/control behavior should not be fit as one formula",
        ),)
    return (PlanRevision(
        original_step=f"previous attempt for {target}",
        problem=feedback.summary or feedback.failure_type,
        revised_step="re-run local cone/support analysis and choose a narrower target",
        reason="feedback did not map to a specific known failure class",
    ),)


def plan_recovery(circuit: Circuit, outputs: list[str] | None = None,
                  feedback: str | dict[str, Any] | None = None) -> RecoveryPlan:
    """Build an agent-facing iterative recovery plan."""
    analysis = analyze_words(circuit, outputs=outputs)
    parsed_feedback = _feedback_from_any(feedback)
    clusters = _build_clusters(analysis, circuit)
    steps = _steps_from_clusters(clusters)
    warnings = [
        "Do not call recover_expression; it is deprecated and returns a guardrail.",
        "Do not call infer_expression; it is removed from MCP because it bypasses this workflow.",
        "Do not use full-module baseline recovery; use assemble_rtl only after accepted local candidates exist.",
        "If you already have a formula hypothesis, call validate_expr before any search tool.",
        "If a target is controlled, validate branches with fixed_inputs and combine cases before assembly.",
        "On failure, shrink the target or boundary before trying heavier tools.",
    ]
    if parsed_feedback:
        warnings.append(
            "Feedback was provided; revise the next local step before trying global RTL assembly.")
    sequence = [
        "run the listed plan steps per cluster/target",
        "choose the cheapest sufficient tool: probe/verify before light templates, medium enumeration, or heavy search",
        "after each failed or inconclusive step, summarize failure and call plan_recovery(feedback=...)",
        "only after local candidates are accepted, run assemble_rtl or verify_rtl_candidate",
    ]
    return RecoveryPlan(
        circuit_name=circuit.name,
        word_analysis=analysis,
        clusters=clusters,
        steps=steps,
        recommended_sequence=tuple(sequence),
        feedback=parsed_feedback,
        revisions=_feedback_revisions(parsed_feedback),
        warnings=tuple(warnings),
    )
