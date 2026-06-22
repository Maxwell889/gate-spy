"""Tests for :mod:`src.circuit` — parsing, topology, simulation, AIGER, DOT."""

import pytest

from src.circuit import Circuit


@pytest.fixture
def mul():
    return Circuit.from_aig_file("examples/Mul_INT16.aig")


@pytest.fixture
def iccad22_test01():
    """ICCAD22 Problem A test01 - primitive gates only"""
    return Circuit.from_file("examples/iccad22_test01.v")


def mod(body, ports, decls):
    return Circuit.from_string(f"module top({ports}); {decls} {body} endmodule")


# --- Verilog parsing & simulation ------------------------------------------

def test_parse_structure():
    c = mod("AND _0_ ( .A(a), .B(b), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y;")
    assert c.name == "top" and c.input_nets == ["a", "b"] and c.output_nets == ["y"]
    assert len(c.gate_nodes) == 1 and c.nodes[c.gate_nodes[0]].kind == "and"


def test_simulation():
    inv = mod("NOT g0 ( .A(a), .Y(n) ); NOT g1 ( .A(n), .Y(y) );",
              "a,y", "input a; output y; wire a,y,n;")
    assert inv.simulate({"a": 1}) == {"y": 1}
    ao = mod("AND g0 ( .A(a), .B(b), .Y(w) ); OR g1 ( .A(w), .B(c), .Y(y) );",
             "a,b,c,y", "input a,b,c; output y; wire a,b,c,y,w;")
    assert ao.simulate({"a": 1, "b": 1, "c": 0}) == {"y": 1}
    assert ao.simulate({"a": 0, "b": 0, "c": 0}) == {"y": 0}
    xor = mod("XOR g0 ( .A(a), .B(b), .Y(y) );",
              "a,b,y", "input a,b; output y; wire a,b,y;")
    assert [xor.simulate({"a": x, "b": z})["y"] for x, z in [(0, 0), (0, 1), (1, 1)]] == [0, 1, 0]
    const = mod("AND g0 ( .A(a), .B(1'b1), .Y(y) );",
                "a,y", "input a; output y; wire a,y;")
    assert const.simulate({"a": 1}) == {"y": 1}
    asgn = mod("BUF g0 ( .A(a), .Y(w) ); assign y = w;",
               "a,y", "input a; output y; wire a,y,w;")
    assert asgn.simulate({"a": 1}) == {"y": 1}
    buf = mod("BUF g0 ( .A(a), .Y(y) );", "a,y", "input a; output y; wire a,y;")
    assert buf.simulate({}) == {"y": 0}


def test_buses_and_complex_exprs():
    bus = mod("BUF g0 ( .A(a[0]), .Y(y[0]) ); BUF g1 ( .A(a[1]), .Y(y[1]) );",
              "a,y", "input [1:0] a; output [1:0] y; wire [1:0] a,y;")
    assert bus.input_nets == ["a[1]", "a[0]"]
    assert bus.simulate({"a[1]": 1, "a[0]": 0}) == {"y[1]": 1, "y[0]": 0}
    cat = mod("assign y = {a, b};", "a,b,y",
              "input a,b; output [1:0] y; wire a,b; wire [1:0] y;")
    assert cat.simulate({"a": 1, "b": 0}) == {"y[1]": 1, "y[0]": 0}
    lit = mod("assign y = 4'b1010;", "y", "output [3:0] y; wire [3:0] y;")
    assert lit.simulate({}) == {"y[3]": 1, "y[2]": 0, "y[1]": 1, "y[0]": 0}


def test_no_module_raises():
    with pytest.raises(ValueError, match="no module declaration"):
        Circuit.from_string("wire a;")


def test_topology_and_summary():
    c = mod("NOT g0 ( .A(a), .Y(n1) ); AND g1 ( .A(n1), .B(b), .Y(n2) ); "
                 "OR g2 ( .A(a), .B(n2), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y,n1,n2;")
    order = c.topological_order()
    assert len(order) == len(c.nodes) and c.topological_order() is order
    assert c.gate_histogram() == {"not": 1, "and": 1, "or": 1}
    pi = c.nodes[c.pi_nodes[0]]
    assert pi.is_pi and pi.is_source and not pi.is_gate
    assert c.nodes[c.gate_nodes[0]].is_gate
    s = c.summary()
    assert "top" in s and "not:1" in s and repr(c) == s


