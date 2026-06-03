"""Tests for :mod:`src.session` — the framework-agnostic service layer."""

import pytest

from src.session import CircuitSession, NoCircuitLoadedError


@pytest.fixture
def session():
    return CircuitSession()


def test_load(session):
    aiger = session.load("examples/Mul_INT16.aig")
    assert "aiger" in aiger and "Mul_INT16" in aiger and "PIs=32" in aiger
    assert session.circuit is not None and session.source == "examples/Mul_INT16.aig"
    assert "MulRecFN" in session.load("examples/Mul_F16_synth.v")  # verilog + default lib


def test_summary_and_extraction_stats(session):
    session.load("examples/Mul_INT16.aig")
    assert session.summary() == session.circuit.summary()
    assert "longest chain" in session.xor_stats()
    assert "largest tree" in session.adder_stats()


def test_requires_loaded_circuit(session):
    for op in (session.summary, session.xor_stats, session.adder_stats):
        with pytest.raises(NoCircuitLoadedError):
            op()


def test_bad_paths(session):
    with pytest.raises(FileNotFoundError):
        session.load("examples/does_not_exist.aig")
    with pytest.raises(ValueError, match="unsupported file type"):
        session.load("examples/example.lib")
