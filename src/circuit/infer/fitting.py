"""Template fitting for word-level expression hypotheses."""

from __future__ import annotations

import itertools
import re
from typing import Any

import numpy as np

from .hypothesis import eval_expr
from .sampling import Sample
from .words import Word


def _term(coeff: int, name: str, first: bool) -> str:
    if coeff == 0:
        return ""
    sign = "-" if coeff < 0 else "+"
    mag = abs(coeff)
    body = name if mag == 1 else f"{mag} * {name}"
    if first:
        return f"-{body}" if coeff < 0 else body
    return f" {sign} {body}"


def _linear_expr(names: list[str], coeffs: tuple[int, ...], const: int,
                 width: int) -> str:
    parts: list[str] = []
    first = True
    for coeff, name in zip(coeffs, names):
        text = _term(coeff, name, first)
        if text:
            parts.append(text)
            first = False
    if const:
        c = const
        if c > (1 << (width - 1)):
            c = c - (1 << width)
        if first:
            parts.append(str(c))
        else:
            parts.append(f" {'+' if c >= 0 else '-'} {abs(c)}")
    return "".join(parts) if parts else "0"


def _check_linear(samples: list[Sample], output: str, width: int,
                  names: list[str], coeffs: tuple[int, ...]) -> int | None:
    mask = (1 << width) - 1
    first = samples[0]
    base0 = sum(c * first.inputs[n] for c, n in zip(coeffs, names))
    const = (first.outputs[output] - base0) & mask
    for sample in samples:
        val = sum(c * sample.inputs[n] for c, n in zip(coeffs, names)) + const
        if (val & mask) != sample.outputs[output]:
            return None
    return const


