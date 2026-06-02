"""Gate-level netlist as a DAG of single-output nodes.

:class:`Circuit` models a technology-mapped, flattened gate-level netlist at
**bit granularity**: each node drives exactly one bit (PI, PO, constant, or a
library gate).
"""

from __future__ import annotations

import re
import sys

from ..library import Cell, Library, LogicFn
from .node import Node, CONST0, CONST1, CONSTX, _CONST_KIND
from .parser import parse as _parse_verilog

def _strip_meta(text: str) -> str:
    """Remove comments and ``(* ... *)`` attributes from Verilog source."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"\(\*.*?\*\)", "", text, flags=re.DOTALL)
    return text


import colorsys


def _make_palette(kinds: set[str]) -> dict[str, str]:
    """Assign a visually distinct pastel colour to each gate type.

    Hues are evenly distributed around the colour wheel.  Saturation and
    lightness are fixed to keep the palette soft and readable on white
    backgrounds.
    """
    palette: dict[str, str] = {}
    n = len(kinds)
    if n == 0:
        return palette

    s, l = 0.55, 0.82  # pastel saturation / lightness
    for i, kind in enumerate(sorted(kinds)):
        h = i / n
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        palette[kind] = f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
    return palette


# ---------------------------------------------------------------------------
# Circuit
# ---------------------------------------------------------------------------

class Circuit:
    """A bit-level gate netlist parsed from yosys-flattened Verilog."""

    def __init__(self, lib: Library) -> None:
        self.lib = lib
        self.name = ""
        self.nodes: dict[int, Node] = {}

        # Primary I/O bit nets, in declaration order (MSB first within a bus).
        self.input_nets: list[str] = []
        self.output_nets: list[str] = []

        self._next_id = 0
        self._driver: dict[str, int] = {}   # net -> node id that drives it
        self._alias: dict[str, str] = {}     # net -> net it is defined equal to
        self._topo: list[int] | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, verilog_path: str, lib: Library) -> Circuit:
        with open(verilog_path) as f:
            return cls.from_string(f.read(), lib)

    @classmethod
    def from_string(cls, text: str, lib: Library) -> Circuit:
        circuit = cls(lib)
        _parse_verilog(circuit, _strip_meta(text))
        return circuit

    def _add_node(self, kind: str, net: str = "", *, cell: Cell | None = None,
                  logic: LogicFn | None = None, inputs: list[int] | None = None) -> int:
        nid = self._next_id
        self._next_id += 1
        self.nodes[nid] = Node(
            node_id=nid, kind=kind, net=net,
            inputs=inputs or [], cell=cell, logic=logic,
        )
        return nid

    def _resolve(self, net: str) -> int:
        """Return the node id driving *net*, following alias chains.

        Constant nets materialise constant nodes on demand; a net with no
        driver at all becomes a dangling source (warned once).
        """
        canon = self._chase(net)
        if canon in self._driver:
            return self._driver[canon]
        if canon in _CONST_KIND:
            nid = self._add_node(_CONST_KIND[canon], canon)
            self._driver[canon] = nid
            return nid
        # Genuinely undriven — keep the DAG total by inserting a 0 source.
        print(f"Warning: net '{net}' has no driver; tying to 0", file=sys.stderr)
        nid = self._add_node(CONST0, canon)
        self._driver[canon] = nid
        return nid

    def _chase(self, net: str) -> str:
        seen: set[str] = set()
        while net in self._alias and net not in seen:
            seen.add(net)
            net = self._alias[net]
        return net

    # ------------------------------------------------------------------
    # Topology / queries
    # ------------------------------------------------------------------

    @property
    def pi_nodes(self) -> list[int]:
        return [n.node_id for n in self.nodes.values() if n.is_pi]

    @property
    def po_nodes(self) -> list[int]:
        return [n.node_id for n in self.nodes.values() if n.is_po]

    @property
    def gate_nodes(self) -> list[int]:
        return [n.node_id for n in self.nodes.values() if n.is_gate]

    def topological_order(self) -> list[int]:
        """Node ids in a topological order (sources first, sinks last)."""
        if self._topo is not None:
            return self._topo

        indegree = {nid: len(node.inputs) for nid, node in self.nodes.items()}
        queue = [nid for nid, d in indegree.items() if d == 0]
        order: list[int] = []
        while queue:
            nid = queue.pop()
            order.append(nid)
            for out in self.nodes[nid].fanouts:
                indegree[out] -= 1
                if indegree[out] == 0:
                    queue.append(out)

        if len(order) != len(self.nodes):
            raise ValueError("circuit is not acyclic (combinational loop detected)")
        self._topo = order
        return order

    def simulate(self, inputs: dict[str, int]) -> dict[str, int]:
        """Evaluate the circuit for the given primary-input bit values.

        Args:
            inputs: maps primary-input net names (e.g. ``"io_a[3]"`` or a scalar
                port name) to ``0`` / ``1``.  Unspecified inputs default to 0.

        Returns:
            Maps each primary-output net name to its computed bit value.
        """
        value: dict[int, int] = {}
        const_value = {CONST0: 0, CONST1: 1, CONSTX: 0}

        for nid in self.topological_order():
            node = self.nodes[nid]
            if node.kind in const_value:
                value[nid] = const_value[node.kind]
            elif node.is_pi:
                value[nid] = inputs.get(node.net, 0)
            elif node.is_po:
                value[nid] = value[node.inputs[0]] if node.inputs else 0
            else:  # library gate
                assert node.logic is not None
                value[nid] = node.logic(*(value[i] for i in node.inputs))

        return {self.nodes[nid].net: value[nid] for nid in self.po_nodes}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def gate_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for nid in self.gate_nodes:
            kind = self.nodes[nid].kind
            hist[kind] = hist.get(kind, 0) + 1
        return hist

    def summary(self) -> str:
        parts = [f"{k}:{v}" for k, v in sorted(self.gate_histogram().items())]
        return (f"Circuit({self.name}, PIs={len(self.pi_nodes)}, "
                f"POs={len(self.po_nodes)}, gates={len(self.gate_nodes)} "
                f"[{', '.join(parts)}])")

    def __repr__(self) -> str:
        return self.summary()

    # ------------------------------------------------------------------
    # DOT / Graphviz export
    # ------------------------------------------------------------------

    @staticmethod
    def _dot_escape(label: str) -> str:
        """Escape special characters for a DOT quoted string."""
        return label.replace("\\", "\\\\").replace('"', '\\"')

    def to_dot(self, filename: str) -> None:
        """Write a Graphviz DOT file representing the circuit DAG.

        * PI nodes are light steel-blue **boxes**.
        * PO nodes are light salmon **boxes**.
        * Constant nodes are gray **diamonds**.
        * Library gates are coloured **ellipses** (distinct pastel per gate type).

        Render with: ``dot -Tpng filename -o out.png``
        """
        with open(filename, "w") as f:
            f.write(f"digraph {self._dot_escape(self.name)} {{\n")
            f.write("  rankdir=LR;\n")
            f.write('  node [fontname="monospace", fontsize=10];\n')
            f.write("\n")

            pi_ids: list[str] = []
            po_ids: list[str] = []
            palette = _make_palette(set(self.gate_histogram()))

            for nid, node in self.nodes.items():
                if node.is_pi:
                    pi_ids.append(f"n{nid}")
                    label = self._dot_escape(node.net or "PI")
                    f.write(
                        f'  n{nid} [label="{label}", shape=box, style=filled, '
                        f'fillcolor="#D4E6F1"];\n'
                    )
                elif node.is_po:
                    po_ids.append(f"n{nid}")
                    label = self._dot_escape(node.net or "PO")
                    f.write(
                        f'  n{nid} [label="{label}", shape=box, style=filled, '
                        f'fillcolor="#FADBD8"];\n'
                    )
                elif node.kind in (CONST0, CONST1, CONSTX):
                    f.write(
                        f'  n{nid} [label="{node.kind}", shape=diamond, '
                        f'style=filled, fillcolor="#F0F0F0"];\n'
                    )
                else:  # library gate
                    color = palette.get(node.kind, "#FFFFFF")
                    f.write(
                        f'  n{nid} [label="{node.kind}", shape=ellipse, '
                        f'style=filled, fillcolor="{color}"];\n'
                    )

            # Rank constraints — PI on the left, PO on the right.
            f.write("\n")
            if pi_ids:
                f.write(f'  {{rank=source; {" ".join(pi_ids)}}}\n')
            if po_ids:
                f.write(f'  {{rank=sink; {" ".join(po_ids)}}}\n')

            # Edges — source node -> consuming node.
            f.write("\n")
            for nid, node in self.nodes.items():
                for src in node.inputs:
                    f.write(f"  n{src} -> n{nid};\n")

            f.write("}\n")
