"""Tests for analysis-driven RTL recovery tools."""

from pathlib import Path
import shutil

import pytest

from scripts.cost import calc_verilog_cost
from scripts.recovery_eval import _auto_recover_with_session
from scripts.yosys_cec import run_cec
from src.circuit import Circuit
from src.circuit.recovery.models import ExpressionCandidate, VerificationResult
from src.circuit.recovery.pipeline import emit_candidate_rtl
from src.circuit.symbolic.ast import Binary, Var
from src.circuit.symbolic.search import Candidate
from src.session import CircuitSession


def _mux_session() -> CircuitSession:
    c = Circuit.from_string(
        "module top(s,a,b,y); input [1:0] a,b; input s; output [1:0] y; "
        "wire [1:0] a,b,y; wire s; "
        "wire ns,t0,t1,t2,t3; "
        "NOT g0 ( .A(s), .Y(ns) ); "
        "AND g1 ( .A(a[0]), .B(ns), .Y(t0) ); "
        "AND g2 ( .A(b[0]), .B(s), .Y(t1) ); "
        "OR g3 ( .A(t0), .B(t1), .Y(y[0]) ); "
        "AND g4 ( .A(a[1]), .B(ns), .Y(t2) ); "
        "AND g5 ( .A(b[1]), .B(s), .Y(t3) ); "
        "OR g6 ( .A(t2), .B(t3), .Y(y[1]) ); "
        "endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    return session


def test_analyze_words_reports_support():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = session.analyze_words(detail=True)
    assert "Word analysis" in report
    assert "IN1[16]" in report
    assert "Out[32]" in report
    assert "support=IN1[16], IN2[16]" in report


def test_probe_target_reports_profile_without_candidate_formulas():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    report = session.probe_target(
        "out1", inputs=["in1", "in2", "in3"], detail=True)
    assert "Target probe" in report
    assert "support bits : 12" in report
    assert "cone nodes" in report
    assert "Sample profile" in report
    assert "Candidate expressions" not in report
    assert "out1 =" not in report


def test_fit_template_recovers_product_and_comparator():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    product = session.fit_template(
        "Out", inputs=["IN1", "IN2"], pattern_num=128, validation_num=256)
    assert "Out = IN1 * IN2" in product
    assert "multiplication-template-fitting" in product
    assert "sample-exact" in product
    assert "PySR" not in product

    c = Circuit.from_string(
        "module top(a,y); input [1:0] a; output y; "
        "wire [1:0] a; wire y; "
        "AND g0 ( .A(a[0]), .B(a[1]), .Y(y) ); "
        "endmodule"
    )
    cmp_session = CircuitSession()
    cmp_session.circuit = c
    comparator = cmp_session.fit_template(
        "y", inputs=["a"], pattern_num=16, validation_num=16)
    assert "comparator-boundary-fitting" in comparator
    assert "exhaustive-exact" in comparator


def test_validate_expr_verifies_manual_hypothesis_and_caches_candidate():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    report = session.validate_expr(
        "out1", "in1 + in2 + in3", inputs=["in1", "in2", "in3"])
    assert "manual-hypothesis" in report
    assert "out1 = (in1 + in2) + in3" in report
    assert "exhaustive-exact" in report
    assert "C1" in report
    assert "no search was run" in report
    assert "overall verification: not-run" not in report

    assembled = session.assemble_rtl()
    assert "overall verification:" in assembled
    assert "Candidate RTL" in assembled
    assert "assign out1 = (in1 + in2) + in3;" in assembled


def test_validate_expr_accepts_signed_cast_syntax():
    c = Circuit.from_string(
        "module top(a,y); input a; output y; wire a,y; "
        "BUF g0 ( .A(a), .Y(y) ); endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    report = session.validate_expr("y", "$signed(a) < 1'sd0", inputs=["a"])
    assert "manual-hypothesis" in report
    assert "$signed(a) < 1'sd0" in report
    assert "exhaustive-exact" in report


def test_validate_expr_reports_width_hint_for_test15_mac_carry():
    session = CircuitSession()
    session.load("examples/testcase/test15/top_primitive.v")
    failed = session.validate_expr(
        "out1", "in1 * in2 + in3",
        pattern_num=64,
        validation_num=64,
        seed=2,
        detail=True,
    )
    assert "[F" in failed
    assert "expr_width=32 < target_width=33" in failed
    assert "try explicit zero/sign extension or a wider literal" in failed

    widened = session.validate_expr(
        "out1", "33'd0 + (in1 * in2) + in3",
        pattern_num=64,
        validation_num=64,
        seed=2,
    )
    assert "out1 = (33'd0 + (in1 * in2)) + in3" in widened
    assert "sample-exact" in widened


def test_candidate_rtl_preserves_original_test13_port_order():
    circuit = Circuit.from_file("examples/testcase/test13/top_primitive.v")
    chosen = {
        f"out{i}": ExpressionCandidate(
            target=f"out{i}",
            expression="1'd0",
            method="test",
            cost=0,
            width=1,
            inputs=(),
            verification=VerificationResult("sample-exact"),
        )
        for i in range(1, 6)
    }
    rtl = emit_candidate_rtl(circuit, chosen)
    assert rtl.splitlines()[0] == (
        "module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, "
        "in10, in11, in12, in13, in14, in15, out1, out2, out3, out4, out5);"
    )


def test_cec_guard_rejects_same_port_set_wrong_order_before_abc():
    session = CircuitSession()
    session.load("examples/testcase/test13/top_primitive.v")
    ports = list(session.circuit.module_ports)
    wrong_ports = [ports[0], ports[2], ports[1], *ports[3:]]
    candidate = (
        f"module top({', '.join(wrong_ports)});\n"
        "  input [11:0] in1, in2, in7;\n"
        "  input in3, in4, in5, in6, in8, in9, in10, in11, in12, in13, in14, in15;\n"
        "  output out1, out2, out3, out4, out5;\n"
        "endmodule\n"
    )
    result = session._verify_candidate_code(candidate, timeout_s=1)
    assert result.status == "failed"
    assert "port_order_mismatch" in result.reason


def test_fit_template_recovers_test13_signed_predicates():
    session = CircuitSession()
    session.load("examples/testcase/test13/top_primitive.v")
    out4 = session.fit_template(
        "out4",
        inputs=["in2", "in7"],
        exhaustive="never",
        pattern_num=128,
        validation_num=256,
    )
    assert "out4 = $signed(in2) < $signed(in7)" in out4
    assert "signed-comparator-template-fitting" in out4
    out5 = session.fit_template(
        "out5",
        inputs=["in1", "in2"],
        exhaustive="never",
        pattern_num=128,
        validation_num=256,
    )
    assert "out5 = $signed(in1) < $signed(in2)" in out5


def test_fit_template_recovers_test13_branch_signed_affine_comparator():
    session = CircuitSession()
    session.load("examples/testcase/test13/top_primitive.v")
    report = session.fit_template(
        "out1",
        inputs=["in1", "in2", "in7"],
        fixed_inputs={"in3": 0, "in5": 0},
        exhaustive="never",
        pattern_num=128,
        validation_num=256,
    )
    assert "B1" in report
    assert "signed-affine-difference-comparator-template-fitting" in report
    assert "$signed({in1[11], in1}) - $signed({in2[11], in2})" in report
    assert "branch-conditioned template candidate is evidence only" in report


def test_fit_template_warns_on_partial_test13_control_assignment():
    session = CircuitSession()
    session.load("examples/testcase/test13/top_primitive.v")
    report = session.fit_template(
        "out1",
        inputs=["in1", "in2", "in7"],
        fixed_inputs={"in5": 1},
        exhaustive="never",
        pattern_num=64,
        validation_num=128,
    )
    assert "diagnostic=partial_control_assignment_suspected" in report
    assert "scalar support still varies: in3" in report
    assert "not a complete branch" in report
    assert "before escalating to native search or PySR" in report


def test_validate_expr_fixed_inputs_validates_branch_without_assembly_cache():
    session = _mux_session()
    report = session.validate_expr(
        "y",
        "a",
        fixed_inputs={"s": 0},
        detail=True,
    )
    assert "manual-hypothesis" in report
    assert "y when s=0 = a" in report
    assert "exhaustive-exact" in report
    assert "branch-conditioned candidate is evidence only" in report
    assert "C1" not in report

    assembled = session.assemble_rtl()
    assert "unrecovered outputs : y" in assembled
    assert "Candidate RTL" not in assembled


def test_validate_expr_expr_only_slice_does_not_cache_test29_hypothesis():
    session = CircuitSession()
    session.load("examples/testcase/test29/top_primitive.v")
    fixed = {
        "in3": 1,
        "in5": 1,
        "in6": 1,
        "in7": 1,
        "in8": 1,
        "in9": 1,
        "in10": 1,
        "in12": 1,
    }
    sliced = session.validate_expr(
        "out1",
        "in2 - 26'd8",
        inputs=["in2"],
        fixed_inputs=fixed,
        validate_scope="expr_only",
        exhaustive="never",
        pattern_num=32,
        validation_num=64,
        detail=True,
    )
    assert "sample_scope=expr-only-slice" in sliced
    assert "slice-sample-exact" in sliced
    assert "omitted support inputs are not driven" in sliced
    assert "C1" not in sliced
    assert "B1" not in sliced
    assert session.list_candidates().strip() == "Candidate cache\n  (empty)"

    full = session.validate_expr(
        "out1",
        "in2 - 26'd8",
        inputs=["in2"],
        fixed_inputs=fixed,
        validate_scope="support",
        exhaustive="never",
        pattern_num=32,
        validation_num=64,
        detail=True,
    )
    assert "sample_scope=full-support" in full
    assert "omitted_support=" in full
    assert "full-support validation samples every non-fixed primary input" in full
    assert "failed-" in full
    assert "branch-conditioned candidate is evidence only" not in full


def test_validate_expr_auto_adds_expression_primary_inputs():
    session = _mux_session()
    report = session.validate_expr("y", "s ? b : a", inputs=["a", "b"])
    assert "manual-hypothesis" in report
    assert "exhaustive-exact" in report
    assert "inputs=a[2], b[2], s" in report


def test_list_candidates_tracks_global_branch_and_failed_candidates():
    session = _mux_session()
    session.validate_expr("y", "a", fixed_inputs={"s": 0}, detail=True)
    session.validate_expr("y", "s", inputs=["s"], detail=True)
    session.validate_expr("y", "s", inputs=["s"], detail=True)
    report = session.list_candidates()
    assert "B1: y when s=0 = a" in report
    assert "F1: y = s" in report
    assert "Branch-only evidence" in report
    assert "Only C# accepted-global candidates are consumed by assemble_rtl" in report


def test_combine_case_expr_validates_and_caches_full_conditional():
    session = _mux_session()
    branch0 = session.validate_expr("y", "a", fixed_inputs={"s": 0})
    branch1 = session.validate_expr("y", "b", fixed_inputs={"s": 1})
    assert "B1" in branch0
    assert "B2" in branch1
    report = session.combine_case_expr(
        "y",
        controls=["s"],
        inputs=["s", "a", "b"],
        cases=[
            {"fixed_inputs": {"s": 0}, "expression": "a"},
            {"fixed_inputs": {"s": 1}, "expression": "b"},
        ],
    )
    assert "combined 2 branch expression" in report
    assert "exhaustive-exact" in report
    assert "C1" in report
    assembled = session.assemble_rtl()
    assert "Candidate RTL" in assembled
    assert "assign y =" in assembled


def test_validate_expr_can_reference_cached_candidate_by_target_name():
    c = Circuit.from_string(
        "module top(a,b,y,z); input a,b; output y,z; wire a,b,y,z; "
        "AND g0 ( .A(a), .B(b), .Y(y) ); "
        "BUF g1 ( .A(y), .Y(z) ); endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    with pytest.raises(ValueError, match="accepted candidates"):
        session.validate_expr("z", "y")
    first = session.validate_expr("y", "a & b", inputs=["a", "b"])
    assert "C1" in first
    second = session.validate_expr("z", "y")
    assert "z = a & b" in second
    assert "C2" in second


def test_infer_native_expr_recovers_and_and_mux_without_pysr():
    c = Circuit.from_string(
        "module top(a,b,y); input a,b; output y; wire a,b,y; "
        "AND g0 ( .A(a), .B(b), .Y(y) ); endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    and_report = session.infer_native_expr(
        "y", inputs=["a", "b"], mode="bit")
    assert "y = a & b" in and_report
    assert "exhaustive-exact" in and_report
    assert "PySR" not in and_report

    mux_report = _mux_session().infer_native_expr(
        "y", inputs=["s", "a", "b"], mode="mixed")
    assert "?" in mux_report
    assert "exhaustive-exact" in mux_report
    assert "PySR" not in mux_report


def test_infer_native_expr_defers_wide_default_search():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = session.infer_native_expr("Out", inputs=["IN1", "IN2"])
    assert "Native expression inference deferred" in report
    assert "support_bits=32" in report


def test_infer_native_expr_prunes_large_exhaustive_candidate_pool_for_test14():
    session = CircuitSession()
    session.load("examples/testcase/test14/top_primitive.v")
    report = session.infer_native_expr(
        "out1",
        inputs=["in1", "in2", "in3"],
        pattern_num=16,
        validation_num=16,
    )
    assert "native TABLE-I generator produced" in report
    assert "native exhaustive verification pruned" in report
    assert "directed prefilter survivors=" in report


def test_check_polynomial_lowbits_returns_bounded_skip_reason():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = session.check_polynomial_lowbits(
        "Out", inputs=["IN1", "IN2"], max_low_bits=8)
    assert "polynomial skipped: support has 32 bit(s), cap is 24" in report
    assert "No candidate passed validation" in report


def test_split_control_cases_identifies_scalar_control():
    report = _mux_session().split_control_cases(
        "y", inputs=["s", "a", "b"], detail=True)
    assert "Control split probe" in report
    assert "controls     : s" in report
    assert "Marginal case profile" in report
    assert "s=0" in report
    assert "s=1" in report
    assert "Branch next steps" in report


def test_split_control_cases_reports_control_combinations():
    c = Circuit.from_string(
        "module top(s,t,a,b,y); input s,t,a,b; output y; wire s,t,a,b,y; "
        "wire sa,tb; "
        "AND g0 ( .A(s), .B(a), .Y(sa) ); "
        "AND g1 ( .A(t), .B(b), .Y(tb) ); "
        "OR g2 ( .A(sa), .B(tb), .Y(y) ); "
        "endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    report = session.split_control_cases(
        "y", controls=["s", "t"], inputs=["s", "t", "a", "b"], detail=True)
    assert "Control combination profile" in report
    assert "assignment coverage: balanced full-control assignments" in report
    assert "s=0, t=0" in report
    assert "samples=4" in report
    assert "fixed_inputs includes every listed control" in report
    assert "do not infer a branch from one marginal control row" in report


def test_influence_profile_reports_branch_local_zero_and_nonzero_deltas():
    report = _mux_session().influence_profile(
        "y",
        fixed_inputs={"s": 0},
        inputs=["s", "a", "b"],
        base_inputs={"a": 0, "b": 0},
        detail=True,
    )
    assert "Influence profile" in report
    assert "fixed_inputs : s=0" in report
    assert "a[0]: signed_delta={1}" in report
    assert "a[1]: signed_delta={-2} unsigned_delta={2}" in report
    assert "bits : b[0], b[1]" in report
    assert "words: b" in report


def test_influence_profile_default_bases_expose_test15_product_interaction():
    session = CircuitSession()
    session.load("examples/testcase/test15/top_primitive.v")
    report = session.influence_profile(
        "out3",
        fixed_inputs={"in4": 0, "in5": 0, "in6": 1},
    )
    assert "base samples : 2" in report
    assert "in1[0]:" in report
    assert "in10[0]:" in report
    assert "single-base profiles can miss multiplicative or gated interactions" in report


def test_infer_pysr_expr_requires_force_and_force_runs(monkeypatch):
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    guard = session.infer_pysr_expr("Out", inputs=["IN1", "IN2"])
    assert "force=True is required" in guard

    def fake_pysr(input_fields, target, samples, *, niterations, timeout_s, maxsize):
        return [
            Candidate(Binary("*", Var("IN1", 16), Var("IN2", 16)), "pysr")
        ], "fake PySR completed"

    monkeypatch.setattr(
        "src.circuit.recovery.small_tools.pysr_candidates", fake_pysr)
    report = session.infer_pysr_expr(
        "Out", inputs=["IN1", "IN2"], force=True, niterations=1)
    assert "fake PySR completed" in report
    assert "Out = IN1 * IN2" in report


def test_plan_recovery_recommends_small_tools_not_recover_expression():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    report = session.plan_recovery(detail=True)
    assert "RTL recovery plan" in report
    assert "cluster1" in report
    assert "out1" in report
    assert "probe_target" in report
    assert "validate_expr" in report
    assert "effort : verify; search=none; risk=low" in report
    assert "effort : medium; search=bounded native TABLE-I candidate enumeration" in report
    assert "infer_native_expr" in report or "fit_template" in report
    assert "recover_expression ->" not in report
    assert "in1[4], in2[4], in3[4]" in report


def test_recovery_tool_guide_orders_tools_by_effort():
    session = CircuitSession()
    guide = session.recovery_tool_guide()
    assert "validate_expr: effort=verify search=none risk=low" in guide
    assert "infer_native_expr: effort=medium" in guide
    assert "infer_pysr_expr: effort=heavy" in guide
    assert "if a formula hypothesis exists, validate_expr is cheaper than any search" in guide


def test_plan_recovery_feedback_revises_next_step():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    report = session.plan_recovery(feedback={
        "source": "verify_rtl_candidate",
        "failure_type": "cex",
        "target": "out1",
        "summary": "mismatch on out1 with in1=3 in2=4 in3=1",
        "counterexample": {"word_values": {"in1": {"unsigned": 3}}},
    })
    assert "revised from feedback" in report
    assert "CEC/counterexample mismatch" in report
    assert "activated cone/control path" in report


def test_assemble_rtl_requires_accepted_candidates():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = session.assemble_rtl()
    assert "unrecovered outputs : Out" in report
    assert "accepted candidates are missing" in report
    assert "Candidate RTL" not in report


def test_assemble_rtl_after_local_candidate_emits_candidate_without_cec():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    local = session.fit_template(
        "Out", inputs=["IN1", "IN2"], pattern_num=128, validation_num=256)
    assert "C1" in local
    report = session.assemble_rtl()
    assert "Candidate RTL" in report
    assert "assign Out = IN1 * IN2;" in report
    assert "no original Verilog source is available for CEC" in report


def test_test13_bounded_session_recovery_assembles_and_cec_proves():
    if not shutil.which("yosys") or not shutil.which("yosys-abc"):
        pytest.skip("Yosys/ABC are required for test13 CEC acceptance")
    session = CircuitSession()
    session.load("examples/testcase/test13/top_primitive.v")
    result = _auto_recover_with_session(session, timeout_s=300)
    assert result["status"] == "cec-proved"
    assert result["unrecovered"] == []
    assert result["candidate_rtl"].splitlines()[0] == (
        "module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, "
        "in10, in11, in12, in13, in14, in15, out1, out2, out3, out4, out5);"
    )
    assert "assign out4 = $signed(in2) < $signed(in7);" in result["candidate_rtl"]


def test_test13_checked_in_recovered_rtl_matches_wolfex_target_cost():
    primitive = Path("examples/testcase/test13/top_primitive.v")
    recovered = Path("examples/testcase/test13/top_primitive_recovered.v")
    assert calc_verilog_cost(str(recovered)) == 17
    if not shutil.which("yosys"):
        pytest.skip("Yosys is required for test13 recovered RTL CEC")
    result = run_cec(str(primitive), str(recovered), timeout=120)
    assert result["success"], result["reason"]


def test_iccad22_skill_uses_short_cec_for_optimization_rounds():
    text = Path("skills/iccad22.md").read_text()
    assert "`round_cec_timeout_s`: normally `60`" in text
    assert "`final_cec_timeout_s`: normally `300`" in text
    assert "verify_rtl_candidate(timeout_s=60)" in text
    assert "verify_rtl_candidate(timeout_s=300)" in text


def test_recover_expression_is_deprecated_guardrail():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = session.recover_expression("Out", inputs=["IN1", "IN2"])
    assert "recover_expression is deprecated" in report
    assert "no longer runs the broad multi-method search" in report


def test_infer_expression_is_deprecated_guardrail():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = session.infer_expression("Out", inputs=["IN1", "IN2"])
    assert "infer_expression is deprecated" in report
    assert "validate_expr(..., fixed_inputs={...})" in report


def test_old_broad_tools_are_not_exposed_by_mcp():
    text = Path("mcp_server.py").read_text()
    assert "@mcp.tool()\ndef infer_expression" not in text
    assert "@mcp.tool()\ndef recover_expression" not in text
    assert "@mcp.tool()\ndef recover_rtl" not in text
    assert "@mcp.tool()\ndef list_candidates" in text
    assert "@mcp.tool()\ndef combine_case_expr" in text
    assert "@mcp.tool()\ndef influence_profile" in text
    assert "@mcp.tool()\ndef record_recovery_graph_node" in text
    assert "@mcp.tool()\ndef query_recovery_experience" in text
    assert "@mcp.tool()\ndef query_recovery_memory" in text
    assert "@mcp.tool()\ndef export_recovery_ir" in text
    assert "@mcp.tool()\ndef promote_recovery_experience" in text
    assert "@mcp.tool()\ndef promote_recovery_memory" in text


def test_mcp_tool_descriptions_include_agent_guardrails():
    text = Path("mcp_server.py").read_text()
    assert "validate_scope=\"support\"" in text
    assert "Slice results are labelled ``slice-*``" in text
    assert "balanced full assignments" in text
    assert "Perturb support bits/words" in text
    assert "internal signal names are rejected" in text
    assert "marginal rows such as" in text
    assert "after two nearby whole-module candidates fail" in text
    assert "not a hypothesis search tool" in text
    assert "read_file`` resets the session cache" in text
    assert "Retrieval is deterministic local RAG" in text
    assert "graph-only recording" in text
    assert "ordinary tool log" in text
    assert "expr_only`` evidence" in text


def test_recover_rtl_default_guard_and_force_baseline():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    guard = session.recover_rtl(
        niterations=0,
        pattern_num=128,
        validation_num=256,
    )
    assert "recover_rtl is deprecated" in guard
    assert "assemble_rtl" in guard

    forced = session.recover_rtl(
        force=True,
        niterations=0,
        pattern_num=128,
        validation_num=256,
    )
    assert "Candidate RTL" in forced
    assert "assign Out = IN1 * IN2;" in forced
