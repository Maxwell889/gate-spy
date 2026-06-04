"""Tests for :mod:`src.session` — the framework-agnostic service layer."""

import pytest

from src.session import CircuitSession, NoCircuitLoadedError
from src.circuit import Circuit


@pytest.fixture
def session():
    return CircuitSession()


def test_load(session):
    aiger = session.load("examples/Mul_INT16.aig")
    assert "aiger" in aiger and "Mul_INT16" in aiger and "PIs=32" in aiger
    assert "IN1[0..15]" in aiger and "Out[0..31]" in aiger  # port listing
    assert session.circuit is not None and session.source == "examples/Mul_INT16.aig"
    verilog = session.load("examples/Mul_F16_synth.v")
    assert "MulRecFN" in verilog and "io_a[0..16]" in verilog


def test_subgraph_extraction(session, out_dir):
    session.load("examples/Mul_INT16.aig")
    p = str(out_dir / "test_sub.v")
    result = session.extract_subcircuit(["IN1[0]", "IN2[0]"], ["Out[0]"], p)
    assert "written to" in result and "PIs=2" in result
    # read back (names are sanitised: IN1[0] -> IN1_0, etc.)
    c2 = Circuit.from_file(p, session._lib())
    assert c2.simulate({"IN1_0": 1, "IN2_0": 1}) == {"Out_0": 1}


def test_loaded_circuit_queries(session):
    session.load("examples/Mul_INT16.aig")
    assert session.summary() == session.circuit.summary()
    assert "longest chain" in session.xor_stats()
    assert "largest tree" in session.adder_stats()
    assert "kind" in session.node_info("Out[0]")


def test_requires_loaded_circuit(session):
    for op in (session.summary, session.xor_stats, session.adder_stats,
               lambda: session.node_info("x")):
        with pytest.raises(NoCircuitLoadedError):
            op()


def test_bad_paths(session):
    with pytest.raises(FileNotFoundError):
        session.load("examples/does_not_exist.aig")
    with pytest.raises(ValueError, match="unsupported file type"):
        session.load("examples/example.lib")
