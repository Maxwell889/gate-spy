"""Subgraph extraction between given input/output signal boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .node import PI, PO

if TYPE_CHECKING:
    from .circuit import Circuit


def extract_subgraph(circuit: Circuit, input_signals: list[str],
                     output_signals: list[str]) -> Circuit:
    in_ids = {circuit._node_by_ref(s) for s in input_signals}
    out_ids = {circuit._node_by_ref(s) for s in output_signals}
    if None in in_ids or None in out_ids:
        missing = [s for s in input_signals + output_signals
                   if circuit._node_by_ref(s) is None]
        raise KeyError(f"unresolved signal(s): {missing}")

    common = in_ids & out_ids
    if common:
        names = sorted(circuit.nodes[i].net or f"#{i}" for i in common)
        raise ValueError(f"signal(s) cannot be both input and output: {names}")

    # forward cone from inputs, stopping at outputs
    fwd: set[int] = set()
    stack = list(in_ids)
    while stack:
        nid = stack.pop()
        if nid in fwd:
            continue
        fwd.add(nid)
        if nid in out_ids:
            continue
        for nb in circuit.nodes[nid].fanouts:
            if nb not in fwd:
                stack.append(nb)

    # backward cone from outputs, stopping at inputs
    bwd: set[int] = set()
    stack = list(out_ids)
    while stack:
        nid = stack.pop()
        if nid in bwd:
            continue
        bwd.add(nid)
        if nid in in_ids:
            continue
        for nb in circuit.nodes[nid].inputs:
            if nb not in bwd:
                stack.append(nb)

    sub = fwd & bwd
    if not sub:
        raise ValueError("no nodes in subgraph — input/output cones are disjoint")

    # boundary check: every internal node's fan-in must be in (sub ∪ in_ids)
    for nid in sub:
        node = circuit.nodes[nid]
        for child in node.inputs:
            if child not in sub and child not in in_ids:
                cn = circuit.nodes[child]
                raise ValueError(
                    f"node '{node.net or nid}' ({node.kind}) depends on "
                    f"'{cn.net or child}' ({cn.kind}), outside the subgraph. "
                    f"Add it to the input set."
                )

    return _rebuild(circuit, sub, in_ids, out_ids)


def _rebuild(circuit: Circuit, sub: set[int], in_ids: set[int],
             out_ids: set[int]) -> Circuit:
    from .circuit import Circuit as C

    result = C(circuit.lib)
    old_to_new: dict[int, int] = {}

    # input boundary (non-output) → PIs
    for old in in_ids - out_ids:
        net = circuit.nodes[old].net or f"i{old}"
        old_to_new[old] = result._add_node(PI, net)
        result.input_nets.append(net)

    # inputs that are also outputs (feed-through) → PIs
    for old in in_ids & out_ids:
        net = circuit.nodes[old].net or f"i{old}"
        old_to_new[old] = result._add_node(PI, net)
        result.input_nets.append(net)

    # internal nodes in topological order (skip boundary inputs and PO sinks)
    for old in (n for n in circuit.topological_order()
                if n in sub - in_ids and not circuit.nodes[n].is_po):
        node = circuit.nodes[old]
        new = result._add_node(node.kind, node.net or "",
                               cell=node.cell, logic=node.logic,
                               inputs=[old_to_new[c] for c in node.inputs])
        old_to_new[old] = new

    # output nodes → POs
    for old in out_ids:
        net = circuit.nodes[old].net or f"o{old}"
        node = circuit.nodes[old]
        if node.is_po:
            driver = old_to_new[node.inputs[0]]  # PO sink → use its driver
        else:
            driver = old_to_new[old]             # gate output directly
        result._add_node(PO, net, inputs=[driver])
        result.output_nets.append(net)

    for n in result.nodes.values():
        for src in n.inputs:
            result.nodes[src].fanouts.append(n.node_id)

    result.name = circuit.name + "_sub"
    return result