def test_describe_node():
    c = mod("AND g0 ( .A(a), .B(b), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y;")
    text = c.describe_node("y")
    assert "kind    : and" in text
    assert "a (PI)" in text and "b (PI)" in text
    assert "y (PO)" in text
    assert c.describe_node(str(c.gate_nodes[0]))
    with pytest.raises(KeyError):
        c.describe_node("nope")


# --- AIGER parsing ---------------------------------------------------------

def test_aig_parse(mul):
    assert mul.name == "Mul_INT16" and len(mul.pi_nodes) == 32 and len(mul.po_nodes) == 32
    assert set(mul.gate_histogram()) == {"and", "not"} and mul.gate_histogram()["and"] == 2784
    assert {n.split("[")[0] for n in mul.input_nets} == {"IN1", "IN2"}


@pytest.mark.parametrize("a,b", [(3, 5), (255, 255)])
def test_aig_multiplies(mul, a, b):
    inputs = {}
    for i in range(16):
        inputs[f"IN1[{i}]"] = (a >> i) & 1
        inputs[f"IN2[{i}]"] = (b >> i) & 1
    result = mul.simulate(inputs)
    product = sum(result[f"Out[{i}]"] << i for i in range(32))
    assert product == a * b


def test_aig_options_and_errors():
    with open("examples/Mul_INT16.aig", "rb") as f:
        assert Circuit.from_aig_bytes(f.read()).gate_histogram()["and"] == 2784
    c = Circuit.from_aig_file("examples/Mul_INT16.aig")
    assert c.name == "Mul_INT16"
    with pytest.raises(ValueError, match="latches"):
        Circuit.from_aig_bytes(b"aag 1 0 1 2 0\n2 3\n2\n3\n")
    with pytest.raises(ValueError, match="invalid AIGER header"):
        Circuit.from_aig_bytes(b"not-an-aig 1 2 3\n")


# --- DOT export ------------------------------------------------------------

def test_to_dot(out_dir):
    c = mod("AND g0 ( .A(a), .B(b), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y;")
    path = out_dir / "and.dot"
    c.to_dot(str(path))
    content = path.read_text()
    assert content.startswith("digraph") and content.strip().endswith("}")
    assert "label=and" in content or "label=\"and\"" in content


def test_to_dot_large(mul, out_dir):
    path = out_dir / "mul.dot"
    mul.to_dot(str(path))
    assert path.stat().st_size > 100_000


# --- find_cone -------------------------------------------------------------

def test_find_cone_backward_basic():
    c = mod("AND g0 ( .A(a), .B(b), .Y(y) );", "a,b,y",
            "input a,b; output y; wire a,b,y;")
    data = c.find_cone(["y"], "backward")
    assert data["ok"] and data["direction"] == "backward"
    assert len(data["layers"]) >= 1
    assert len(data["boundary"].get("pi_po", [])) == 2


def test_find_cone_forward_basic():
    c = mod("AND g0 ( .A(a), .B(b), .Y(m) ); AND g1 ( .A(m), .B(c), .Y(y) );",
            "a,b,c,y", "input a,b,c; output y; wire a,b,c,m,y;")
    data = c.find_cone(["a"], "forward")
    assert data["ok"] and data["direction"] == "forward"
    pis = {c.nodes[nid].net for nid in data["boundary"].get("pi_po", [])}
    assert "y" in pis


def test_find_cone_depth_limit():
    c = mod("AND g1 ( .A(a), .B(b), .Y(m) ); AND g2 ( .A(m), .B(c), .Y(y) );",
            "a,b,c,y", "input a,b,c; output y; wire a,b,c,m,y;")
    data = c.find_cone(["y"], "backward", depth=1)
    assert len(data["layers"]) == 1
    assert len(data["boundary"].get("pi_po", [])) < 3


