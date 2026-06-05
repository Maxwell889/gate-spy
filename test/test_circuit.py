"""Tests for :mod:`src.circuit` — parsing, topology, simulation, AIGER, DOT."""

import pytest

from src.library import Library
from src.circuit import Circuit


@pytest.fixture
def lib():
    return Library.from_file("examples/example.lib")


@pytest.fixture
def synth(lib):
    return Circuit.from_file("examples/Mul_F16_synth.v", lib)


@pytest.fixture
def mul():
    return Circuit.from_aig_file("examples/Mul_INT16.aig")


def mod(lib, body, ports, decls):
    return Circuit.from_string(f"module top({ports}); {decls} {body} endmodule", lib)


# --- Verilog parsing & simulation ------------------------------------------

def test_parse_structure(lib):
    c = mod(lib, "AND _0_ ( .A(a), .B(b), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y;")
    assert c.name == "top" and c.input_nets == ["a", "b"] and c.output_nets == ["y"]
    assert len(c.gate_nodes) == 1 and c.nodes[c.gate_nodes[0]].kind == "AND"


def test_simulation(lib):
    inv = mod(lib, "NOT g0 ( .A(a), .Y(n) ); NOT g1 ( .A(n), .Y(y) );",
              "a,y", "input a; output y; wire a,y,n;")
    assert inv.simulate({"a": 1}) == {"y": 1}              # double inversion buffers
    ao = mod(lib, "AND g0 ( .A(a), .B(b), .Y(w) ); OR g1 ( .A(w), .B(c), .Y(y) );",
             "a,b,c,y", "input a,b,c; output y; wire a,b,c,y,w;")
    assert ao.simulate({"a": 1, "b": 1, "c": 0}) == {"y": 1}
    assert ao.simulate({"a": 0, "b": 0, "c": 0}) == {"y": 0}
    xor = mod(lib, "XOR g0 ( .A(a), .B(b), .Y(y) );",
              "a,b,y", "input a,b; output y; wire a,b,y;")
    assert [xor.simulate({"a": x, "b": z})["y"] for x, z in [(0, 0), (0, 1), (1, 1)]] == [0, 1, 0]
    const = mod(lib, "AND g0 ( .A(a), .B(1'b1), .Y(y) );",
                "a,y", "input a; output y; wire a,y;")
    assert const.simulate({"a": 1}) == {"y": 1}            # a & 1 = a
    asgn = mod(lib, "BUF g0 ( .A(a), .Y(w) ); assign y = w;",
               "a,y", "input a; output y; wire a,y,w;")
    assert asgn.simulate({"a": 1}) == {"y": 1}
    buf = mod(lib, "BUF g0 ( .A(a), .Y(y) );", "a,y", "input a; output y; wire a,y;")
    assert buf.simulate({}) == {"y": 0}                    # unspecified input -> 0


def test_buses_and_complex_exprs(lib):
    bus = mod(lib, "BUF g0 ( .A(a[0]), .Y(y[0]) ); BUF g1 ( .A(a[1]), .Y(y[1]) );",
              "a,y", "input [1:0] a; output [1:0] y; wire [1:0] a,y;")
    assert bus.input_nets == ["a[1]", "a[0]"]              # MSB first
    assert bus.simulate({"a[1]": 1, "a[0]": 0}) == {"y[1]": 1, "y[0]": 0}
    cat = mod(lib, "assign y = {a, b};", "a,b,y",
              "input a,b; output [1:0] y; wire a,b; wire [1:0] y;")
    assert cat.simulate({"a": 1, "b": 0}) == {"y[1]": 1, "y[0]": 0}
    lit = mod(lib, "assign y = 4'b1010;", "y", "output [3:0] y; wire [3:0] y;")
    assert lit.simulate({}) == {"y[3]": 1, "y[2]": 0, "y[1]": 1, "y[0]": 0}


