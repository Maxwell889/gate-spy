"""Counterexample replay and mismatch tracing for failed hypotheses."""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

from .hypothesis import HypothesisRecord, eval_expr, parse_internal_assigns, parse_local_widths
from .words import Word, expand_word_value, word_value

if TYPE_CHECKING:
    from ..circuit import Circuit


def _extract_subterms(expr: str) -> list[str]:
    terms = [expr.strip()]
    depth = 0
    start = 0
    for i, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in "+-" and depth == 0 and i > start:
            terms.append(expr[start:i].strip())
            start = i
    tail = expr[start:].strip()
    if tail and tail not in terms:
        terms.append(tail)
    return [t for t in terms if t]


def _bits_from_word_values(input_words: dict[str, Word],
                           values: dict[str, int]) -> dict[str, int]:
    bits: dict[str, int] = {}
    for name, word in input_words.items():
        bits.update(expand_word_value(word, values.get(name, 0)))
    return bits


def _pattern_to_inputs(pattern: dict[str, str],
                       input_words: dict[str, Word]) -> dict[str, int]:
    bit_values: dict[str, int] = {}
    for net, val in pattern.items():
        if val in {"0", "1", 0, 1}:
            bit_values[net] = int(val)
    return {name: word_value(word, bit_values) for name, word in input_words.items()}


def _sample_mismatch_inputs(record: HypothesisRecord) -> dict[str, int] | None:
    for mismatch in record.sample_mismatches:
        if "inputs" in mismatch:
            return dict(mismatch["inputs"])
    return None


def trace_hypothesis_counterexample(circuit: "Circuit",
                                    record: HypothesisRecord,
                                    input_words: dict[str, Word],
                                    output_words: dict[str, Word],
                                    output: str = "",
                                    bits: list[int] | None = None,
                                    depth: int = 3) -> str:
    """Replay a failed hypothesis on its CEX or first sample mismatch."""
    cex = record.cec.get("counterexample") if record.cec else None
    if cex and cex.get("pattern"):
        inputs = _pattern_to_inputs(cex["pattern"], input_words)
        source = f"CEC counterexample from hypothesis #{record.id}"
    else:
        inputs = _sample_mismatch_inputs(record)
        source = f"sample mismatch from hypothesis #{record.id}"

    if inputs is None:
        return f"No counterexample or sample mismatch is available for hypothesis #{record.id}."

    input_bits = _bits_from_word_values(input_words, inputs)
    original_bits = circuit.simulate(input_bits)
    original_outputs = {
        name: word_value(word, original_bits)
        for name, word in output_words.items()
    }

    local_widths = parse_local_widths(record.declarations)
    internal_assigns = parse_internal_assigns(record.declarations)
    env: dict[str, int] = dict(inputs)
    subterm_values: dict[str, list[tuple[str, int | str]]] = {}
    candidate_outputs: dict[str, int] = {}

    try:
        for lhs, rhs in internal_assigns:
            width = local_widths.get(lhs)
            val = eval_expr(rhs, env)
            env[lhs] = val & ((1 << width) - 1) if width else val
        for out_name, expr in record.assignments.items():
            vals: list[tuple[str, int | str]] = []
            for term in _extract_subterms(expr):
                try:
                    vals.append((term, eval_expr(term, env)))
                except Exception as exc:
                    vals.append((term, f"error: {exc}"))
            subterm_values[out_name] = vals
            if out_name in output_words:
                val = eval_expr(expr, env) & output_words[out_name].mask
                env[out_name] = val
                candidate_outputs[out_name] = val
    except Exception as exc:
        return f"Trace failed while evaluating hypothesis #{record.id}: {exc}"

    outputs = [output] if output else list(record.assignments)
    lines = [
        f"Counterexample trace — {source}",
        "word-level inputs:",
    ]
    for name in input_words:
        lines.append(f"  {name}[{input_words[name].width}] = {inputs.get(name, 0)}")

    lines.append("mismatching outputs:")
    mismatch_outputs: list[str] = []
    for out_name in outputs:
        if out_name not in output_words:
            continue
        old = original_outputs[out_name]
        new = candidate_outputs.get(out_name)
        if new is None or old != new:
            mismatch_outputs.append(out_name)
            lines.append(f"  {out_name}: original={old}, hypothesis={new}")
            if bits:
                bad_bits = []
                for bit in bits:
                    old_bit = (old >> bit) & 1
                    new_bit = ((new or 0) >> bit) & 1
                    if old_bit != new_bit:
                        bad_bits.append(f"{out_name}[{bit}] {old_bit}!={new_bit}")
                if bad_bits:
                    lines.append("    selected bit mismatches: " + ", ".join(bad_bits))
    if not mismatch_outputs:
        lines.append("  (none on replay; CEX may have had don't-care bits not reproduced)")

    for out_name in outputs:
        if out_name in subterm_values:
            lines.append(f"candidate subterms for {out_name}:")
            for term, value in subterm_values[out_name]:
                lines.append(f"  {term} = {value}")

    cone_outputs = mismatch_outputs or outputs
    for out_name in cone_outputs:
        if out_name not in output_words:
            continue
        try:
            data = circuit.find_cone(output_words[out_name].nets_lsb_first,
                                     "backward", depth=depth)
        except Exception as exc:
            lines.append(f"cone for {out_name}: error: {exc}")
            continue
        hist: dict[str, int] = {}
        for layer in data.get("layers", []):
            for nid in layer.get("node_ids", []):
                kind = circuit.nodes[nid].kind
                hist[kind] = hist.get(kind, 0) + 1
        compact_hist = ", ".join(f"{k}:{v}" for k, v in sorted(hist.items()))
        boundary = []
        for nid in data.get("boundary", {}).get("pi_po", []):
            net = circuit.nodes[nid].net
            if net:
                boundary.append(net)
        grouped = sorted(set(re.sub(r"\[\d+\]$", "", n) for n in boundary))
        lines.append(
            f"cone for {out_name}: depth={depth}, gates={compact_hist or '(none)'}, "
            f"boundary words={', '.join(grouped) or '(none)'}"
        )

    return "\n".join(lines)
