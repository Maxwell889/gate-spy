"""Tests for :mod:`src.circuit` — parsing, topology, and simulation."""

import pytest
from src.library import Library
from src.circuit import Circuit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def example_lib():
    return Library.from_file("examples/example.lib")


@pytest.fixture
def synth_circuit(example_lib):
    """The fully synthesised MulRecFN circuit."""
    return Circuit.from_file("examples/Mul_F16_synth.v", example_lib)


# ---------------------------------------------------------------------------
# Circuit parsing — small hand-written cases
# ---------------------------------------------------------------------------

class TestCircuitParse:
    """Parsing structural Verilog into a Circuit DAG."""

    def test_single_gate(self, example_lib):
        src = """
        module top(a, b, y);
          input a, b;
          output y;
          wire a, b, y;
          AND _0_ ( .A(a), .B(b), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        assert c.name == "top"
        assert c.input_nets == ["a", "b"]
        assert c.output_nets == ["y"]
        assert len(c.gate_nodes) == 1
        assert c.nodes[c.gate_nodes[0]].kind == "AND"

    def test_inverter_chain(self, example_lib):
        src = """
        module chain(a, y);
          input a;
          output y;
          wire a, y, n1;
          NOT _0_ ( .A(a), .Y(n1) );
          NOT _1_ ( .A(n1), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        assert len(c.gate_nodes) == 2
        # a -> NOT -> n1 -> NOT -> y  should buffer a
        assert c.simulate({"a": 0}) == {"y": 0}
        assert c.simulate({"a": 1}) == {"y": 1}

    def test_and_or_circuit(self, example_lib):
        src = """
        module andor(a, b, c, y);
          input a, b, c;
          output y;
          wire a, b, c, y, w;
          AND _0_ ( .A(a), .B(b), .Y(w) );
          OR  _1_ ( .A(w), .B(c), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        # y = (a & b) | c
        assert c.simulate({"a": 0, "b": 0, "c": 0}) == {"y": 0}
        assert c.simulate({"a": 1, "b": 1, "c": 0}) == {"y": 1}
        assert c.simulate({"a": 0, "b": 0, "c": 1}) == {"y": 1}
        assert c.simulate({"a": 1, "b": 1, "c": 1}) == {"y": 1}

    def test_bus_ports(self, example_lib):
        src = """
        module bus_top(a, y);
          input [1:0] a;
          output [1:0] y;
          wire [1:0] a, y;
          BUF _0_ ( .A(a[0]), .Y(y[0]) );
          BUF _1_ ( .A(a[1]), .Y(y[1]) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        assert c.input_nets == ["a[1]", "a[0]"]   # MSB first
        assert c.output_nets == ["y[1]", "y[0]"]
        assert len(c.pi_nodes) == 2
        assert len(c.po_nodes) == 2
        assert c.simulate({"a[1]": 1, "a[0]": 0}) == {"y[1]": 1, "y[0]": 0}

    def test_constants_in_circuit(self, example_lib):
        src = """
        module const_top(a, y);
          input a;
          output y;
          wire a, y;
          AND _0_ ( .A(a), .B(1'b1), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        # y = a & 1 = a
        assert c.simulate({"a": 0}) == {"y": 0}
        assert c.simulate({"a": 1}) == {"y": 1}

    def test_assign_statement(self, example_lib):
        src = """
        module assign_top(a, y);
          input a;
          output y;
          wire a, y, w;
          BUF _0_ ( .A(a), .Y(w) );
          assign y = w;
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        assert c.simulate({"a": 0}) == {"y": 0}
        assert c.simulate({"a": 1}) == {"y": 1}

    def test_no_module_raises(self, example_lib):
        with pytest.raises(ValueError, match="no module declaration"):
            Circuit.from_string("wire a;", example_lib)

    def test_default_input_value_is_zero(self, example_lib):
        src = """
        module top(a, y);
          input a;
          output y;
          wire a, y;
          BUF _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        # unspecified input defaults to 0
        assert c.simulate({}) == {"y": 0}


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

class TestTopology:
    """DAG structure: topological order, node types, counts."""

    def test_topological_order_linear(self, example_lib):
        src = """
        module top(a, y);
          input a;
          output y;
          wire a, y;
          NOT _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        order = c.topological_order()
        assert len(order) == len(c.nodes)
        # PI before gate, gate before PO
        pi_idx = order.index(c.pi_nodes[0])
        gate_idx = order.index(c.gate_nodes[0])
        po_idx = order.index(c.po_nodes[0])
        assert pi_idx < gate_idx < po_idx

    def test_topological_order_cached(self, example_lib):
        src = """
        module top(a, y);
          input a;
          output y;
          wire a, y;
          BUF _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        o1 = c.topological_order()
        o2 = c.topological_order()
        assert o1 is o2  # cached — same list object

    def test_gate_histogram(self, example_lib):
        src = """
        module top(a, b, y);
          input a, b;
          output y;
          wire a, b, y, n1, n2;
          NOT _0_ ( .A(a), .Y(n1) );
          AND _1_ ( .A(n1), .B(b), .Y(n2) );
          OR  _2_ ( .A(a), .B(n2), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        hist = c.gate_histogram()
        assert hist == {"NOT": 1, "AND": 1, "OR": 1}

    def test_node_properties(self, example_lib):
        src = """
        module top(a, y);
          input a;
          output y;
          wire a, y;
          BUF _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        pi = c.nodes[c.pi_nodes[0]]
        assert pi.is_pi and pi.is_source and not pi.is_po and not pi.is_gate

        po = c.nodes[c.po_nodes[0]]
        assert po.is_po and not po.is_source and not po.is_gate

        gate = c.nodes[c.gate_nodes[0]]
        assert gate.is_gate and not gate.is_pi and not gate.is_po

    def test_summary_and_repr(self, example_lib):
        src = """
        module top(a, y);
          input a;
          output y;
          wire a, y;
          NOT _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        s = c.summary()
        assert "top" in s
        assert "NOT:1" in s
        assert repr(c) == s


# ---------------------------------------------------------------------------
# Synthesised circuit (MulRecFN)
# ---------------------------------------------------------------------------

class TestSynthesizedCircuit:
    """Integration tests using the yosys-synthesised floating-point multiplier."""

    def test_parses_without_error(self, synth_circuit):
        """The synthesised netlist must parse successfully."""
        assert synth_circuit.name == "MulRecFN"
        assert len(synth_circuit.nodes) > 0

    def test_input_output_nets(self, synth_circuit):
        """Check port widths are expanded correctly."""
        # io_a[16:0] = 17 bits, io_b[16:0] = 17 bits,
        # io_roundingMode[2:0] = 3 bits, io_detectTininess = 1 bit
        # Total PI bits = 17+17+3+1 = 38
        assert len(synth_circuit.input_nets) == 38
        assert len(synth_circuit.pi_nodes) == 38
        # io_out[16:0] = 17 bits, io_exceptionFlags[4:0] = 5 bits
        # Total PO bits = 17+5 = 22
        assert len(synth_circuit.output_nets) == 22
        assert len(synth_circuit.po_nodes) == 22

    def test_topological_order_is_acyclic(self, synth_circuit):
        """A successful topological sort means the circuit has no combinational loops."""
        order = synth_circuit.topological_order()
        assert len(order) == len(synth_circuit.nodes)

    def test_gate_histogram_only_library_cells(self, synth_circuit):
        """Every gate must be from the example library."""
        hist = synth_circuit.gate_histogram()
        valid = {"NOT", "BUF", "AND", "NAND", "OR", "NOR", "XOR", "XNOR"}
        for kind in hist:
            assert kind in valid, f"unexpected gate kind: {kind}"
        total_gates = sum(hist.values())
        assert total_gates == len(synth_circuit.gate_nodes)
        assert total_gates > 100  # non-trivial circuit

    def test_simulate_all_zeros(self, synth_circuit):
        """Simulate with all inputs tied to 0 — should not crash."""
        result = synth_circuit.simulate({})
        assert len(result) == len(synth_circuit.output_nets)
        # all outputs should be deterministic (0 or 1)
        for net, val in result.items():
            assert val in (0, 1), f"{net} = {val}"

    def test_simulate_with_specific_inputs(self, synth_circuit):
        """Drive a few bits high and ensure simulation returns valid bits."""
        inputs = {net: 0 for net in synth_circuit.input_nets}
        # Set io_a to a known value through its bit nets
        for i in range(17):
            inputs[f"io_a[{i}]"] = 1 if i < 3 else 0  # io_a = 7
        for i in range(17):
            inputs[f"io_b[{i}]"] = 1 if i < 2 else 0  # io_b = 3
        result = synth_circuit.simulate(inputs)
        assert len(result) == len(synth_circuit.output_nets)
        for net, val in result.items():
            assert val in (0, 1), f"{net} = {val}"

    def test_xor_gate_simulation(self, example_lib):
        """End-to-end: XOR gate truth table via simulate."""
        src = """
        module xor_test(a, b, y);
          input a, b;
          output y;
          wire a, b, y;
          XOR _0_ ( .A(a), .B(b), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        assert c.simulate({"a": 0, "b": 0}) == {"y": 0}
        assert c.simulate({"a": 0, "b": 1}) == {"y": 1}
        assert c.simulate({"a": 1, "b": 0}) == {"y": 1}
        assert c.simulate({"a": 1, "b": 1}) == {"y": 0}

    def test_simulation_deterministic(self, synth_circuit):
        """Same inputs should always produce the same outputs."""
        inputs = {net: (i % 2) for i, net in enumerate(synth_circuit.input_nets)}
        r1 = synth_circuit.simulate(inputs)
        r2 = synth_circuit.simulate(inputs)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Concatenations and complex expressions
# ---------------------------------------------------------------------------

class TestComplexVerilog:
    """Parsing of concatenations, part-selects, and sized literals."""

    def test_concat_in_assign(self, example_lib):
        src = """
        module concat_top(a, b, y);
          input a, b;
          output [1:0] y;
          wire a, b;
          wire [1:0] y;
          assign y = {a, b};
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        # y[1] = a, y[0] = b
        assert c.simulate({"a": 1, "b": 0}) == {"y[1]": 1, "y[0]": 0}

    def test_part_select_in_connection(self, example_lib):
        src = """
        module partsel(a, y);
          input [2:0] a;
          output y;
          wire [2:0] a;
          wire y;
          BUF _0_ ( .A(a[0]), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        assert c.simulate({"a[0]": 1, "a[1]": 0, "a[2]": 0}) == {"y": 1}

    def test_sized_literal_constant(self, example_lib):
        src = """
        module lit_top(y);
          output [3:0] y;
          wire [3:0] y;
          assign y = 4'b1010;
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        # MSB first: y[3]=1, y[2]=0, y[1]=1, y[0]=0
        assert c.simulate({}) == {
            "y[3]": 1, "y[2]": 0, "y[1]": 1, "y[0]": 0,
        }


# ---------------------------------------------------------------------------
# DOT / Graphviz visualization
# ---------------------------------------------------------------------------

class TestDotVisualization:
    """``to_dot()`` generates valid Graphviz DOT for the circuit DAG."""

    def test_to_dot_produces_valid_dot_format(self, example_lib, tmp_path):
        src = """
        module top(a, b, y);
          input a, b;
          output y;
          wire a, b, y;
          AND _0_ ( .A(a), .B(b), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        dot_file = tmp_path / "top.dot"
        c.to_dot(str(dot_file))
        content = dot_file.read_text()
        assert content.startswith("digraph top {")
        assert "rankdir=LR" in content
        assert 'label="a"' in content       # PI node
        assert 'label="b"' in content       # PI node
        assert 'label="y"' in content       # PO node
        assert 'label="AND"' in content     # gate node
        assert "->" in content              # edges present
        assert content.strip().endswith("}")

    def test_to_dot_node_shapes_and_colors(self, example_lib, tmp_path):
        src = """
        module colors(a, y);
          input a;
          output y;
          wire a, y;
          NOT _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        dot_file = tmp_path / "colors.dot"
        c.to_dot(str(dot_file))
        content = dot_file.read_text()
        # PI: box shape, steel blue fill
        assert "shape=box" in content
        assert "#D4E6F1" in content    # PI fill
        assert "#FADBD8" in content    # PO fill
        # Gate: ellipse shape with auto-generated colour (hex pattern)
        assert "shape=ellipse" in content
        # every ellipse node should have a fill colour
        assert 'fillcolor="#' in content

    def test_to_dot_constants_use_diamond(self, example_lib, tmp_path):
        src = """
        module consts(a, y);
          input a;
          output y;
          wire a, y;
          AND _0_ ( .A(a), .B(1'b1), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        dot_file = tmp_path / "consts.dot"
        c.to_dot(str(dot_file))
        content = dot_file.read_text()
        assert "shape=diamond" in content
        assert "CONST1" in content

    def test_to_dot_on_synthesized_circuit(self, synth_circuit, tmp_path):
        dot_file = tmp_path / "synth.dot"
        synth_circuit.to_dot(str(dot_file))
        content = dot_file.read_text()
        assert content.startswith("digraph MulRecFN {")
        # Verify the gate types that are present in the synthed circuit appear
        hist = synth_circuit.gate_histogram()
        for kind in hist:
            assert kind in content
        # At least as many edges as gate nodes
        assert content.count("->") >= len(synth_circuit.gate_nodes)

    def test_to_dot_rank_constraints(self, example_lib, tmp_path):
        src = """
        module ranks(a, y);
          input a;
          output y;
          wire a, y;
          BUF _0_ ( .A(a), .Y(y) );
        endmodule
        """
        c = Circuit.from_string(src, example_lib)
        dot_file = tmp_path / "ranks.dot"
        c.to_dot(str(dot_file))
        content = dot_file.read_text()
        assert "rank=source" in content
        assert "rank=sink" in content