def test_no_module_raises(lib):
    with pytest.raises(ValueError, match="no module declaration"):
        Circuit.from_string("wire a;", lib)


def test_topology_and_summary(lib):
    c = mod(lib, "NOT g0 ( .A(a), .Y(n1) ); AND g1 ( .A(n1), .B(b), .Y(n2) ); "
                 "OR g2 ( .A(a), .B(n2), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y,n1,n2;")
    order = c.topological_order()
    assert len(order) == len(c.nodes) and c.topological_order() is order  # cached
    assert c.gate_histogram() == {"NOT": 1, "AND": 1, "OR": 1}
    pi = c.nodes[c.pi_nodes[0]]
    assert pi.is_pi and pi.is_source and not pi.is_gate
    assert c.nodes[c.gate_nodes[0]].is_gate
    s = c.summary()
    assert "top" in s and "NOT:1" in s and repr(c) == s


def test_describe_node(lib):
    c = mod(lib, "AND g0 ( .A(a), .B(b), .Y(y) );",
            "a,b,y", "input a,b; output y; wire a,b,y;")
    text = c.describe_node("y")
    assert "kind    : AND" in text and "function: (A * B)" in text
    assert "a (PI)" in text and "b (PI)" in text   # fan-in cone
    assert "y (PO)" in text                          # fan-out cone
    assert c.describe_node(str(c.gate_nodes[0]))     # resolvable by id too
    with pytest.raises(KeyError):
        c.describe_node("nope")


# --- Synthesised circuit (integration) -------------------------------------

def test_synth_circuit(synth):
    assert synth.name == "MulRecFN"
    assert len(synth.input_nets) == 38 and len(synth.output_nets) == 22
    assert len(synth.topological_order()) == len(synth.nodes)        # acyclic
    valid = {"NOT", "BUF", "AND", "NAND", "OR", "NOR", "XOR", "XNOR"}
    assert set(synth.gate_histogram()) <= valid
    inputs = {n: (i % 2) for i, n in enumerate(synth.input_nets)}
    result = synth.simulate(inputs)
    assert len(result) == 22 and all(v in (0, 1) for v in result.values())
    assert synth.simulate(inputs) == result                          # deterministic


# --- AIGER parsing ---------------------------------------------------------

def test_aig_parse(mul):
    assert mul.name == "Mul_INT16" and len(mul.pi_nodes) == 32 and len(mul.po_nodes) == 32
    assert set(mul.gate_histogram()) == {"AND", "NOT"} and mul.gate_histogram()["AND"] == 2784
    assert {n.split("[")[0] for n in mul.input_nets} == {"IN1", "IN2"}
    assert len(mul.topological_order()) == len(mul.nodes)


@pytest.mark.parametrize("a, b", [(3, 5), (255, 255)])
def test_aig_multiplies(mul, a, b):
    inp = {f"IN1[{i}]": (a >> i) & 1 for i in range(16)}
    inp.update({f"IN2[{i}]": (b >> i) & 1 for i in range(16)})
    out = mul.simulate(inp)
    assert sum((out[f"Out[{i}]"] & 1) << i for i in range(32)) == a * b


def test_aig_options_and_errors(lib):
    with open("examples/Mul_INT16.aig", "rb") as f:
        assert Circuit.from_aig_bytes(f.read()).gate_histogram()["AND"] == 2784
    assert Circuit.from_aig_file("examples/Mul_INT16.aig", lib).lib is lib
    with pytest.raises(ValueError, match="latches"):
        Circuit.from_aig_bytes(b"aag 1 0 1 2 0\n2 3\n2\n3\n")
    with pytest.raises(ValueError, match="invalid AIGER header"):
        Circuit.from_aig_bytes(b"not-an-aig 1 2 3\n")


# --- DOT export ------------------------------------------------------------

