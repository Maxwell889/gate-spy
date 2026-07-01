"""Tests for LLM-guided word-level inference helpers."""

from src.session import CircuitSession
from src.circuit.infer import (
    Sample,
    Word,
    fit_basis_candidates,
    fit_builtin_candidates,
    fit_custom_template,
)
from src.circuit.infer.hypothesis import eval_expr


def word(name, width):
    return Word(name=name, width=width, bits=tuple((i, f"{name}[{i}]") for i in range(width)), kind="input")


def synthetic_samples(input_words, output_word, fn, values=range(4)):
    samples = []
    rows = [{}]
    for input_word in input_words:
        word_values = sorted({value & input_word.mask for value in values})
        rows = [
            dict(row, **{input_word.name: value})
            for row in rows
            for value in word_values
        ]
    for row in rows:
        samples.append(Sample(
            inputs=row,
            outputs={output_word.name: fn(row) & output_word.mask},
            input_bits={},
            output_bits={},
        ))
    return samples


def assert_candidate_matches(candidates, method, output_word, samples):
    exprs = [c["expr"] for c in candidates if c["method"] == method]
    assert exprs, f"no {method} candidate in {candidates}"
    assert any(
        all((eval_expr(expr, dict(s.inputs)) & output_word.mask) == s.outputs[output_word.name]
            for s in samples)
        for expr in exprs
    )


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


def test_fit_hypothesis_large_affine_template_solves_many_unknowns():
    input_count = 29
    names = [f"x{i}" for i in range(input_count)]
    coeff_names = [f"c{i}" for i in range(input_count)]
    coeffs = [(i % 3) + 1 for i in range(input_count)]
    out = word("y", 16)
    samples = []

    rows = []
    for i in range(input_count):
        rows.append({name: int(j == i) for j, name in enumerate(names)})
    rows.append({name: 1 for name in names})
    for i in range(8):
        rows.append({name: (i + j) % 2 for j, name in enumerate(names)})

    for row in rows:
        samples.append(Sample(
            inputs=row,
            outputs={"y": sum(coeffs[i] * row[names[i]] for i in range(input_count))},
            input_bits={},
            output_bits={},
        ))

    template = " + ".join(
        f"{coeff_names[i]} * {names[i]}" for i in range(input_count))
    fit = fit_custom_template(samples, out, template, coeff_names)

    assert fit["status"] == "fit"
    assert fit["solver"] == "large_affine"
    for name, coeff in zip(coeff_names, coeffs):
        assert fit["coefficients"][name] == coeff


def test_fit_basis_session_fits_llm_supplied_terms():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")

    report = session.fit_basis(
        output="out1",
        basis=["in1", "in2", "in3"],
        sample_num=64,
    )

    assert "Basis fit for out1" in report
    assert "status  : fit" in report
    assert "out1 = in1 + in2 + in3" in report


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


def test_builtin_templates_cover_sparse_bilinear_and_product_of_sums():
    inputs = [word(n, 4) for n in ["a", "b", "c", "d"]]
    out = word("y", 8)

    bilinear_samples = synthetic_samples(
        inputs, out, lambda r: r["a"] * r["b"] + r["c"] * r["d"], values=range(3))
    bilinear = fit_builtin_candidates(
        bilinear_samples, out, inputs, methods=["bilinear"])
    assert_candidate_matches(bilinear, "bilinear", out, bilinear_samples)

    pos_samples = synthetic_samples(
        inputs, out, lambda r: (r["a"] + r["b"]) * (r["c"] + r["d"]), values=range(3))
    product = fit_builtin_candidates(
        pos_samples, out, inputs, methods=["product_of_sums"])
    assert_candidate_matches(product, "product_of_sums", out, pos_samples)


