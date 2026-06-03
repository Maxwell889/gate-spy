"""Tests for :mod:`src.session` — the framework-agnostic service layer."""

import pytest

from src.session import CircuitSession, NoCircuitLoadedError


@pytest.fixture
def session():
    return CircuitSession()


class TestLoad:
    """Loading files of each supported kind into the session."""

    def test_load_aiger(self, session):
        info = session.load("examples/Mul_INT16.aig")
        assert info["format"] == "aiger"
        assert info["source"] == "examples/Mul_INT16.aig"
        assert info["name"] == "Mul_INT16"
        assert info["inputs"] == 32
        assert info["outputs"] == 32
        assert set(info["gate_histogram"]) <= {"AND", "NOT"}

    def test_load_verilog_with_default_lib(self, session):
        info = session.load("examples/Mul_F16_synth.v")
        assert info["format"] == "verilog"
        assert info["name"] == "MulRecFN"
        assert info["gates"] > 0

    def test_load_updates_current_circuit(self, session):
        session.load("examples/Mul_INT16.aig")
        assert session.circuit is not None
        assert session.source == "examples/Mul_INT16.aig"

    def test_missing_file_raises(self, session):
        with pytest.raises(FileNotFoundError, match="no such file"):
            session.load("examples/does_not_exist.aig")

    def test_unsupported_extension_raises(self, session):
        with pytest.raises(ValueError, match="unsupported file type"):
            session.load("examples/example.lib")


class TestStats:
    """The no-argument stats operation over session state."""

    def test_stats_before_load_raises(self, session):
        with pytest.raises(NoCircuitLoadedError, match="no circuit loaded"):
            session.stats()

    def test_stats_after_load(self, session):
        session.load("examples/Mul_INT16.aig")
        stats = session.stats()
        assert stats["name"] == "Mul_INT16"
        assert stats["source"] == "examples/Mul_INT16.aig"
        assert stats["inputs"] == 32

    def test_stats_match_load_payload(self, session):
        loaded = session.load("examples/Mul_INT16.aig")
        stats = session.stats()
        # The circuit-level fields reported by load() and stats() agree.
        for key in ("name", "inputs", "outputs", "gates", "nodes"):
            assert loaded[key] == stats[key]
