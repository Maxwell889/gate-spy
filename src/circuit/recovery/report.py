"""Human-readable formatting for RTL recovery reports."""

from __future__ import annotations

import json

from .models import RecoveryPlan, RecoveryReport, VerificationResult, WordAnalysis


def format_word_analysis(analysis: WordAnalysis, *, detail: bool = False) -> str:
    lines = [f"Word analysis — {analysis.circuit_name}"]
    lines.append("    inputs : " + _word_list(analysis.input_words))
    lines.append("    outputs: " + _word_list(analysis.output_words))
    lines.append("")
    lines.append("== Support cuts ==")
    for cut in analysis.support_cuts:
        lines.append(
            f"  {cut.target}: support={', '.join(cut.support_words) or '(none)'} "
            f"cone_nodes={cut.cone_size}")
        if detail:
            for item in cut.evidence:
                lines.append(f"      - {item}")
    if detail:
        lines.append("")
        lines.append("== Word evidence ==")
        for word in list(analysis.input_words) + list(analysis.output_words):
            lines.append(
                f"  {word.compact()} {word.direction} confidence={word.confidence:.2f}")
            for item in word.evidence:
                lines.append(f"      - {item}")
    return "\n".join(lines)


def _word_list(words) -> str:
    if not words:
        return "(none)"
    return ", ".join(word.compact() for word in words)


def _format_verification(v: VerificationResult | None) -> str:
    if v is None:
        return "not-run"
    msg = v.status
    if v.reason:
        msg += f" ({v.reason})"
    if v.elapsed:
        msg += f" {v.elapsed:.1f}s"
    return msg


def format_recovery_report(report: RecoveryReport, *,
                           detail: bool = False,
                           format: str = "text") -> str:
    if format == "json":
        return json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if format != "text":
        raise ValueError("format must be 'text' or 'json'")

    lines = [f"RTL recovery report — {report.circuit_name}"]
    if report.verification is not None or report.candidate_rtl:
        lines.append(f"    overall verification: {_format_verification(report.verification)}")
    if report.unrecovered:
        lines.append(f"    unrecovered outputs : {', '.join(report.unrecovered)}")
    lines.append("")
    lines.append("== Word/support evidence ==")
    lines.extend(format_word_analysis(report.word_analysis, detail=detail).splitlines()[1:])

    lines.append("")
    lines.append("== Candidate expressions ==")
    if not report.candidates:
        lines.append("  No candidate passed validation.")
    else:
        shown = report.candidates if detail else report.candidates[:24]
        for cand in shown:
            cond = f" when {cand.case_condition}" if cand.case_condition else ""
            cid = f"{cand.candidate_id} " if cand.candidate_id else ""
            lines.append(
                f"  [{cid}{cand.verification.status}] {cand.target}{cond} = "
                f"{cand.expression}")
            lines.append(
                f"      method={cand.method} cost={cand.cost} "
                f"width={cand.width} inputs={', '.join(cand.inputs) or '(none)'}")
            if cand.verification.reason:
                lines.append(f"      reason={cand.verification.reason}")
            if detail:
                for item in cand.evidence:
                    lines.append(f"      - {item}")
        if len(shown) < len(report.candidates):
            lines.append(
                f"  (+{len(report.candidates) - len(shown)} more candidate rows)")

    if report.candidate_rtl:
        lines.append("")
        lines.append("== Candidate RTL ==")
        lines.append("```verilog")
        lines.append(report.candidate_rtl.rstrip())
        lines.append("```")

    if report.notes:
        lines.append("")
        lines.append("== Notes ==")
        for note in report.notes:
            lines.append(f"  - {note}")
    if report.verification and report.verification.counterexample:
        lines.append("")
        lines.append("== CEC counterexample ==")
        lines.append(json.dumps(
            report.verification.counterexample, indent=2, sort_keys=True))
    return "\n".join(lines)


def format_recovery_plan(plan: RecoveryPlan, *,
                         detail: bool = False,
                         format: str = "text") -> str:
    """Format an agent-facing recovery plan."""
    if format == "json":
        return json.dumps(plan.to_dict(), indent=2, sort_keys=True)
    if format != "text":
        raise ValueError("format must be 'text' or 'json'")

    title = "RTL recovery plan"
    if plan.feedback:
        title = "RTL recovery plan — revised from feedback"
    lines = [f"{title} — {plan.circuit_name}"]
    lines.append("    inputs : " + _word_list(plan.word_analysis.input_words))
    lines.append("    outputs: " + _word_list(plan.word_analysis.output_words))
    if plan.feedback:
        lines.append(
            f"    feedback: {plan.feedback.failure_type}"
            + (f" target={plan.feedback.target}" if plan.feedback.target else "")
        )
        if plan.feedback.summary:
            lines.append(f"              {plan.feedback.summary}")
    lines.append("")
    lines.append("== Guardrails ==")
    for warning in plan.warnings:
        lines.append(f"  - {warning}")

    lines.append("")
    lines.append("== Output clusters ==")
    if not plan.clusters:
        lines.append("  (none)")
    for cluster in plan.clusters:
        lines.append(
            f"  {cluster.name}: outputs={', '.join(cluster.outputs)} "
            f"role={cluster.suspected_role}")
        lines.append(
            f"      support={', '.join(cluster.support_words) or '(none)'} "
            f"cone_nodes={cluster.cone_nodes}")
        if detail:
            for item in cluster.evidence:
                lines.append(f"      - {item}")

    lines.append("")
    lines.append("== Plan steps ==")
    if not plan.steps:
        lines.append("  No plan steps were generated.")
    for step in plan.steps:
        lines.append(
            f"  {step.step_id}. [{step.status}] {step.tool} -> {step.target}")
        lines.append(f"      method : {step.method}")
        if step.effort or step.search_space or step.risk:
            lines.append(
                f"      effort : {step.effort or 'unknown'}; "
                f"search={step.search_space or 'unknown'}; "
                f"risk={step.risk or 'unknown'}")
        if step.prerequisites:
            lines.append(
                f"      prereq : {', '.join(step.prerequisites)}")
        if step.parameters:
            params = ", ".join(
                f"{key}={value!r}" for key, value in step.parameters.items())
            lines.append(f"      params : {params}")
        lines.append(f"      why    : {step.expected_evidence}")
        if step.why_cheapest:
            lines.append(f"      cheap  : {step.why_cheapest}")
        lines.append(f"      accept : {step.acceptance}")

    if plan.revisions:
        lines.append("")
        lines.append("== Revisions from feedback ==")
        for revision in plan.revisions:
            lines.append(f"  Problem : {revision.problem}")
            lines.append(f"  Replace : {revision.original_step}")
            lines.append(f"  With    : {revision.revised_step}")
            lines.append(f"  Reason  : {revision.reason}")

    lines.append("")
    lines.append("== Required loop ==")
    for item in plan.recommended_sequence:
        lines.append(f"  - {item}")
    return "\n".join(lines)
