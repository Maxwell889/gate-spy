"""Word boundary and support analysis for RTL recovery."""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from ..symbolic.resolver import (
    Field, build_net_to_node, resolve_field, resolve_inputs, split_bit,
)
from .models import RecoveredWord, SupportCut, WordAnalysis

if TYPE_CHECKING:
    from ..circuit import Circuit


def _group_port_words(nets: list[str], direction: str) -> tuple[RecoveredWord, ...]:
    order: OrderedDict[str, list[tuple[int, str]]] = OrderedDict()
    scalars: set[str] = set()
    for net in nets:
        base, idx = split_bit(net)
        if base not in order:
            order[base] = []
        if idx is None:
            scalars.add(base)
            order[base].append((0, net))
        else:
            order[base].append((idx, net))

    words: list[RecoveredWord] = []
    for base, pairs in order.items():
        pairs.sort()
        width = 1 if base in scalars else max(idx for idx, _ in pairs) + 1
        contiguous = len(pairs) == width and [idx for idx, _ in pairs] == list(range(width))
        evidence = ["declared port bus" if width > 1 else "declared scalar port"]
        confidence = 1.0 if contiguous else 0.75
        if not contiguous:
            evidence.append("non-contiguous bit indices")
        words.append(RecoveredWord(
            name=base,
            bits=tuple(net for _, net in pairs),
            direction=direction,
            width=width,
            confidence=confidence,
            evidence=tuple(evidence),
        ))
    return tuple(words)


def _compact_fields(fields: list[Field]) -> tuple[str, ...]:
    return tuple(field.compact() for field in fields)


def _support_cut(circuit: Circuit, target: Field) -> SupportCut:
    inputs = resolve_inputs(circuit, None, target, build_net_to_node(circuit))
    data = circuit.find_cone([str(nid) for nid in target.node_ids], "backward")
    evidence = [f"auto support has {len(inputs)} word(s)"]
    if inputs:
        evidence.append("support=" + ", ".join(field.compact() for field in inputs))
    return SupportCut(
        target=target.compact(),
        support_words=_compact_fields(inputs),
        cone_size=data["total_nodes"],
        evidence=tuple(evidence),
    )


def _pi_support_bases(circuit: Circuit, nid: int) -> set[str]:
    data = circuit.find_cone([str(nid)], "backward")
    out: set[str] = set()
    for pid in data["boundary"].get("pi_po", []):
        node = circuit.nodes[pid]
        if node.is_pi and node.net:
            base, _ = split_bit(node.net)
            out.add(base)
    return out


def _low_bit_evidence(circuit: Circuit, target: Field,
                      max_low_bits: int) -> tuple[str, ...]:
    if target.width <= 1:
        return ()
    bits = sorted(target.bits, key=lambda bit: bit.index)[:max_low_bits]
    supports = [_pi_support_bases(circuit, bit.node_id) for bit in bits]
    if len(supports) < 2:
        return ()
    increasing = sum(
        1 for a, b in zip(supports, supports[1:])
        if a.issubset(b)
    )
    total = len(supports) - 1
    return (
        f"low-bit fanin support inclusion: {increasing}/{total} adjacent pairs",
    )


def _resolve_output_fields(circuit: Circuit,
                           outputs: list[str] | None) -> list[Field]:
    net_to_node = build_net_to_node(circuit)
    if outputs:
        return [resolve_field(circuit, ref, net_to_node, role="target")
                for ref in outputs]
    fields: list[Field] = []
    seen: set[str] = set()
    for word in _group_port_words(circuit.output_nets, "output"):
        if word.name in seen:
            continue
        seen.add(word.name)
        fields.append(resolve_field(circuit, word.name, net_to_node, role="target"))
    return fields


def analyze_words(circuit: Circuit, outputs: list[str] | None = None,
                  max_low_bits: int = 10) -> WordAnalysis:
    """Recover declared word boundaries and per-output support cuts."""
    input_words = _group_port_words(circuit.input_nets, "input")
    output_words_raw = _group_port_words(circuit.output_nets, "output")

    output_fields = _resolve_output_fields(circuit, outputs)
    selected = {field.label.split("[", 1)[0] for field in output_fields}
    output_words: list[RecoveredWord] = []
    for word in output_words_raw:
        if outputs and word.name not in selected:
            continue
        evidence = list(word.evidence)
        try:
            field = resolve_field(circuit, word.name, role="target")
            evidence.extend(_low_bit_evidence(circuit, field, max_low_bits))
        except Exception:
            pass
        output_words.append(RecoveredWord(
            name=word.name,
            bits=word.bits,
            direction=word.direction,
            width=word.width,
            signed_state=word.signed_state,
            confidence=word.confidence,
            evidence=tuple(evidence),
        ))

    cuts = tuple(_support_cut(circuit, field) for field in output_fields)
    evidence = (
        "word discovery uses declared ports first",
        "support cuts use backward cone primary-input reachability",
    )
    if any(re.search(r"\[\d+\]", net) for net in circuit.output_nets):
        evidence += ("bus bit order inferred from numeric indices",)
    return WordAnalysis(
        circuit_name=circuit.name,
        input_words=input_words,
        output_words=tuple(output_words),
        support_cuts=cuts,
        evidence=evidence,
    )