def test_find_cone_stop_at():
    c = mod("AND g1 ( .A(a), .B(b), .Y(m) ); AND g2 ( .A(m), .B(c), .Y(y) );",
            "a,b,c,y", "input a,b,c; output y; wire a,b,c,m,y;")
    data = c.find_cone(["y"], "backward", stop_at=["m"])
    assert "stop_at" in data["boundary"]
    stop_nets = {c.nodes[nid].net for nid in data["boundary"]["stop_at"]}
    assert stop_nets == {"m"}


def test_find_cone_invalid_errors():
    c = mod("AND g0 ( .A(a), .B(b), .Y(y) );", "a,b,y",
            "input a,b; output y; wire a,b,y;")
    with pytest.raises(KeyError, match="unresolved"):
        c.find_cone(["z"], "backward")
    with pytest.raises(ValueError, match="direction"):
        c.find_cone(["y"], "up")
    with pytest.raises(KeyError, match="unresolved.*stop_at"):
        c.find_cone(["y"], "backward", stop_at=["z"])


def test_find_cone_synthetic_names(mul):
    data = mul.find_cone(["Out[0]"], "backward", depth=3)
    assert data["ok"]
    assert len(data["layers"]) >= 1
    assert len(data["boundary"].get("pi_po", [])) > 0


# --- ICCAD22 Problem A test case -------------------------------------------

def test_iccad22_parse_structure(iccad22_test01):
    """ICCAD22 test01 parsing: primitive gates only"""
    c = iccad22_test01
    assert c.name == "top"
    assert len(c.input_nets) == 12
    assert len(c.output_nets) == 4
    assert len(c.gate_nodes) == 35
    gate_kinds = {c.nodes[nid].kind for nid in c.gate_nodes}
    expected_kinds = {"and", "or", "xor", "xnor", "nor", "not"}
    assert gate_kinds.issubset(expected_kinds)


def test_iccad22_simulation(iccad22_test01):
    """ICCAD22 test01 simulation: 3-input 4-bit adder"""
    c = iccad22_test01
    result = c.simulate({
        "in1[0]": 0, "in1[1]": 0, "in1[2]": 0, "in1[3]": 0,
        "in2[0]": 0, "in2[1]": 0, "in2[2]": 0, "in2[3]": 0,
        "in3[0]": 0, "in3[1]": 0, "in3[2]": 0, "in3[3]": 0,
    })
    out_val = (result["out1[3]"] << 3) | (result["out1[2]"] << 2) | \
              (result["out1[1]"] << 1) | result["out1[0]"]
    assert out_val == 0

    result = c.simulate({
        "in1[0]": 1, "in1[1]": 0, "in1[2]": 0, "in1[3]": 0,
        "in2[0]": 0, "in2[1]": 1, "in2[2]": 0, "in2[3]": 0,
        "in3[0]": 1, "in3[1]": 1, "in3[2]": 0, "in3[3]": 0,
    })
    out_val = (result["out1[3]"] << 3) | (result["out1[2]"] << 2) | \
              (result["out1[1]"] << 1) | result["out1[0]"]
    assert out_val == 6

    result = c.simulate({
        "in1[0]": 1, "in1[1]": 0, "in1[2]": 1, "in1[3]": 0,
        "in2[0]": 1, "in2[1]": 1, "in2[2]": 1, "in2[3]": 0,
        "in3[0]": 1, "in3[1]": 1, "in3[2]": 0, "in3[3]": 0,
    })
    out_val = (result["out1[3]"] << 3) | (result["out1[2]"] << 2) | \
              (result["out1[1]"] << 1) | result["out1[0]"]
    assert out_val == 15


def test_iccad22_topology(iccad22_test01):
    """ICCAD22 test01 topology: verify topological ordering"""
    c = iccad22_test01
    order = c.topological_order()
    assert len(order) == len(c.nodes)
    pos = {nid: i for i, nid in enumerate(order)}
    for nid in c.gate_nodes:
        node = c.nodes[nid]
        for inp in node.inputs:
            assert pos[inp] < pos[nid], f"node {nid} before its input {inp}"
