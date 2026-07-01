"""Small deterministic symbolic-regression engine for word-level samples."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .fitting import fit_basis_candidates
from .hypothesis import eval_expr
from .sampling import Sample
from .words import Word


@dataclass(frozen=True)
class SymbolicBudget:
    max_expr_size: int = 6
    beam_width: int = 256
    max_expr_chars: int = 160
    max_inputs: int = 8
    max_support_bits: int = 64
    max_pair_candidates: int = 64
    max_shift: int = 8


@dataclass(frozen=True)
class _Candidate:
    expr: str
    signature: tuple[int, ...]
    size: int


def _signature(expr: str, samples: list[Sample], mask: int) -> tuple[int, ...] | None:
    values: list[int] = []
    try:
        for sample in samples:
            values.append(eval_expr(expr, dict(sample.inputs)) & mask)
    except Exception:
        return None
    return tuple(values)


def _add_candidate(candidates: dict[tuple[int, ...], _Candidate],
                   expr: str,
                   samples: list[Sample],
                   mask: int,
                   size: int,
                   max_expr_chars: int) -> None:
    if len(expr) > max_expr_chars:
        return
    sig = _signature(expr, samples, mask)
    if sig is None:
        return
    existing = candidates.get(sig)
    cand = _Candidate(expr=expr, signature=sig, size=size)
    if existing is None or (size, len(expr), expr) < (existing.size, len(existing.expr), existing.expr):
        candidates[sig] = cand


def _add_signature_candidate(candidates: dict[tuple[int, ...], _Candidate],
                             expr: str,
                             signature: tuple[int, ...],
                             size: int,
                             max_expr_chars: int) -> None:
    if len(expr) > max_expr_chars:
        return
    existing = candidates.get(signature)
    cand = _Candidate(expr=expr, signature=signature, size=size)
    if existing is None or (size, len(expr), expr) < (
        existing.size,
        len(existing.expr),
        existing.expr,
    ):
        candidates[signature] = cand


def _prune(candidates: dict[tuple[int, ...], _Candidate],
           beam_width: int) -> list[_Candidate]:
    return sorted(
        candidates.values(),
        key=lambda c: (c.size, len(c.expr), c.expr),
    )[:beam_width]


def _constant_seeds(output: Word) -> list[str]:
    vals = [0, 1, 2, output.mask]
    if output.width > 1:
        vals.extend([(1 << (output.width - 1)) - 1, 1 << (output.width - 1)])
    return [str(v & output.mask) for v in dict.fromkeys(vals)]


def _matches_output(expr: str, samples: list[Sample], output: Word) -> bool:
    sig = _signature(expr, samples, output.mask)
    if sig is None:
        return False
    target = tuple(sample.outputs[output.name] & output.mask for sample in samples)
    return sig == target


def _fit_compact_word_sketches(samples: list[Sample],
                               output: Word,
                               inputs: list[Word],
                               max_shift: int) -> dict[str, Any] | None:
    """Search compact word-level sketches before broad grammar expansion.

    These sketches are generated systematically from the support words.  They
    cover common WolFEx-style arithmetic rewrites such as shifted affine forms,
    product-add-shift forms, and small product-of-sums forms.
    """
    if len(inputs) > 8:
        return None
    names = [word.name for word in inputs]
    shifts = range(0, max(0, min(max_shift, output.width)) + 1)
    bodies: list[str] = []

    def add_body(expr: str) -> None:
        if expr not in bodies:
            bodies.append(expr)

    for name in names:
        add_body(name)
    for i, a in enumerate(names):
        for b in names[i:]:
            add_body(f"{a} + {b}")
            add_body(f"{a} - {b}")
            add_body(f"{a} * {b}")
            product = f"{a} * {b}"
            for c in names:
                add_body(f"{product} + {c}")
                add_body(f"{product} - {c}")
                add_body(f"{c} - {product}")

    if len(names) <= 5:
        for a in names:
            others = [name for name in names if name != a]
            for i, b in enumerate(others):
                for c in others[i:]:
                    add_body(f"{a} * ({b} + {c})")
                    add_body(f"{a} * ({b} - {c})")
                    for d in names:
                        add_body(f"{a} * ({b} + {c}) + {d}")
                        add_body(f"{a} * ({b} - {c}) + {d}")

    for body in bodies:
        variants = [body]
        variants.extend(f"({body}) >> {shift}" for shift in shifts if shift)
        variants.extend(f"({body}) << {shift}" for shift in shifts if shift <= 3 and shift)
        for expr in variants:
            if _matches_output(expr, samples, output):
                return {
                    "status": "fit",
                    "output": output.name,
                    "expr": expr,
                    "samples": len(samples),
                    "expr_size": max(1, expr.count("+") + expr.count("-") + expr.count("*")
                                     + expr.count(">>") + expr.count("<<") + 1),
                    "method": "symbolic_regression_compact_word_sketch",
                }
    return None


def _grammar_search(samples: list[Sample],
                    output: Word,
                    inputs: list[Word],
                    budget: SymbolicBudget,
                    operators: list[str],
                    include_bitselects: bool,
                    method: str) -> dict[str, Any]:
    """Enumerate a bounded expression grammar using sample signatures."""
    mask = output.mask
    target = tuple(sample.outputs[output.name] & mask for sample in samples)
    by_size: dict[int, list[_Candidate]] = {}
    all_by_sig: dict[tuple[int, ...], _Candidate] = {}

    def target_match(signature: tuple[int, ...]) -> bool:
        return tuple(value & mask for value in signature) == target

    def first_target(candidates: list[_Candidate]) -> _Candidate | None:
        for candidate in candidates:
            if target_match(candidate.signature):
                return candidate
        return None

    seeds: dict[tuple[int, ...], _Candidate] = {}
    for word in inputs:
        _add_signature_candidate(
            seeds,
            word.name,
            tuple(sample.inputs[word.name] for sample in samples),
            1,
            budget.max_expr_chars,
        )
        if include_bitselects and word.width > 1:
            for bit in range(min(word.width, 8)):
                _add_signature_candidate(
                    seeds,
                    f"{word.name}[{bit}]",
                    tuple((sample.inputs[word.name] >> bit) & 1 for sample in samples),
                    1,
                    budget.max_expr_chars,
                )
    for const in _constant_seeds(output):
        value = int(const)
        _add_signature_candidate(
            seeds,
            const,
            tuple(value for _ in samples),
            1,
            budget.max_expr_chars,
        )

    by_size[1] = _prune(seeds, budget.beam_width)
    for cand in by_size[1]:
        all_by_sig[cand.signature] = cand
    cand = first_target(by_size[1])
    if cand is not None:
        return {
            "status": "fit",
            "output": output.name,
            "expr": cand.expr,
            "samples": len(samples),
            "expr_size": cand.size,
            "method": method,
        }

    for size in range(2, budget.max_expr_size + 1):
        current: dict[tuple[int, ...], _Candidate] = {}

        # Unary shifts are treated as one extra expression node.  This lets the
        # search discover compact arithmetic sketches such as ``(a*b+c) >> 5``
        # without hard-coding that specific shape.
        if "<<" in operators or ">>" in operators:
            for child_size in range(1, size):
                if child_size + 1 != size:
                    continue
                for child in by_size.get(child_size, []):
                    for shift in range(1, max(1, budget.max_shift) + 1):
                        if "<<" in operators:
                            _add_signature_candidate(
                                current,
                                f"({child.expr} << {shift})",
                                tuple(value << shift for value in child.signature),
                                size,
                                budget.max_expr_chars,
                            )
                        if ">>" in operators:
                            _add_signature_candidate(
                                current,
                                f"({child.expr} >> {shift})",
                                tuple((value >> shift) & mask for value in child.signature),
                                size,
                                budget.max_expr_chars,
                            )

        binary_ops = [
            op for op in operators if op in {"+", "-", "*", "&", "|", "^"}
        ]
        for left_size in range(1, size):
            right_size = size - left_size - 1
            if right_size < 1:
                continue
            lefts = by_size.get(left_size, [])
            rights = by_size.get(right_size, [])
            for left in lefts[:budget.max_pair_candidates]:
                for right in rights[:budget.max_pair_candidates]:
                    for op in binary_ops:
                        if op in {"+", "*", "&", "|", "^"} and left.expr > right.expr:
                            continue
                        expr = f"({left.expr} {op} {right.expr})"
                        if op == "+":
                            sig = tuple(
                                a + b
                                for a, b in zip(left.signature, right.signature)
                            )
                        elif op == "-":
                            sig = tuple(
                                a - b
                                for a, b in zip(left.signature, right.signature)
                            )
                        elif op == "*":
                            sig = tuple(
                                a * b
                                for a, b in zip(left.signature, right.signature)
                            )
                        elif op == "&":
                            sig = tuple(a & b for a, b in zip(left.signature, right.signature))
                        elif op == "|":
                            sig = tuple(a | b for a, b in zip(left.signature, right.signature))
                        elif op == "^":
                            sig = tuple(a ^ b for a, b in zip(left.signature, right.signature))
                        else:
                            continue
                        _add_signature_candidate(
                            current,
                            expr,
                            sig,
                            size,
                            budget.max_expr_chars,
                        )

        by_size[size] = _prune(current, budget.beam_width)
        for cand in by_size[size]:
            existing = all_by_sig.get(cand.signature)
            if existing is None or (cand.size, len(cand.expr)) < (
                existing.size,
                len(existing.expr),
            ):
                all_by_sig[cand.signature] = cand
        cand = first_target(by_size[size])
        if cand is not None:
            return {
                "status": "fit",
                "output": output.name,
                "expr": cand.expr,
                "samples": len(samples),
                "expr_size": cand.size,
                "method": method,
            }

    nearest: list[dict[str, Any]] = []
    for cand in _prune(all_by_sig, min(16, budget.beam_width)):
        matches = sum(1 for a, b in zip(cand.signature, target) if (a & mask) == b)
        nearest.append({
            "expr": cand.expr,
            "expr_size": cand.size,
            "matching_samples": matches,
        })
    nearest.sort(
        key=lambda item: (
            -item["matching_samples"],
            item["expr_size"],
            len(item["expr"]),
        )
    )
    return {
        "status": "no_fit",
        "output": output.name,
        "samples": len(samples),
        "max_expr_size": budget.max_expr_size,
        "searched_signatures": len(all_by_sig),
        "nearest": nearest[:5],
        "message": "no symbolic expression matched all samples within budget",
        "method": method,
    }


def symbolic_regression_candidates(samples: list[Sample],
                                   output: Word,
                                   inputs: list[Word],
                                   budget: SymbolicBudget | None = None,
                                   operators: list[str] | None = None
                                   ) -> dict[str, Any]:
    """Search a small expression grammar for sample-matching candidates.

    This is intentionally bounded and deterministic.  It plays the role of a
    cheap symbolic-regression lane; CEC/trace outside this function provide the
    SAT-style refinement loop.
    """
    budget = budget or SymbolicBudget()
    if not samples:
        return {"status": "error", "message": "symbolic regression needs samples"}
    if len(inputs) > budget.max_inputs:
        return {
            "status": "budget_exceeded",
            "message": (
                f"support has {len(inputs)} input words; "
                f"max_inputs={budget.max_inputs}"
            ),
            "support_words": [word.name for word in inputs],
        }

    support_bits = sum(word.width for word in inputs)
    affine_fit = fit_basis_candidates(
        samples,
        output,
        [word.name for word in inputs],
        include_constant=True,
    )
    if affine_fit.get("status") == "fit":
        return {
            "status": "fit",
            "output": output.name,
            "expr": affine_fit["expr"],
            "samples": len(samples),
            "expr_size": max(1, 2 * len(inputs) - 1),
            "method": "symbolic_regression_affine_prefit",
            "coefficients": affine_fit.get("coefficients", {}),
            "constant": affine_fit.get("constant"),
        }

    compact_fit = _fit_compact_word_sketches(
        samples, output, inputs, max_shift=budget.max_shift)
    if compact_fit is not None:
        return compact_fit

    ops = operators or ["+", "-", "*", "&", "|", "^", "<<", ">>"]
    word_fit = _grammar_search(
        samples,
        output,
        inputs,
        budget,
        ops,
        include_bitselects=False,
        method="symbolic_regression_word_grammar",
    )
    if word_fit.get("status") == "fit":
        return word_fit

    if support_bits > budget.max_support_bits:
        return {
            "status": "budget_exceeded",
            "message": (
                "affine prefit and word-level grammar did not match; support has "
                f"{support_bits} bits; max_support_bits="
                f"{budget.max_support_bits}. Build a smaller LLM basis, cut "
                "the cone, or raise the budget explicitly."
            ),
            "samples": len(samples),
            "support_words": [word.name for word in inputs],
            "support_bits": support_bits,
            "word_grammar_nearest": word_fit.get("nearest", []),
        }

    bit_fit = _grammar_search(
        samples,
        output,
        inputs,
        budget,
        ops,
        include_bitselects=True,
        method="symbolic_regression_bit_grammar",
    )
    if bit_fit.get("status") == "no_fit" and word_fit.get("nearest"):
        bit_fit["word_grammar_nearest"] = word_fit.get("nearest", [])
    return bit_fit
