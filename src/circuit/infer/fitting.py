"""Template fitting for word-level expression hypotheses."""

from __future__ import annotations

import itertools
import re
from typing import Any

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


def fit_builtin_candidates(samples: list[Sample],
                           output: Word,
                           inputs: list[Word],
                           methods: list[str] | None = None) -> list[dict[str, Any]]:
    """Fit built-in linear and multiplication-addition templates."""
    methods = methods or ["linear", "muladd"]
    out: list[dict[str, Any]] = []
    if "linear" in methods:
        out.extend(_fit_linear(samples, output, inputs))
    if "muladd" in methods:
        out.extend(_fit_muladd(samples, output, inputs))
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
        return {
            "status": "too_many_combinations",
            "message": f"{combos} combinations requested; narrow unknown domains",
        }

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
