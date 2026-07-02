"""Tests for LLM-guided word-level inference helpers."""

from src.session import CircuitSession
from src.circuit.infer import (
    PolynomialBudget,
    SymbolicBudget,
    Sample,
    Word,
    fit_basis_candidates,
    fit_builtin_candidates,
    fit_custom_template,
    optimise_shared_wires,
    polynomial_rewrite_word,
    symbolic_regression_candidates,
)
from src.circuit.infer.hypothesis import check_samples, eval_expr


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


def test_check_hypothesis_reports_compact_cost_for_selector_factoring():
    session = CircuitSession()
    session.load("examples/testcase/test07/top_primitive.v")

    report = session.check_hypothesis(
        {"out1": "in4 ? ((in4[1] ? in1 : in3) * (in4[0] ? in2 : in3)) : 0"},
        sample_num=64,
        run_cec=False,
    )

    assert "sample_status : pass" in report
    assert "cost          : 6" in report
    assert "width extensions would cost 14" in report


def test_optimise_shared_wires_extracts_test19_style_bases():
    inputs = {
        "in1": word("in1", 1),
        "in2": word("in2", 2),
        "in5": word("in5", 5),
        "in8": word("in8", 2),
        "s1": word("s1", 1),
        "s2": word("s2", 1),
    }
    outputs = {
        "out1": word("out1", 7),
        "out2": word("out2", 7),
    }
    assignments = {
        "out1": "s1 ? (in1 + in2 + 24 * in8 + in5) : (in1 + in2 + 24 * in8)",
        "out2": "s2 ? (in1 + in2 + 24 * in8 + in5) : (in1 + in2 + 24 * in8)",
    }

    declarations, shared_assignments, stats = optimise_shared_wires(
        assignments, "", inputs, outputs)

    assert stats["shared_count"] >= 3
    assert "assign gs_cse0 = in1 + in2;" in declarations
    assert any("24 * in8" in item["expr"] for item in stats["shared_wires"])
    samples = synthetic_samples(list(inputs.values()), outputs["out1"], lambda r: 0, values=range(4))
    samples = [
        Sample(
            inputs=s.inputs,
            outputs={
                "out1": ((s.inputs["in1"] + s.inputs["in2"] + 24 * s.inputs["in8"] + s.inputs["in5"])
                         if s.inputs["s1"] else
                         (s.inputs["in1"] + s.inputs["in2"] + 24 * s.inputs["in8"])) & outputs["out1"].mask,
                "out2": ((s.inputs["in1"] + s.inputs["in2"] + 24 * s.inputs["in8"] + s.inputs["in5"])
                         if s.inputs["s2"] else
                         (s.inputs["in1"] + s.inputs["in2"] + 24 * s.inputs["in8"])) & outputs["out2"].mask,
            },
            input_bits={},
            output_bits={},
        )
        for s in samples
    ]
    status, mismatches = check_samples(shared_assignments, declarations, samples, outputs)
    assert status == "pass", mismatches


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


def test_sample_evaluator_supports_signed_casts():
    env = {"a": 0xFFF, "b": 0}
    widths = {"a": 12, "b": 12}

    assert eval_expr("$signed(a) > $signed(b)", env, widths=widths) == 0
    assert eval_expr("$signed(b) > $signed(a)", env, widths=widths) == 1
    assert eval_expr("$unsigned(a) > $unsigned(b)", env, widths=widths) == 1


def test_check_samples_supports_signed_local_declarations():
    a = word("a", 12)
    b = word("b", 12)
    out = word("y", 1)

    def signed12(value):
        return value - 4096 if value & 0x800 else value

    samples = synthetic_samples(
        [a, b],
        out,
        lambda row: int(signed12(row["b"]) > signed12(row["a"])),
        values=[0, 1, 2047, 2048, 4095],
    )
    declarations = """
    wire signed [11:0] sa = a;
    wire signed [11:0] sb = b;
    """

    status, mismatches = check_samples(
        {"y": "sb > sa"},
        declarations,
        samples,
        {"y": out},
        {"a": a, "b": b},
    )

    assert status == "pass", mismatches


def test_propose_strategy_lists_three_recovery_lanes():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")

    report = session.propose_strategy(output="out1", detail=True)

    assert "Three-lane recovery strategy" in report
    assert "1. template" in report
    assert "2. polynomial" in report
    assert "3. symbolic" in report
    assert 'run_method(output="out1", method="template")' in report


def test_polynomial_rewrite_word_builds_sample_checked_hypothesis():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    circuit = session._current()
    _, outputs = session._io_words()

    fit = polynomial_rewrite_word(
        circuit,
        outputs["out1"],
        PolynomialBudget(max_nodes=256, max_expr_chars=24000),
    )

    assert fit["status"] == "fit"
    assert fit["output"] == "out1"
    report = session.run_method(
        output="out1",
        method="polynomial",
        sample_num=32,
        budget={"max_nodes": 256, "max_expr_chars": 24000},
    )
    assert "Method run: polynomial_rewrite for out1" in report
    assert "sample_status     : pass" in report


def test_symbolic_regression_finds_small_arithmetic_expression():
    a = word("a", 4)
    b = word("b", 4)
    c = word("c", 4)
    out = word("y", 5)
    samples = synthetic_samples(
        [a, b, c],
        out,
        lambda r: r["a"] + r["b"] + r["c"],
        values=range(4),
    )

    fit = symbolic_regression_candidates(
        samples,
        out,
        [a, b, c],
        budget=SymbolicBudget(max_expr_size=5, beam_width=128),
    )

    assert fit["status"] == "fit"
    assert all(
        (eval_expr(fit["expr"], dict(sample.inputs)) & out.mask)
        == sample.outputs[out.name]
        for sample in samples
    )


def test_run_symbolic_method_records_candidate():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")

    report = session.run_method(
        output="out1",
        method="symbolic",
        sample_num=48,
        budget={"max_expr_size": 5, "beam_width": 256},
    )

    assert "Method run: symbolic_regression for out1" in report
    assert "status  : fit" in report
    assert "hypothesis #" in report


def test_symbolic_method_uses_product_shift_probe_on_wide_words():
    session = CircuitSession()
    session.load("examples/release/test09/top_primitive.v")

    report = session.run_method(
        output="out2",
        method="symbolic",
        sample_num=256,
    )

    assert "Method run: symbolic_regression for out2" in report
    assert "status  : fit" in report
    assert "out2 = (in1 * in3 + in2) >> 5" in report


def test_polynomial_method_skips_infeasible_rewrite_unless_forced():
    session = CircuitSession()
    session.load("examples/release/test09/top_primitive.v")

    report = session.run_method(
        output="out2",
        method="polynomial",
        sample_num=16,
    )

    assert "Method run: polynomial_rewrite for out2" in report
    assert "status            : skipped_budget" in report
    assert "polynomial rewrite estimate is over budget" in report


def test_explain_failure_wraps_trace_and_next_actions():
    session = CircuitSession()
    session.load("examples/iccad22_test01.v")
    session.check_hypothesis(
        {"out1": "in1 + in2"},
        sample_num=64,
        run_cec=False,
    )

    report = session.explain_failure(output="out1", depth=2)

    assert "Failure analysis for hypothesis" in report
    assert "diagnosis" in report
    assert "Counterexample trace" in report
    assert "Suggested next actions:" in report
