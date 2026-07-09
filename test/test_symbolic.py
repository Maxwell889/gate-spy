"""Tests for cost-aware symbolic expression discovery."""

from scripts.cost import calc_verilog_cost
from src.circuit import Circuit
from src.circuit.symbolic import (
    Binary, BitVec, Cast, Conditional, Concat, Const, PartSelect, Replicate, Select,
    Unary, Var,
)
from src.circuit.symbolic import infer_expression as infer_expression_core
from src.circuit.symbolic.parser import parse_expression
from src.session import CircuitSession


def _script_expr_cost(tmp_path, expr, width=4):
    path = tmp_path / "cost_expr.v"
    path.write_text(
        "module top(a,b,s,y);\n"
        f"  input [{width - 1}:0] a,b;\n"
        "  input s;\n"
        f"  output [{width - 1}:0] y;\n"
        f"  assign y = {expr};\n"
        "endmodule\n"
    )
    return calc_verilog_cost(str(path))


def test_table_i_ast_cost_matches_cost_script(tmp_path):
    a = Var("a", 4)
    b = Var("b", 4)
    s = Var("s", 1)
    cases = [
        Select(a, 1),
        PartSelect(a, 2, 0),
        Concat((a, b)),
        Replicate(3, s),
        Unary("~", a),
        Unary("&", a),
        Unary("!", a),
        Binary("&", a, b),
        Binary("^~", a, b),
        Binary("+", a, b),
        Binary("%", a, b),
        Binary("<<", a, Const(1, 4)),
        Binary(">=", a, b),
        Binary("===", a, b),
        Binary("&&", s, Select(a, 0)),
        Conditional(s, a, b),
    ]
    for expr in cases:
        assert expr.cost() == _script_expr_cost(tmp_path, expr.render())


def test_table_i_ast_evaluation():
    a = Var("a", 4)
    b = Var("b", 4)
    s = Var("s", 1)
    env = {
        "a": BitVec(0b1010, 4),
        "b": BitVec(0b0011, 4),
        "s": BitVec(1, 1),
    }
    assert Select(a, 1).evaluate(env).unsigned == 1
    assert PartSelect(a, 3, 2).evaluate(env).unsigned == 0b10
    assert Concat((Select(a, 3), Select(b, 0))).evaluate(env).unsigned == 0b11
    assert Replicate(4, Select(b, 0)).evaluate(env).unsigned == 0b1111
    assert Unary("~", a).evaluate(env).unsigned == 0b0101
    assert Unary("^", a).evaluate(env).unsigned == 0
    assert Unary("~|", b).evaluate(env).unsigned == 0
    assert Binary("^", a, b).evaluate(env).unsigned == 0b1001
    assert Binary("^~", a, b).evaluate(env).unsigned == 0b0110
    assert Binary("*", a, b).evaluate(env).unsigned == 30
    assert Binary(">>", a, Const(1, 4)).evaluate(env).unsigned == 0b0101
    assert Binary(">=", a, b).evaluate(env).unsigned == 1
    assert Binary("&&", s, b).evaluate(env).unsigned == 1
    assert Conditional(s, a, b).evaluate(env).unsigned == 0b1010


def test_signed_cast_parse_and_evaluate():
    expr = parse_expression(
        "$signed(a) > $signed(b)",
        input_widths={"a": 2, "b": 2},
        target_width=1,
    )
    assert isinstance(expr.lhs, Cast)
    assert expr.render() == "$signed(a) > $signed(b)"
    env = {"a": BitVec(0b11, 2), "b": BitVec(0b01, 2)}
    assert expr.evaluate(env).unsigned == 0
    env = {"a": BitVec(0b01, 2), "b": BitVec(0b11, 2)}
    assert expr.evaluate(env).unsigned == 1


def test_signed_concat_sign_extension_parse_and_evaluate():
    expr = parse_expression(
        "$signed({in1[11], in1}) < $signed({in2[11], in2})",
        input_widths={"in1": 12, "in2": 12},
        target_width=1,
    )
    assert expr.render() == "$signed({in1[11], in1}) < $signed({in2[11], in2})"
    env = {"in1": BitVec(0xfff, 12), "in2": BitVec(0, 12)}
    assert expr.evaluate(env).unsigned == 1
    env = {"in1": BitVec(1, 12), "in2": BitVec(0xfff, 12)}
    assert expr.evaluate(env).unsigned == 0


def test_infer_expression_and_gate():
    c = Circuit.from_string(
        "module top(a,b,y); input a,b; output y; wire a,b,y; "
        "AND g0 ( .A(a), .B(b), .Y(y) ); endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    report = infer_expression_core(
        c, source=None, targets="y", inputs=["a", "b"], mode="bit", niterations=0)
    assert "[exhaustive-exact]" in report
    assert "y = a & b" in report


def test_infer_expression_mux():
    c = Circuit.from_string(
        "module top(s,a,b,y); input s,a,b; output y; wire s,a,b,y,ns,t0,t1; "
        "NOT g0 ( .A(s), .Y(ns) ); "
        "AND g1 ( .A(a), .B(ns), .Y(t0) ); "
        "AND g2 ( .A(b), .B(s), .Y(t1) ); "
        "OR g3 ( .A(t0), .B(t1), .Y(y) ); endmodule"
    )
    session = CircuitSession()
    session.circuit = c
    report = infer_expression_core(
        c, source=None, targets="y", inputs=["s", "a", "b"],
        mode="mixed", niterations=0)
    assert "[exhaustive-exact]" in report
    assert "?" in report and ":" in report


def test_infer_expression_multiplier_example():
    session = CircuitSession()
    session.load("examples/Mul_INT16.aig")
    report = infer_expression_core(
        session.circuit, source=session.source,
        targets="Out", inputs=["IN1", "IN2"], niterations=0,
        pattern_num=128, validation_num=256)
    assert "sample-exact" in report
    assert "Out = IN1 * IN2" in report
    assert "cost=1" in report


def test_infer_expression_iccad22_adder():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    report = infer_expression_core(
        session.circuit, source=session.source,
        targets="out1", inputs=["in1", "in2", "in3"], niterations=0)
    assert "[exhaustive-exact]" in report
    assert "out1 = (in1 + in2) + in3" in report
