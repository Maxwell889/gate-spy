"""Word-level views over bit-level circuit ports and cones."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..circuit import Circuit

_BIT_RE = re.compile(r"^(.*)\[(\d+)\]$")


@dataclass(frozen=True)
class Word:
    """A bus/scalar grouped from bit-level net names."""

    name: str
    width: int
    bits: tuple[tuple[int, str], ...]
    kind: str

    @property
    def nets_lsb_first(self) -> list[str]:
        return [net for _, net in self.bits]

    @property
    def nets_msb_first(self) -> list[str]:
        return [net for _, net in sorted(self.bits, reverse=True)]

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1


def split_bit(net: str) -> tuple[str, int | None]:
    m = _BIT_RE.match(net)
    if m:
        return m.group(1), int(m.group(2))
    return net, None


def collect_words(nets: list[str], kind: str) -> dict[str, Word]:
    """Group bit nets into words while preserving first-seen base order."""
    order: list[str] = []
    grouped: dict[str, list[tuple[int, str]]] = {}
    for net in nets:
        base, idx = split_bit(net)
        if base not in grouped:
            order.append(base)
            grouped[base] = []
        grouped[base].append((0 if idx is None else idx, net))

    words: dict[str, Word] = {}
    for base in order:
        bits = tuple(sorted(grouped[base], key=lambda x: x[0]))
        width = 1 if len(bits) == 1 and split_bit(bits[0][1])[1] is None else bits[-1][0] + 1
        words[base] = Word(base, width, bits, kind)
    return words


def io_words(circuit: "Circuit") -> tuple[dict[str, Word], dict[str, Word]]:
    return (
        collect_words(circuit.input_nets, "input"),
        collect_words(circuit.output_nets, "output"),
    )


def word_value(word: Word, bits: dict[str, int]) -> int:
    val = 0
    for idx, net in word.bits:
        if bits.get(net, 0):
            val |= 1 << idx
    return val & word.mask


def expand_word_value(word: Word, value: int) -> dict[str, int]:
    return {net: (int(value) >> idx) & 1 for idx, net in word.bits}


def support_words(circuit: "Circuit", output: Word,
                  input_words: dict[str, Word]) -> list[Word]:
    """Return PI words reached by the backward cone for *output*."""
    data = circuit.find_cone(output.nets_lsb_first, "backward")
    reached: set[str] = set()
    for nid in data.get("boundary", {}).get("pi_po", []):
        net = circuit.nodes[nid].net
        if not net:
            continue
        base, _ = split_bit(net)
        if base in input_words:
            reached.add(base)
    return [w for name, w in input_words.items() if name in reached]


def cone_gate_histogram(circuit: "Circuit", output: Word) -> dict[str, int]:
    data = circuit.find_cone(output.nets_lsb_first, "backward")
    hist: dict[str, int] = {}
    for layer in data.get("layers", []):
        for nid in layer.get("node_ids", []):
            kind = circuit.nodes[nid].kind
            hist[kind] = hist.get(kind, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def format_words(words: list[Word]) -> str:
    if not words:
        return "(none)"
    return ", ".join(f"{w.name}[{w.width}]" for w in words)