def _fit_linear(samples: list[Sample], output: Word,
                inputs: list[Word]) -> list[dict[str, Any]]:
    names = [w.name for w in inputs]
    if not names or len(names) > 7:
        return []
    candidates: list[dict[str, Any]] = []
    for coeffs in itertools.product([-2, -1, 0, 1, 2], repeat=len(names)):
        if not any(coeffs):
            continue
        const = _check_linear(samples, output.name, output.width, names, coeffs)
        if const is None:
            continue
        expr = _linear_expr(names, coeffs, const, output.width)
        score = sum(1 for c in coeffs if c) + (1 if const else 0)
        candidates.append({
            "method": "linear",
            "output": output.name,
            "expr": expr,
            "coefficients": dict(zip(names, coeffs)),
            "constant": const,
            "score": score,
        })
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _solve_linear_integer(rows: list[list[int]],
                          values: list[int]) -> list[int] | None:
    if not rows:
        return None
    matrix = np.asarray(rows, dtype=float)
    value_vec = np.asarray(values, dtype=float)
    try:
        solution, _residuals, rank, _singular = np.linalg.lstsq(
            matrix, value_vec, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if rank != matrix.shape[1]:
        return None
    rounded = [int(round(v)) for v in solution]
    predicted = matrix @ np.asarray(rounded, dtype=float)
    if not np.allclose(predicted, value_vec, atol=1e-6):
        return None
    return rounded


def _linear_solution_attempts(rows: list[list[int]],
                              values: list[int],
                              max_extra_rows: int = 128) -> list[list[int]]:
    """Propose integer solutions from full data and low-magnitude subsets.

    Full-sample least squares fails when the real expression is interpreted
    modulo the output width.  Low-magnitude deterministic samples often avoid
    wraparound and can still identify the coefficient vector; callers must
    verify any proposal on all samples.
    """
    if not rows:
        return []
    attempts: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()

    def add(solution: list[int] | None) -> None:
        if solution is None:
            return
        key = tuple(solution)
        if key in seen:
            return
        seen.add(key)
        attempts.append(solution)

    add(_solve_linear_integer(rows, values))

    col_count = len(rows[0])
    if len(rows) < col_count:
        return attempts

    order = sorted(
        range(len(rows)),
        key=lambda i: (sum(abs(v) for v in rows[i]), abs(values[i])),
    )
    extras = [0, 2, 4, 8, 16, 32, 64, max_extra_rows]
    for extra in extras:
        take = min(len(order), col_count + extra)
        if take < col_count:
            continue
        idx = order[:take]
        subset_rows = [rows[i] for i in idx]
        subset_values = [values[i] for i in idx]
        add(_solve_linear_integer(subset_rows, subset_values))
    return attempts


def _fit_affine_exact(samples: list[Sample], output: Word,
                      inputs: list[Word]) -> list[dict[str, Any]]:
    """Fit ``sum ai*Xi + c`` by exact rational elimination, then verify."""
    names = [w.name for w in inputs]
    if not names or len(names) > 6 or len(samples) < len(names) + 1:
        return []

    rows = [[sample.inputs[n] for n in names] + [1] for sample in samples]
    values = [sample.outputs[output.name] for sample in samples]
    solution = _solve_linear_integer(rows, values)
    if solution is None:
        return []

    coeffs = tuple(solution[:-1])
    const = solution[-1]
    if not any(coeffs) and const == 0:
        return []
    expr = _linear_expr(names, coeffs, const & output.mask, output.width)
    if not _candidate_matches(samples, output, expr):
        return []
    return [{
        "method": "affine",
        "output": output.name,
        "expr": expr,
        "coefficients": dict(zip(names, coeffs)),
        "constant": const & output.mask,
        "score": sum(1 for c in coeffs if c) + (1 if const else 0),
    }]


def _sum_expr(names: list[str], coeffs: tuple[int, ...]) -> str:
    parts: list[str] = []
    first = True
    for coeff, name in zip(coeffs, names):
        text = _term(coeff, name, first)
        if text:
            parts.append(text)
            first = False
    return "".join(parts) if parts else "0"


def _fit_muladd(samples: list[Sample], output: Word,
                inputs: list[Word]) -> list[dict[str, Any]]:
    names = [w.name for w in inputs]
    if len(names) < 2 or len(names) > 6:
        return []

    mask = (1 << output.width) - 1
    candidates: list[dict[str, Any]] = []
    coeff_domain = [-1, 0, 1]

    for factor in names:
        others = [n for n in names if n != factor]
        for prod_coeffs in itertools.product(coeff_domain, repeat=len(others)):
            if not any(prod_coeffs):
                continue
            prod0 = samples[0].inputs[factor] * sum(
                c * samples[0].inputs[n] for c, n in zip(prod_coeffs, others)
            )
            for lin_coeffs in itertools.product(coeff_domain, repeat=len(names)):
                lin0 = sum(c * samples[0].inputs[n] for c, n in zip(lin_coeffs, names))
                const = (samples[0].outputs[output.name] - prod0 - lin0) & mask
                ok = True
                for sample in samples:
                    prod = sample.inputs[factor] * sum(
                        c * sample.inputs[n] for c, n in zip(prod_coeffs, others)
                    )
                    lin = sum(c * sample.inputs[n] for c, n in zip(lin_coeffs, names))
                    if ((prod + lin + const) & mask) != sample.outputs[output.name]:
                        ok = False
                        break
                if not ok:
                    continue

                prod_inner = _sum_expr(others, prod_coeffs)
                lin_expr = _linear_expr(names, lin_coeffs, const, output.width)
                expr = f"{factor} * ({prod_inner})"
                if lin_expr != "0":
                    if lin_expr.startswith("-"):
                        expr += f" - {lin_expr[1:]}"
                    else:
                        expr += f" + {lin_expr}"
                score = (
                    1
                    + sum(1 for c in prod_coeffs if c)
                    + sum(1 for c in lin_coeffs if c)
                    + (1 if const else 0)
                )
                candidates.append({
                    "method": "muladd",
                    "output": output.name,
                    "expr": expr,
                    "factor": factor,
                    "product_coefficients": dict(zip(others, prod_coeffs)),
                    "linear_coefficients": dict(zip(names, lin_coeffs)),
                    "constant": const,
                    "score": score,
                })
                if len(candidates) >= 16:
                    break
            if len(candidates) >= 16:
                break
        if len(candidates) >= 16:
            break

    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _candidate_matches(samples: list[Sample], output: Word, expr: str) -> bool:
    for sample in samples:
        try:
            got = eval_expr(expr, dict(sample.inputs)) & output.mask
        except Exception:
            return False
        if got != sample.outputs[output.name]:
            return False
    return True


def _const_expr(samples: list[Sample], output: Word) -> str | None:
    if not samples:
        return None
    val = samples[0].outputs[output.name] & output.mask
    if all((s.outputs[output.name] & output.mask) == val for s in samples):
        return str(val)
    return None


def _product_expr(pair: tuple[str, str]) -> str:
    a, b = pair
    return f"{a} * {b}"


def _sparse_sum_expr(terms: list[tuple[int, str]], const: int, width: int) -> str:
    pieces: list[str] = []
    first = True
    for coeff, name in terms:
        text = _term(coeff, name, first)
        if text:
            pieces.append(text)
            first = False
    if const:
        c = const
        if c > (1 << (width - 1)):
            c = c - (1 << width)
        if first:
            pieces.append(str(c))
        else:
            pieces.append(f" {'+' if c >= 0 else '-'} {abs(c)}")
    return "".join(pieces) if pieces else "0"


def _fit_bilinear_sparse(samples: list[Sample], output: Word,
                         inputs: list[Word]) -> list[dict[str, Any]]:
    """Fit sparse ``sum cij*Xi*Xj + sum ai*Xi + c`` candidates."""
    names = [w.name for w in inputs]
    if len(names) < 2 or len(names) > 5:
        return []

    mask = (1 << output.width) - 1
    products = list(itertools.combinations_with_replacement(names, 2))
    product_choices: list[list[tuple[int, tuple[str, str]]]] = []
    for p in products:
        product_choices.append([(1, p), (-1, p)])
    for p1, p2 in itertools.combinations(products, 2):
        for c1 in (-1, 1):
            for c2 in (-1, 1):
                product_choices.append([(c1, p1), (c2, p2)])

    candidates: list[dict[str, Any]] = []
    for prod_terms in product_choices:
        for lin_coeffs in itertools.product([-1, 0, 1], repeat=len(names)):
            first = samples[0]
            prod0 = sum(
                coeff * first.inputs[a] * first.inputs[b]
                for coeff, (a, b) in prod_terms
            )
            lin0 = sum(c * first.inputs[n] for c, n in zip(lin_coeffs, names))
            const = (first.outputs[output.name] - prod0 - lin0) & mask
            ok = True
            for sample in samples:
                prod = sum(
                    coeff * sample.inputs[a] * sample.inputs[b]
                    for coeff, (a, b) in prod_terms
                )
                lin = sum(c * sample.inputs[n] for c, n in zip(lin_coeffs, names))
                if ((prod + lin + const) & mask) != sample.outputs[output.name]:
                    ok = False
                    break
            if not ok:
                continue
            terms: list[tuple[int, str]] = [
                (coeff, _product_expr(pair)) for coeff, pair in prod_terms
            ]
            terms.extend(
                (coeff, name) for coeff, name in zip(lin_coeffs, names) if coeff
            )
            expr = _sparse_sum_expr(terms, const, output.width)
            candidates.append({
                "method": "bilinear",
                "output": output.name,
                "expr": expr,
                "product_terms": [(c, p[0], p[1]) for c, p in prod_terms],
                "linear_coefficients": dict(zip(names, lin_coeffs)),
                "constant": const,
                "score": len(prod_terms) + sum(1 for c in lin_coeffs if c) + (1 if const else 0),
            })
            if len(candidates) >= 16:
                break
        if len(candidates) >= 16:
            break
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _fit_product_of_sums(samples: list[Sample], output: Word,
                         inputs: list[Word]) -> list[dict[str, Any]]:
    """Fit ``(sum ai*Xi) * (sum bi*Xi) + linear + c`` candidates."""
    names = [w.name for w in inputs]
    if len(names) < 2 or len(names) > 4:
        return []

    mask = (1 << output.width) - 1
    sums: list[tuple[str, tuple[int, ...]]] = []
    for coeffs in itertools.product([-1, 0, 1], repeat=len(names)):
        if any(coeffs):
            sums.append((_sum_expr(names, coeffs), coeffs))

    candidates: list[dict[str, Any]] = []
    for i, (sum_a, coeff_a) in enumerate(sums):
        for sum_b, coeff_b in sums[i:]:
            for lin_coeffs in itertools.product([-1, 0, 1], repeat=len(names)):
                first = samples[0]
                prod0 = (
                    sum(c * first.inputs[n] for c, n in zip(coeff_a, names))
                    * sum(c * first.inputs[n] for c, n in zip(coeff_b, names))
                )
                lin0 = sum(c * first.inputs[n] for c, n in zip(lin_coeffs, names))
                const = (first.outputs[output.name] - prod0 - lin0) & mask
                ok = True
                for sample in samples:
                    prod = (
                        sum(c * sample.inputs[n] for c, n in zip(coeff_a, names))
                        * sum(c * sample.inputs[n] for c, n in zip(coeff_b, names))
                    )
                    lin = sum(c * sample.inputs[n] for c, n in zip(lin_coeffs, names))
                    if ((prod + lin + const) & mask) != sample.outputs[output.name]:
                        ok = False
                        break
                if not ok:
                    continue
                lin_expr = _linear_expr(names, lin_coeffs, const, output.width)
                expr = f"({sum_a}) * ({sum_b})"
                if lin_expr != "0":
                    expr += f" + {lin_expr}" if not lin_expr.startswith("-") else f" - {lin_expr[1:]}"
                candidates.append({
                    "method": "product_of_sums",
                    "output": output.name,
                    "expr": expr,
                    "left_coefficients": dict(zip(names, coeff_a)),
                    "right_coefficients": dict(zip(names, coeff_b)),
                    "linear_coefficients": dict(zip(names, lin_coeffs)),
                    "constant": const,
                    "score": (
                        sum(1 for c in coeff_a if c)
                        + sum(1 for c in coeff_b if c)
                        + sum(1 for c in lin_coeffs if c)
                        + (1 if const else 0)
                    ),
                })
                if len(candidates) >= 12:
                    break
            if len(candidates) >= 12:
                break
        if len(candidates) >= 12:
            break
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:6]


