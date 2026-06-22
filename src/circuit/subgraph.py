"""General backward-cone sub-circuit extraction.

Given a set of *output* signals (and, optionally, a set of *input* signals to
cut at), collect every node on a path into an output by walking fan-in until a
cut signal, a primary input, or a constant source is reached.  Those cut points
and primary inputs become the sub-circuit's inputs; constants are kept inline;
the outputs become its outputs.

This is deliberately *total*: it never fails on a "missing" input.  Any fan-in
not covered by the given inputs simply surfaces as a fresh primary input — the
sub-circuit's true support set.  (The previous windowed extractor rejected such
cases with "outside the subgraph", forcing the caller to pre-list every
dependency; this version discovers them instead.)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .node import PI, PO

if TYPE_CHECKING:
    from .circuit import Circuit


def extract_subgraph(circuit: Circuit, output_signals: list[str],
                     input_signals: list[str] | None = None) -> Circuit:
    """Extract the backward cone feeding *output_signals*.

    Args:
        circuit: the source circuit.
        output_signals: signals that become the sub-circuit's primary outputs
            (order preserved, duplicates collapsed).
        input_signals: optional signals to cut the fan-in walk at.  Each one
            that is actually reached becomes a primary input; the walk does not
            go past it.  Fan-in not covered here is followed all the way to the
            circuit's own primary inputs, which then become inputs too.

    Returns:
        A new :class:`Circuit` whose primary inputs are the reached cut signals
        plus every primary input the outputs transitively depend on, and whose
        primary outputs are *output_signals*.

    Raises:
        KeyError: if an output or input signal name cannot be resolved.
        ValueError: if *output_signals* is empty.
    """
    # -- resolve outputs (preserve order, dedupe) ----------------------
    out_ids: list[int] = []
    seen_out: set[int] = set()
    for s in output_signals:
        nid = circuit._node_by_ref(s)
        if nid is None:
            raise KeyError(f"unresolved output signal: {s!r}")
        if nid not in seen_out:
            seen_out.add(nid)
            out_ids.append(nid)
    if not out_ids:
        raise ValueError("no output signals given")

    # -- resolve optional cut inputs -----------------------------------
    cut_ids: set[int] = set()
    for s in (input_signals or []):
        nid = circuit._node_by_ref(s)
        if nid is None:
            raise KeyError(f"unresolved input signal: {s!r}")
        cut_ids.add(nid)

    # -- backward cone via the shared traversal core -------------------
    # The same walk that powers find_cone(direction="backward"): nodes reached
    # but not expanded (here: cut signals and PIs) are the boundary.  We then
    # split every reached node into a *leaf* (becomes a PI) or an *internal*
    # node to copy.  A node is a leaf when the caller cut there or it is a PI;
    # constants are NOT leaves — they stay inline (the writer emits literals),
    # so the sub-circuit needs no input for them.
    dist, _ = circuit._cone_reachable(out_ids, "backward", stop_ids=cut_ids)
    sub: set[int] = set()        # internal gate/const nodes to copy
    leaves: list[int] = []        # cut / PI nodes -> sub-circuit PIs
    for nid in dist:
        if nid in cut_ids or circuit.nodes[nid].is_pi:
            leaves.append(nid)
        else:
            sub.add(nid)

    return _rebuild(circuit, out_ids, sub, leaves)


def _natural_key(name: str):
    """Sort key that orders ``io_a[2]`` before ``io_a[10]``."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _rebuild(circuit: Circuit, out_ids: list[int], sub: set[int],
             leaves: list[int]) -> Circuit:
    from .circuit import Circuit as C

    result = C()
    old_to_new: dict[int, int] = {}

    # 1. leaves -> PIs, ordered by natural net name for readable ports.
    leaves_sorted = sorted(
        leaves, key=lambda nid: _natural_key(circuit.nodes[nid].net or f"#{nid}"))
    for old in leaves_sorted:
        net = circuit.nodes[old].net or f"i{old}"
        old_to_new[old] = result._add_node(PI, net)
        result.input_nets.append(net)

    # 2. internal nodes in topological order (sources first), skipping PO sinks
    #    (a PO given directly as an output is materialised in step 3).  Every
    #    fan-in is already mapped: it is either a leaf (step 1) or an earlier
    #    sub node (topological order guarantees it).
    out_set = set(out_ids)
    for old in circuit.topological_order():
        if old not in sub or circuit.nodes[old].is_po:
            continue
        node = circuit.nodes[old]
        new = result._add_node(node.kind, node.net or "",
                               logic=node.logic,
                               inputs=[old_to_new[c] for c in node.inputs])
        old_to_new[old] = new

    # 3. outputs -> POs.  An output may be a gate (mapped in step 2), a leaf
    #    PI (feed-through, mapped in step 1), or a PO sink (use its driver).
    seen_po: set[str] = set()
    for old in out_ids:
        node = circuit.nodes[old]
        net = node.net or f"o{old}"
        if net in seen_po:
            continue
        seen_po.add(net)
        if node.is_po:
            driver = old_to_new[node.inputs[0]]
        else:
            driver = old_to_new[old]
        result._add_node(PO, net, inputs=[driver])
        result.output_nets.append(net)

    # fan-out bookkeeping
    for n in result.nodes.values():
        for src in n.inputs:
            result.nodes[src].fanouts.append(n.node_id)

    result.name = circuit.name + "_sub"
    return result