def test_to_dot(lib, out_dir):
    c = mod(lib, "AND g0 ( .A(a), .B(1'b1), .Y(y) );",
            "a,y", "input a; output y; wire a,y;")
    path = out_dir / "top.dot"
    c.to_dot(str(path))
    s = path.read_text()
    assert s.startswith("digraph top {") and s.strip().endswith("}")
    assert "rankdir=LR" in s and "->" in s
    assert "shape=box" in s and "shape=ellipse" in s and "shape=diamond" in s  # PI / gate / const
    assert "rank=source" in s and "rank=sink" in s and "CONST1" in s


def test_to_dot_large(mul, out_dir):
    path = out_dir / "mul.dot"
    mul.to_dot(str(path))
    s = path.read_text()
    assert s.startswith("digraph Mul_INT16 {")
    assert s.count("->") >= len(mul.gate_nodes)


# --- find_cone --------------------------------------------------------------

def test_find_cone_backward_basic(lib):
    c = mod(lib, "AND g0 ( .A(a), .B(b), .Y(y) );", "a,b,y",
            "input a,b; output y; wire a,b,y;")
    data = c.find_cone(["y"], "backward")
    assert data["ok"]
    assert data["direction"] == "backward"
    assert len(data["boundary"].get("pi_po", [])) == 2  # a, b
    # a and b should be at some layer
    boundary_pis = {c.nodes[nid].net for nid in data["boundary"]["pi_po"]}
    assert boundary_pis == {"a", "b"}


def test_find_cone_forward_basic(lib):
    c = mod(lib, "AND g0 ( .A(a), .B(b), .Y(y) );", "a,b,y",
            "input a,b; output y; wire a,b,y;")
    data = c.find_cone(["a"], "forward")
    assert data["direction"] == "forward"
    # a fans out to g0, g0 fans out to y
    po_boundary = data["boundary"].get("pi_po", [])
    assert len(po_boundary) >= 1


def test_find_cone_depth_limit(lib):
    c = mod(lib, "AND g1 ( .A(a), .B(b), .Y(m) ); AND g2 ( .A(m), .B(c), .Y(y) );",
            "a,b,c,y", "input a,b,c; output y; wire a,b,c,m,y;")
    data = c.find_cone(["y"], "backward", depth=1)
    # depth=1 should not reach PIs (need depth=2 for m, depth=3 for a,b,c)
    assert data["truncated"]
    assert len(data["boundary"].get("pi_po", [])) < 3  # shouldn't reach all PIs


def test_find_cone_stop_at(lib):
    c = mod(lib, "AND g1 ( .A(a), .B(b), .Y(m) ); AND g2 ( .A(m), .B(c), .Y(y) );",
            "a,b,c,y", "input a,b,c; output y; wire a,b,c,m,y;")
    data = c.find_cone(["y"], "backward", stop_at=["m"])
    assert "stop_at" in data["boundary"]
    stop_nets = {c.nodes[nid].net for nid in data["boundary"]["stop_at"]}
    assert stop_nets == {"m"}


def test_find_cone_invalid_errors(lib):
    c = mod(lib, "AND g0 ( .A(a), .B(b), .Y(y) );", "a,b,y",
            "input a,b; output y; wire a,b,y;")
    with pytest.raises(KeyError, match="unresolved"):
        c.find_cone(["z"], "backward")
    with pytest.raises(ValueError, match="direction"):
        c.find_cone(["y"], "up")
    with pytest.raises(KeyError, match="unresolved.*stop_at"):
        c.find_cone(["y"], "backward", stop_at=["z"])


def test_find_cone_synthetic_names(synth):
    """find_cone works on Verilog circuits with Yosys _0001_ internal names."""
    # The 36 adder-tree results are internal signals in Mul_F16_synth.v
    data = synth.find_cone(["_1514_", "_1517_", "_1539_"], "backward", depth=3)
    assert data["ok"]
    assert len(data["layers"]) >= 1
    # Should find some PIs among the transitive fan-in
    assert len(data["boundary"].get("pi_po", [])) > 0