def test_builtin_templates_cover_compare_mux_shift_and_bitselect():
    a, b, c, d = [word(n, 4) for n in ["a", "b", "c", "d"]]
    sel = word("sel", 1)
    out1 = word("y", 1)
    out8 = word("z", 8)
    out4 = word("m", 4)

    compare_samples = synthetic_samples(
        [a, b], out1, lambda r: int(r["a"] < r["b"]), values=range(4))
    compare = fit_builtin_candidates(
        compare_samples, out1, [a, b], methods=["compare"])
    assert_candidate_matches(compare, "compare", out1, compare_samples)

    shift_samples = synthetic_samples(
        [a], out8, lambda r: r["a"] << 3, values=range(8))
    shift = fit_builtin_candidates(
        shift_samples, out8, [a], methods=["scale_shift"])
    assert any(c["expr"] == "a << 3" for c in shift)

    bit_samples = synthetic_samples(
        [a], out1, lambda r: (r["a"] >> 2) & 1, values=range(16))
    bitselect = fit_builtin_candidates(
        bit_samples, out1, [a], methods=["bitselect"])
    assert any(c["expr"] == "a[2]" for c in bitselect)

    mux_samples = synthetic_samples(
        [sel, a, b, c, d],
        out4,
        lambda r: (r["a"] + r["b"]) if r["sel"] else (r["c"] - r["d"]),
        values=range(3),
    )
    mux = fit_builtin_candidates(
        mux_samples, out4, [sel, a, b, c, d], methods=["mux"])
    assert_candidate_matches(mux, "mux", out4, mux_samples)


def test_builtin_mux_template_fits_wide_linear_branches():
    in1 = word("in1", 1)
    in2 = word("in2", 2)
    in8 = word("in8", 2)
    in25 = word("in25", 1)
    in5 = word("in5", 5)
    out = word("out10", 7)

    samples = synthetic_samples(
        [in1, in25, in2, in8, in5],
        out,
        lambda r: (r["in5"] if r["in25"] else 0)
        + r["in2"] + 24 * r["in8"] + r["in1"],
        values=range(4),
    )
    candidates = fit_builtin_candidates(
        samples, out, [in1, in25, in2, in8, in5], methods=["mux"])

    assert_candidate_matches(candidates, "mux", out, samples)
    assert any("24 * in8" in c["expr"] for c in candidates)


def test_fit_basis_candidates_supports_custom_product_and_condition_terms():
    a = word("a", 4)
    b = word("b", 4)
    c = word("c", 4)
    sel = word("sel", 1)
    out = word("y", 8)

    samples = synthetic_samples(
        [a, b, c, sel],
        out,
        lambda r: 4 + 3 * r["a"] - 2 * r["b"]
        + 5 * r["a"] * r["b"]
        + 7 * (r["c"] if r["sel"] else 0),
        values=range(4),
    )
    fit = fit_basis_candidates(
        samples,
        out,
        ["a", "b", "a * b", "sel ? c : 0"],
    )

    assert fit["status"] == "fit"
    assert fit["constant"] == 4
    assert fit["coefficients"]["a"] == 3
    assert fit["coefficients"]["b"] == -2
    assert fit["coefficients"]["a * b"] == 5
    assert fit["coefficients"]["sel ? c : 0"] == 7
    assert all(
        (eval_expr(fit["expr"], dict(sample.inputs)) & out.mask)
        == sample.outputs[out.name]
        for sample in samples
    )


def test_fit_basis_candidates_handles_modular_output_wrap():
    a = word("a", 4)
    b = word("b", 4)
    out = word("y", 4)
    samples = synthetic_samples(
        [a, b],
        out,
        lambda r: 8 + r["a"] + 2 * r["b"],
        values=range(16),
    )

    fit = fit_basis_candidates(samples, out, ["a", "b"])

    assert fit["status"] == "fit"
    assert fit["constant"] == 8
    assert fit["coefficients"]["a"] == 1
    assert fit["coefficients"]["b"] == 2
    assert all(
        (eval_expr(fit["expr"], dict(sample.inputs)) & out.mask)
        == sample.outputs[out.name]
        for sample in samples
    )


def test_sample_evaluator_supports_ternary_and_bit_selects():
    env = {"sel": 1, "a": 0b1011, "b": 3, "c": 9}
    assert eval_expr("sel ? a[3:1] : b", env) == 0b101
    assert eval_expr("!sel ? b : c[0]", env) == 1
    assert eval_expr("(sel ? a : b) + 4", env) == 15
