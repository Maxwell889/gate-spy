"""Graphviz export that boxes extracted units over the original circuit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..node import CONST0, CONST1, CONSTX

if TYPE_CHECKING:
    from ..circuit import Circuit

_CLUSTER_COLORS = ["#E74C3C", "#2E86C1", "#27AE60", "#8E44AD", "#E67E22", "#16A085"]


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _node_line(circuit: Circuit, nid: int) -> str:
    node = circuit.nodes[nid]
    if node.is_pi:
        return f'n{nid} [label="{_esc(node.net or "PI")}", shape=box, style=filled, fillcolor="#D4E6F1"];'
    if node.is_po:
        return f'n{nid} [label="{_esc(node.net or "PO")}", shape=box, style=filled, fillcolor="#FADBD8"];'
    if node.kind in (CONST0, CONST1, CONSTX):
        return f'n{nid} [label="{node.kind}", shape=diamond, style=filled, fillcolor="#F0F0F0"];'
    return f'n{nid} [label="{node.kind}", shape=ellipse, style=filled, fillcolor="#FCF3CF"];'


def write_dot(circuit: Circuit, groups: list[tuple[str, list[int]]], filename: str) -> None:
    """Render *circuit*, clustering each (label, nodes) group.

    A node shared by several groups is drawn in the first that claims it.
    """
    owner: dict[int, int] = {}
    for gi, (_, nodes) in enumerate(groups):
        for nid in nodes:
            owner.setdefault(nid, gi)
    members: dict[int, list[int]] = {}
    for nid, gi in owner.items():
        members.setdefault(gi, []).append(nid)

    with open(filename, "w") as f:
        f.write(f"digraph {_esc(circuit.name)} {{\n  rankdir=LR;\n")
        f.write('  node [fontname="monospace", fontsize=10];\n\n')

        for gi, (label, _) in enumerate(groups):
            if gi not in members:
                continue
            color = _CLUSTER_COLORS[gi % len(_CLUSTER_COLORS)]
            f.write(f'  subgraph cluster_{gi} {{\n')
            f.write(f'    label="{_esc(label)}"; style=rounded; color="{color}"; fontcolor="{color}";\n')
            for nid in members[gi]:
                f.write("    " + _node_line(circuit, nid) + "\n")
            f.write("  }\n")

        for nid in circuit.nodes:
            if nid not in owner:
                f.write("  " + _node_line(circuit, nid) + "\n")

        pis = [f"n{n}" for n in circuit.pi_nodes]
        pos = [f"n{n}" for n in circuit.po_nodes]
        if pis:
            f.write(f'\n  {{rank=source; {" ".join(pis)}}}\n')
        if pos:
            f.write(f'  {{rank=sink; {" ".join(pos)}}}\n')

        f.write("\n")
        for nid, node in circuit.nodes.items():
            for src in node.inputs:
                f.write(f"  n{src} -> n{nid};\n")
        f.write("}\n")
