"""Resolve Verilog-ish signal references into typed symbolic fields."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..circuit import Circuit


_BIT_RE = re.compile(r"^(.*)\[(\d+)\]$")
_SLICE_RE = re.compile(r"^(.*)\[(\d+)\s*:\s*(\d+)\]$")


def split_bit(net: str) -> tuple[str, int | None]:
    m = _BIT_RE.match(net)
    if m:
        return m.group(1), int(m.group(2))
    return net, None


def natural_key(name: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


@dataclass(frozen=True)
class FieldBit:
    index: int
    node_id: int
    net: str


@dataclass(frozen=True)
class Field:
    """A scalar or bus represented as LSB-indexed circuit node bits."""

    label: str
    bits: tuple[FieldBit, ...]
    role: str = ""

    @property
    def width(self) -> int:
        return max((bit.index for bit in self.bits), default=-1) + 1

    @property
    def nets(self) -> list[str]:
        return [bit.net for bit in self.bits]

    @property
    def node_ids(self) -> list[int]:
        return [bit.node_id for bit in self.bits]

    def value_from_nodes(self, values: dict[int, int]) -> int:
        out = 0
        for bit in self.bits:
            out |= (values[bit.node_id] & 1) << bit.index
        return out

    def compact(self) -> str:
        if self.width == 1 and len(self.bits) == 1 and "[" not in self.label:
            return self.label
        return f"{self.label}[{self.width}]"


def build_net_to_node(circuit: Circuit) -> dict[str, int]:
    """Map net name to its producing node, preferring non-PO drivers."""
    net_to_node: dict[str, int] = {}
    for nid, node in circuit.nodes.items():
        if not node.net:
            continue
        if node.net not in net_to_node or not node.is_po:
            net_to_node[node.net] = nid
    return net_to_node


def _bus_bits(circuit: Circuit, base: str,
              net_to_node: dict[str, int]) -> dict[int, FieldBit]:
    bits: dict[int, FieldBit] = {}
    prefix = base + "["
    for net, nid in net_to_node.items():
        if not net.startswith(prefix) or not net.endswith("]"):
            continue
        try:
            idx = int(net[len(prefix):-1])
        except ValueError:
            continue
        bits[idx] = FieldBit(idx, nid, net)
    return bits


def resolve_field(circuit: Circuit, ref: str,
                  net_to_node: dict[str, int] | None = None,
                  *, role: str = "") -> Field:
    """Resolve a scalar, bit, part-select, or bus base into a :class:`Field`."""
    net_to_node = net_to_node or build_net_to_node(circuit)
    s = ref.strip()

    m = _SLICE_RE.match(s)
    if m:
        base, hi_s, lo_s = m.group(1), m.group(2), m.group(3)
        hi, lo = int(hi_s), int(lo_s)
        if hi < lo:
            hi, lo = lo, hi
        bus = _bus_bits(circuit, base, net_to_node)
        missing = [idx for idx in range(lo, hi + 1) if idx not in bus]
        if missing:
            raise KeyError(f"no bit(s) {missing} for slice {s!r}")
        bits = tuple(FieldBit(idx - lo, bus[idx].node_id, bus[idx].net)
                     for idx in range(lo, hi + 1))
        return Field(s, bits, role)

    m = _BIT_RE.match(s)
    if m:
        if s not in net_to_node:
            raise KeyError(f"no signal matching {s!r}")
        return Field(s, (FieldBit(0, net_to_node[s], s),), role)

    if s in net_to_node:
        return Field(s, (FieldBit(0, net_to_node[s], s),), role)

    bus = _bus_bits(circuit, s, net_to_node)
    if bus:
        bits = tuple(bus[idx] for idx in sorted(bus))
        return Field(s, bits, role)

    nid = circuit._node_by_ref(s)
    if nid is not None:
        net = circuit.nodes[nid].net or f"#{nid}"
        return Field(s, (FieldBit(0, nid, net),), role)

    raise KeyError(f"no signal or bus matching {s!r}")


def auto_input_fields(circuit: Circuit, target: Field,
                      net_to_node: dict[str, int] | None = None) -> list[Field]:
    """Infer primary-input support for *target* by backward cone traversal."""
    net_to_node = net_to_node or build_net_to_node(circuit)
    data = circuit.find_cone([str(nid) for nid in target.node_ids], "backward")
    pi_nodes = [
        nid for nid in data["boundary"].get("pi_po", [])
        if circuit.nodes[nid].is_pi
    ]
    if not pi_nodes:
        return []

    grouped: dict[str, list[tuple[int, int, str]]] = {}
    for nid in pi_nodes:
        net = circuit.nodes[nid].net
        base, idx = split_bit(net)
        grouped.setdefault(base, []).append((0 if idx is None else idx, nid, net))

    fields: list[Field] = []
    for base in sorted(grouped, key=natural_key):
        triples = sorted(grouped[base])
        bits = tuple(FieldBit(idx, nid, net) for idx, nid, net in triples)
        fields.append(Field(base, bits, "input"))
    return fields


def resolve_inputs(circuit: Circuit, refs: list[str] | None, target: Field,
                   net_to_node: dict[str, int] | None = None) -> list[Field]:
    net_to_node = net_to_node or build_net_to_node(circuit)
    if refs is None:
        return auto_input_fields(circuit, target, net_to_node)
    return [resolve_field(circuit, ref, net_to_node, role="input") for ref in refs]
