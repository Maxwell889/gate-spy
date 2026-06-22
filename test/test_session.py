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
    assert "IN1[0..15]" in aiger and "Out[0..31]" in aiger
    assert session.circuit is not None and session.source == "examples/Mul_INT16.aig"


def test_loaded_circuit_queries(session):
    session.load("examples/Mul_INT16.aig")
    assert session.summary() == session.circuit.summary()
    assert "longest chain" in session.xor_stats()
    assert "largest tree" in session.adder_stats()
    assert "kind" in session.node_info("Out[0]")


def test_simulate_fixed(session):
    session.load("examples/Mul_INT16.aig")
    # Fully pinned inputs -> a single deterministic pattern; 3 * 5 == 15.
    report = session.simulate(fixed_inputs={"IN1": 3, "IN2": 5})
    assert "patterns : 1 (fixed)" in report
    rows = [l for l in report.splitlines() if l and l[0].isdigit()]
    assert len(rows) == 1
    # columns: # | IN1 IN2 | Out
    cells = rows[0].replace("|", "").split()
    assert cells == ["0", "3", "5", "15"]


def test_simulate_random_and_pinned(session):
    session.load("examples/Mul_INT16.aig")
    report = session.simulate(pattern_num=8, fixed_inputs={"IN1": 7},
                              watch=["Out[0]", "n33"], seed=42)
    assert "patterns : 8 (random (partial constraints))" in report
    assert "IN1[16] = 7" in report and "IN2[16] random" in report
    assert "seed     : 42" in report
    assert "watch    : Out[0], n33" in report
    rows = [l for l in report.splitlines() if l and l[0].isdigit()]
    assert len(rows) == 8
    # IN1 pinned to 7 on every row; Out == IN1 * IN2 (product check).
    for row in rows:
        cells = [c for c in row.replace("|", "").split()]
        _, in1, in2, out = cells[0], int(cells[1]), int(cells[2]), int(cells[3])
        assert in1 == 7 and out == 7 * in2
    # Reproducible with the same seed.
    assert session.simulate(pattern_num=8, fixed_inputs={"IN1": 7},
                            watch=["Out[0]", "n33"], seed=42) == report


def test_simulate_bit_and_bus_keys(session):
    session.load("examples/Mul_INT16.aig")
    # Per-bit pin plus a watched internal bus base (Out gathered as integer).
    bits = {f"IN1[{i}]": (5 >> i) & 1 for i in range(16)}
    bits.update({f"IN2[{i}]": (9 >> i) & 1 for i in range(16)})
    report = session.simulate(fixed_inputs=bits, watch=["Out"])
    row = [l for l in report.splitlines() if l.startswith("0")][0]
    cells = row.replace("|", "").split()
    assert cells[1:] == ["5", "9", "45", "45"]   # IN1 IN2 Out | watch Out


def test_simulate_partial_bus_warns(session):
    session.load("examples/Mul_INT16.aig")
    # IN1 only half-pinned -> flagged partial, with a NOTE; not read as a value.
    report = session.simulate(pattern_num=4,
                              fixed_inputs={f"IN1[{i}]": 1 for i in range(8)},
                              seed=0)
    assert "patterns : 4 (random (partial constraints))" in report
    assert "IN1[16] partial (8/16 fixed)" in report
    assert "NOTE: input bus 'IN1' is only partially constrained" in report


def test_simulate_errors(session):
    session.load("examples/Mul_INT16.aig")
    # Unknown input -> message lists the real buses so the LLM can recover.
    with pytest.raises(ValueError, match="not a primary input"):
        session.simulate(fixed_inputs={"NOPE": 1})
    with pytest.raises(ValueError, match=r"Available input buses: IN1\[16\]"):
        session.simulate(fixed_inputs={"NOPE": 1})
    # Word-level overflow is rejected, not silently truncated.
    with pytest.raises(ValueError, match=r"out of range for its 16-bit width"):
        session.simulate(fixed_inputs={"IN1": 70000})
    # A single bit must be 0/1, not masked.
    with pytest.raises(ValueError, match=r"not a single bit"):
        session.simulate(fixed_inputs={"IN1[0]": 2})
    # pattern_num bounds (both ends).
    with pytest.raises(ValueError, match="pattern_num"):
        session.simulate(pattern_num=0)
    with pytest.raises(ValueError, match="exceeds the cap"):
        session.simulate(pattern_num=10**9)
    with pytest.raises(KeyError, match="no signal matching"):
        session.simulate(watch=["does_not_exist"])


def test_requires_loaded_circuit(session):
    for op in (session.summary, session.xor_stats, session.adder_stats,
               session.simulate, lambda: session.node_info("x")):
        with pytest.raises(NoCircuitLoadedError):
            op()


def test_bad_paths(session):
    with pytest.raises(FileNotFoundError):
        session.load("examples/does_not_exist.aig")
