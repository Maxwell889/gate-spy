"""Tests for LLM-guided word-level inference helpers."""

from src.session import CircuitSession


def test_infer_candidates_finds_test01_adder():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")

    report = session.infer_candidates(output="out1", sample_num=64, detail=True)

    assert "Word-level candidate inference" in report
    assert "support : in1[4], in2[4], in3[4]" in report
    assert "out1 = in1 + in2 + in3" in report
    assert "cec=not_run_candidate_only" in report


def test_check_hypothesis_samples_pass_and_fail():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")

    ok = session.check_hypothesis(
        {"out1": "in1 + in2 + in3"},
        sample_num=64,
        run_cec=False,
    )
    assert "sample_status : pass" in ok
    assert "cec_status    : not_run" in ok

    bad = session.check_hypothesis(
        {"out1": "in1 + in2"},
        sample_num=64,
        run_cec=False,
    )
    assert "sample_status : mismatch" in bad
    assert "sample mismatches:" in bad


def test_fit_hypothesis_custom_constant_template():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")

    report = session.fit_hypothesis(
        output="out1",
        template="in1 + in2 + in3 + c",
        unknowns={"c": {"min": -2, "max": 2}},
        sample_num=64,
    )

    assert "status  : fit" in report
    assert "out1 = in1 + in2 + in3 + 0" in report
    assert "'c': 0" in report


def test_trace_counterexample_uses_sample_mismatch():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    session.check_hypothesis(
        {"out1": "in1 + in2"},
        sample_num=64,
        run_cec=False,
    )

    trace = session.trace_counterexample(output="out1", bits=[0, 1, 2, 3], depth=2)

    assert "Counterexample trace" in trace
    assert "word-level inputs:" in trace
    assert "out1: original=" in trace
    assert "candidate subterms for out1:" in trace
    assert "cone for out1:" in trace