def _fit_scale_shift(samples: list[Sample], output: Word,
                     inputs: list[Word]) -> list[dict[str, Any]]:
    """Fit constant scale and fixed shift candidates."""
    candidates: list[dict[str, Any]] = []
    scale_domain = [v for v in range(-32, 33) if v not in (-2, -1, 0, 1, 2)]
    for word in inputs:
        for shift in range(1, min(word.width, output.width, 8) + 1):
            for op, expr in [
                ("lshift", f"{word.name} << {shift}"),
                ("rshift", f"{word.name} >> {shift}"),
            ]:
                if _candidate_matches(samples, output, expr):
                    candidates.append({
                        "method": "shift",
                        "output": output.name,
                        "expr": expr,
                        "operator": op,
                        "score": 2,
                    })
        for scale in scale_domain:
            first = samples[0]
            const = (first.outputs[output.name] - scale * first.inputs[word.name]) & output.mask
            expr = _linear_expr([word.name], (scale,), const, output.width)
            if _candidate_matches(samples, output, expr):
                candidates.append({
                    "method": "scale",
                    "output": output.name,
                    "expr": expr,
                    "scale": scale,
                    "constant": const,
                    "score": 2 + (1 if const else 0),
                })
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _fit_bitselect(samples: list[Sample], output: Word,
                   inputs: list[Word]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for word in inputs:
        if output.width == 1:
            for bit in range(word.width):
                expr = f"{word.name}[{bit}]"
                if _candidate_matches(samples, output, expr):
                    candidates.append({
                        "method": "bit_select",
                        "output": output.name,
                        "expr": expr,
                        "bit": bit,
                        "score": 1,
                    })
        elif output.width < word.width:
            for lo in range(0, word.width - output.width + 1):
                hi = lo + output.width - 1
                expr = f"{word.name}[{hi}:{lo}]"
                if _candidate_matches(samples, output, expr):
                    candidates.append({
                        "method": "bit_slice",
                        "output": output.name,
                        "expr": expr,
                        "range": (hi, lo),
                        "score": 1,
                    })
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _compare_exprs(names: list[str], inputs: list[Word]) -> list[tuple[str, str]]:
    ops = ["<", "<=", ">", ">=", "==", "!="]
    exprs: list[tuple[str, str]] = []
    for a, b in itertools.permutations(names, 2):
        for op in ops:
            exprs.append((op, f"{a} {op} {b}"))
    for word in inputs:
        if word.width <= 8:
            constants = [0, 1, word.mask, 1 << (word.width - 1)]
            constants = list(dict.fromkeys(c & word.mask for c in constants))
            for k in constants:
                for op in ops:
                    exprs.append((op, f"{word.name} {op} {k}"))
                    exprs.append((op, f"{k} {op} {word.name}"))
    return exprs


def _fit_compare(samples: list[Sample], output: Word,
                 inputs: list[Word]) -> list[dict[str, Any]]:
    if output.width != 1 or len(inputs) > 8:
        return []
    names = [w.name for w in inputs]
    candidates: list[dict[str, Any]] = []
    for op, expr in _compare_exprs(names, inputs):
        if _candidate_matches(samples, output, expr):
            candidates.append({
                "method": "compare",
                "output": output.name,
                "expr": expr,
                "operator": op,
                "score": 2,
            })
            if len(candidates) >= 16:
                break
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _fit_branch(samples: list[Sample], output: Word,
                inputs: list[Word],
                include_bilinear: bool = False) -> str | None:
    const = _const_expr(samples, output)
    if const is not None:
        return const
    affine = _fit_affine_exact(samples, output, inputs)
    if affine:
        return affine[0]["expr"]
    linear = _fit_linear(samples, output, inputs)
    if linear:
        return linear[0]["expr"]
    if not include_bilinear:
        return None
    bilinear = _fit_bilinear_sparse(samples, output, inputs)
    if bilinear:
        return bilinear[0]["expr"]
    return None


def _condition_candidates(samples: list[Sample],
                          inputs: list[Word]) -> list[tuple[str, set[str]]]:
    conditions: list[tuple[str, set[str]]] = []
    for word in inputs:
        if word.width == 1:
            conditions.append((word.name, {word.name}))
        values = sorted({s.inputs[word.name] for s in samples})
        if word.width <= 4 or len(values) <= 8:
            for val in values[:8]:
                conditions.append((f"{word.name} == {val}", {word.name}))
    return conditions


def _branch_with_delta(samples: list[Sample], output: Word,
                       base_expr: str, inputs: list[Word]) -> str | None:
    for word in inputs:
        for expr in [
            f"{base_expr} + {word.name}",
            f"{base_expr} - {word.name}",
            f"{word.name} + {base_expr}",
        ]:
            if _candidate_matches(samples, output, expr):
                return expr
    return None


def _fit_mux(samples: list[Sample], output: Word,
             inputs: list[Word]) -> list[dict[str, Any]]:
    if len(inputs) > 6:
        return []
    candidates: list[dict[str, Any]] = []
    for cond, used in _condition_candidates(samples, inputs):
        try:
            true_samples = [s for s in samples if eval_expr(cond, dict(s.inputs))]
            false_samples = [s for s in samples if not eval_expr(cond, dict(s.inputs))]
        except Exception:
            continue
        if not true_samples or not false_samples:
            continue
        branch_inputs = [w for w in inputs if w.name not in used]
        true_expr = _fit_branch(true_samples, output, branch_inputs)
        false_expr = _fit_branch(false_samples, output, branch_inputs)
        if true_expr is None and false_expr is not None:
            true_expr = _branch_with_delta(
                true_samples, output, false_expr, branch_inputs)
        if false_expr is None and true_expr is not None:
            false_expr = _branch_with_delta(
                false_samples, output, true_expr, branch_inputs)
        if true_expr is None or false_expr is None or true_expr == false_expr:
            continue
        expr = f"({cond}) ? ({true_expr}) : ({false_expr})"
        if _candidate_matches(samples, output, expr):
            candidates.append({
                "method": "mux",
                "output": output.name,
                "expr": expr,
                "condition": cond,
                "true_expr": true_expr,
                "false_expr": false_expr,
                "score": 1 + len(true_expr.split()) + len(false_expr.split()),
            })
            if len(candidates) >= 8:
                break
    candidates.sort(key=lambda c: (c["score"], len(c["expr"])))
    return candidates[:8]


def _dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for cand in candidates:
        expr = cand["expr"]
        if expr in seen:
            continue
        seen.add(expr)
        out.append(cand)
    return out


def fit_builtin_candidates(samples: list[Sample],
                           output: Word,
                           inputs: list[Word],
                           methods: list[str] | None = None) -> list[dict[str, Any]]:
    """Fit built-in arithmetic, compare, bit-selection, and mux templates."""
    methods = methods or [
        "linear",
        "affine",
        "scale_shift",
        "bitselect",
        "bilinear",
        "product_of_sums",
        "muladd",
        "compare",
        "mux",
    ]
    out: list[dict[str, Any]] = []
    if "linear" in methods:
        out.extend(_fit_linear(samples, output, inputs))
    if "affine" in methods or "wide_linear" in methods:
        out.extend(_fit_affine_exact(samples, output, inputs))
    if "scale_shift" in methods or "scale" in methods or "shift" in methods:
        out.extend(_fit_scale_shift(samples, output, inputs))
    if "bitselect" in methods or "bit_select" in methods or "bit_slice" in methods:
        out.extend(_fit_bitselect(samples, output, inputs))
    if "bilinear" in methods:
        out.extend(_fit_bilinear_sparse(samples, output, inputs))
    if "product_of_sums" in methods:
        out.extend(_fit_product_of_sums(samples, output, inputs))
    if "muladd" in methods:
        out.extend(_fit_muladd(samples, output, inputs))
    if "compare" in methods or "comparator" in methods:
        out.extend(_fit_compare(samples, output, inputs))
    if "mux" in methods or "condition" in methods or "conditioned" in methods:
        out.extend(_fit_mux(samples, output, inputs))
    out = _dedupe(out)
    out.sort(key=lambda c: (c.get("score", 999), c.get("method", ""), len(c["expr"])))
    return out


def _normalise_unknown_domains(unknowns: list[str] | dict[str, Any] | None
                               ) -> dict[str, list[int]]:
    if unknowns is None:
        return {}
    if isinstance(unknowns, list):
        if len(unknowns) == 1:
            return {unknowns[0]: list(range(-512, 513))}
        return {name: [-2, -1, 0, 1, 2] for name in unknowns}
    domains: dict[str, list[int]] = {}
    for name, raw in unknowns.items():
        if isinstance(raw, dict):
            lo = int(raw.get("min", -2))
            hi = int(raw.get("max", 2))
            domains[name] = list(range(lo, hi + 1))
        elif isinstance(raw, list):
            domains[name] = [int(v) for v in raw]
        else:
            domains[name] = [-2, -1, 0, 1, 2]
    return domains


def _replace_unknowns(template: str, values: dict[str, int]) -> str:
    expr = template
    for name, value in sorted(values.items(), key=lambda kv: -len(kv[0])):
        expr = re.sub(rf"\b{re.escape(name)}\b", str(value), expr)
    return expr


def _basis_linear_expr(basis: list[str], coeffs: list[int], const: int) -> str:
    terms: list[tuple[int, str]] = []
    for coeff, expr in zip(coeffs, basis):
        stripped = expr.strip()
        if stripped in {"", "0"} or coeff == 0:
            continue
        if stripped == "1":
            const += coeff
        else:
            body = stripped if re.match(r"^[A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?$", stripped) else f"({stripped})"
            terms.append((coeff, body))
    return _sparse_sum_expr(terms, const, max(1, abs(const).bit_length() + 1))


def _evaluate_basis(samples: list[Sample],
                    basis: list[str]) -> tuple[list[list[int]], str | None]:
    rows: list[list[int]] = []
    for sample in samples:
        env = dict(sample.inputs)
        row: list[int] = []
        for expr in basis:
            try:
                row.append(eval_expr(expr, env))
            except Exception as exc:
                return [], f"{expr!r}: {exc}"
        rows.append(row)
    return rows, None


def fit_basis_candidates(samples: list[Sample],
                         output: Word,
                         basis: list[str],
                         include_constant: bool = True,
                         coefficient_limit: int = 4096) -> dict[str, Any]:
    """Fit ``const + sum(coeff_i * basis_i)`` for LLM-supplied basis terms.

    The solved expression is still verified on every sample with output masking;
    the numeric solver is only used to propose coefficients.
    """
    basis = [expr.strip() for expr in basis if expr and expr.strip()]
    if not basis:
        return {"status": "error", "message": "basis must contain at least one expression"}
    if len(basis) > 32:
        return {"status": "too_many_basis_terms", "message": "basis is limited to 32 terms"}

    basis_rows, error = _evaluate_basis(samples, basis)
    if error:
        return {"status": "error", "message": error, "samples": len(samples)}

    rows = [list(row) + ([1] if include_constant else []) for row in basis_rows]
    values = [sample.outputs[output.name] for sample in samples]

    attempts: list[tuple[list[int], int]] = []
    attempts.extend(([1 if i == j else 0 for i in range(len(basis))], 0)
                    for j in range(len(basis)))
    attempts.append(([1] * len(basis), 0))

    for solution in _linear_solution_attempts(rows, values):
        coeffs = solution[:len(basis)]
        const = solution[-1] if include_constant else 0
        attempts.append((coeffs, const))

    signed_values = [
        value - (1 << output.width) if value >= (1 << (output.width - 1)) else value
        for value in values
    ]
    for signed_solution in _linear_solution_attempts(rows, signed_values):
        coeffs = signed_solution[:len(basis)]
        const = signed_solution[-1] if include_constant else 0
        attempts.append((coeffs, const))

    seen: set[tuple[tuple[int, ...], int]] = set()
    for coeffs, const in attempts:
        key = (tuple(coeffs), const)
        if key in seen:
            continue
        seen.add(key)
        if any(abs(c) > coefficient_limit for c in coeffs) or abs(const) > coefficient_limit:
            continue
        expr = _basis_linear_expr(basis, coeffs, const)
        if _candidate_matches(samples, output, expr):
            return {
                "status": "fit",
                "output": output.name,
                "expr": expr,
                "basis": basis,
                "coefficients": dict(zip(basis, coeffs)),
                "constant": const,
                "samples": len(samples),
            }

    return {
        "status": "no_fit",
        "basis": basis,
        "samples": len(samples),
        "message": "no integer affine combination of supplied basis matched all samples",
    }


def _eval_template_with_unknowns(template: str,
                                 sample: Sample,
                                 values: dict[str, int]) -> int:
    env = dict(sample.inputs)
    env.update(values)
    return eval_expr(template, env)


def _fit_large_affine_template(samples: list[Sample],
                               output: Word,
                               template: str,
                               unknown_names: list[str],
                               coefficient_limit: int = 4096) -> dict[str, Any]:
    """Solve templates that are affine in many unknown coefficients."""
    zero_values = {name: 0 for name in unknown_names}
    rows: list[list[int]] = []
    values: list[int] = []

    try:
        for sample in samples:
            base = _eval_template_with_unknowns(template, sample, zero_values)
            row: list[int] = []
            for name in unknown_names:
                one_values = dict(zero_values)
                one_values[name] = 1
                row.append(
                    _eval_template_with_unknowns(template, sample, one_values)
                    - base
                )
            rows.append(row)
            values.append(sample.outputs[output.name] - base)
    except Exception as exc:
        return {"status": "error", "message": str(exc), "samples": len(samples)}

    attempts: list[list[int]] = []
    attempts.extend(_linear_solution_attempts(rows, values))

    signed_values = []
    for value, sample in zip(values, samples):
        raw = sample.outputs[output.name]
        signed_out = raw - (1 << output.width) if raw >= (1 << (output.width - 1)) else raw
        base = _eval_template_with_unknowns(template, sample, zero_values)
        signed_values.append(signed_out - base)
    attempts.extend(_linear_solution_attempts(rows, signed_values))

    seen: set[tuple[int, ...]] = set()
    for coeff_list in attempts:
        coeff_tuple = tuple(coeff_list)
        if coeff_tuple in seen:
            continue
        seen.add(coeff_tuple)
        if any(abs(c) > coefficient_limit for c in coeff_list):
            continue
        coeffs = dict(zip(unknown_names, coeff_list))
        expr = _replace_unknowns(template, coeffs)
        if _candidate_matches(samples, output, expr):
            return {
                "status": "fit",
                "output": output.name,
                "template": template,
                "expr": expr,
                "coefficients": coeffs,
                "samples": len(samples),
                "solver": "large_affine",
            }

    return {
        "status": "no_fit",
        "samples": len(samples),
        "message": "large affine solve found no coefficient set matching all samples",
    }


def fit_custom_template(samples: list[Sample],
                        output: Word,
                        template: str,
                        unknowns: list[str] | dict[str, Any] | None
                        ) -> dict[str, Any]:
    """Brute-force small integer unknowns for an LLM-proposed template."""
    domains = _normalise_unknown_domains(unknowns)
    combos = 1
    for values in domains.values():
        combos *= max(1, len(values))
    if combos > 1_000_000:
        return _fit_large_affine_template(samples, output, template, list(domains))

    names = list(domains)
    for raw_values in itertools.product(*(domains[name] for name in names)):
        coeffs = dict(zip(names, raw_values))
        expr = _replace_unknowns(template, coeffs)
        ok = True
        for sample in samples:
            env = dict(sample.inputs)
            try:
                got = eval_expr(expr, env) & output.mask
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
            if got != sample.outputs[output.name]:
                ok = False
                break
        if ok:
            return {
                "status": "fit",
                "output": output.name,
                "template": template,
                "expr": expr,
                "coefficients": coeffs,
                "samples": len(samples),
            }
    return {"status": "no_fit", "samples": len(samples)}
