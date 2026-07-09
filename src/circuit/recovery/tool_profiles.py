"""Agent-facing effort metadata for recovery tools."""

from __future__ import annotations

from .models import ToolProfile


TOOL_PROFILES: dict[str, ToolProfile] = {
    "probe_target": ToolProfile(
        tool="probe_target",
        effort="probe",
        search_space="none",
        risk="low",
        guidance="Use first to inspect target behavior; it cannot produce formulas.",
    ),
    "simulate": ToolProfile(
        tool="simulate",
        effort="probe",
        search_space="none",
        risk="low",
        guidance="Use for directed evidence and hypothesis formation.",
    ),
    "validate_expr": ToolProfile(
        tool="validate_expr",
        effort="verify",
        search_space="none",
        risk="low",
        requires_hypothesis=True,
        prerequisites=("explicit expression hypothesis",),
        guidance="Use when the agent already has a formula; verifies and caches it without searching.",
    ),
    "list_candidates": ToolProfile(
        tool="list_candidates",
        effort="probe",
        search_space="none",
        risk="low",
        guidance="Use to review accepted global candidates, branch evidence, and failures before deciding next steps.",
    ),
    "combine_case_expr": ToolProfile(
        tool="combine_case_expr",
        effort="verify",
        search_space="none",
        risk="low",
        requires_hypothesis=True,
        prerequisites=("validated or hypothesized branch expressions",),
        guidance="Use after split_control_cases and branch validation to produce a full conditional candidate.",
    ),
    "fit_template": ToolProfile(
        tool="fit_template",
        effort="light",
        search_space="fixed linear/product/comparator templates",
        risk="low",
        prerequisites=("known target/support boundary",),
        guidance="Use for simple arithmetic or comparator hypotheses before native enumeration.",
    ),
    "split_control_cases": ToolProfile(
        tool="split_control_cases",
        effort="light",
        search_space="case profiling only",
        risk="low",
        prerequisites=("possible scalar control signal",),
        guidance="Use before fitting one formula across mixed control/datapath behavior.",
    ),
    "infer_native_expr": ToolProfile(
        tool="infer_native_expr",
        effort="medium",
        search_space="bounded native TABLE-I candidate enumeration",
        risk="medium",
        prerequisites=("probe_target or explicit support boundary",),
        fallback_after=("validate_expr", "fit_template", "split_control_cases"),
        guidance="Use after cheaper verification/template steps are insufficient.",
    ),
    "check_polynomial_lowbits": ToolProfile(
        tool="check_polynomial_lowbits",
        effort="medium",
        search_space="bounded low-bit polynomial rewriting",
        risk="medium",
        prerequisites=("small enough support/cone for polynomial cap",),
        guidance="Use for arithmetic evidence; respect skip reasons instead of raising caps blindly.",
    ),
    "infer_pysr_expr": ToolProfile(
        tool="infer_pysr_expr",
        effort="heavy",
        search_space="external symbolic-regression search",
        risk="high",
        prerequisites=("narrowed target", "cheaper methods failed"),
        fallback_after=("validate_expr", "fit_template", "infer_native_expr"),
        guidance="Requires force=True; use only as an explicit fallback.",
    ),
    "assemble_rtl": ToolProfile(
        tool="assemble_rtl",
        effort="verify",
        search_space="none",
        risk="low",
        prerequisites=("accepted local candidates in session cache",),
        guidance="Final assembly only; never recovers missing expressions.",
    ),
    "verify_rtl_candidate": ToolProfile(
        tool="verify_rtl_candidate",
        effort="verify",
        search_space="CEC",
        risk="low",
        prerequisites=("candidate RTL text",),
        guidance="Use for whole-module proof/counterexample after local hypotheses exist.",
    ),
    "print_adder_stats": ToolProfile(
        tool="print_adder_stats",
        effort="probe",
        search_space="structural scan",
        risk="low",
        guidance="Use to identify adder/compressor structure before expression attempts.",
    ),
    "find_cone": ToolProfile(
        tool="find_cone",
        effort="probe",
        search_space="graph traversal",
        risk="low",
        guidance="Use to inspect boundaries and shrink targets.",
    ),
}


def get_tool_profile(tool: str) -> ToolProfile:
    """Return a profile for *tool*, falling back to a conservative unknown one."""
    return TOOL_PROFILES.get(tool, ToolProfile(
        tool=tool,
        effort="unknown",
        search_space="unknown",
        risk="medium",
        guidance="No profile registered; treat as non-default until inspected.",
    ))
