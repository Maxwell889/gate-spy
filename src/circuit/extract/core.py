"""Cut enumeration, logic cones and gate matching.

Each node drives one signal, so a node id is also a signal id and a NOT is just
a 1-input internal node.  We enumerate k-feasible cuts, build the cone between a
root and a cut, simulate its partial truth table, and test that truth table
against a target gate up to input/output negation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..circuit import Circuit

CUT_LIMIT = 20


def _merge(a_cuts, b_cuts, k, limit):
    res: list[frozenset] = []
    for a in a_cuts:
        for b in b_cuts:
            m = a | b
            if len(m) > k or any(e <= m for e in res):
                continue
            res.append(m)
    res.sort(key=len)
    return res[:limit]


def enumerate_cuts(circuit: Circuit, k: int, limit: int = CUT_LIMIT) -> dict[int, list[frozenset]]:
    cuts: dict[int, list[frozenset]] = {}
    for nid in circuit.topological_order():
        node = circuit.nodes[nid]
        if node.is_po:
            continue
        if not node.inputs:
            cuts[nid] = [frozenset((nid,))]
            continue
        merged = list(cuts[node.inputs[0]])
        for child in node.inputs[1:]:
            merged = _merge(merged, cuts[child], k, limit)
            if not merged:
                break
        merged.append(frozenset((nid,)))
        cuts[nid] = merged
    return cuts


def dfs_cone(circuit: Circuit, root: int, cut: frozenset) -> tuple[list[int], list[int]]:
    """Return (leaf signals, internal nodes in evaluation order)."""
    inputs: list[int] = []
    nodes: list[int] = []
    seen: set[int] = set()
    stack: list[tuple[int, bool]] = [(root, False)]
    while stack:
        nid, done = stack.pop()
        if done:
            nodes.append(nid)
            continue
        if nid in seen:
            continue
        seen.add(nid)
        node = circuit.nodes[nid]
        if nid in cut or not node.inputs:
            inputs.append(nid)
            continue
        stack.append((nid, True))
        for child in reversed(node.inputs):
            if child not in seen:
                stack.append((child, False))
    return inputs, nodes


def cone_tt(circuit: Circuit, inputs: list[int], nodes: list[int], root: int) -> list[bool]:
    k = len(inputs)
    table: list[bool] = []
    for mask in range(1 << k):
        value = {leaf: (mask >> i) & 1 for i, leaf in enumerate(inputs)}
        for nid in nodes:
            node = circuit.nodes[nid]
            value[nid] = node.logic(*(value[c] for c in node.inputs))
        table.append(bool(value[root]))
    return table


_XOR3 = [False, True, True, False, True, False, False, True]


def match_xor(tt) -> bool:
    return tt == [False, True, True, False] or tt == [True, False, False, True]


def match_and(tt) -> bool:
    return sum(tt) in (1, 3)        # AND/NAND up to input negation


def match_xor3(tt) -> bool:
    return tt == _XOR3 or tt == [not x for x in _XOR3]


def match_maj(tt) -> bool:
    for mask in range(8):
        negs = [(mask >> i) & 1 for i in range(3)]
        exp = [sum(((i >> b) & 1) ^ negs[b] for b in range(3)) >= 2 for i in range(8)]
        if tt == exp or all(a != b for a, b in zip(tt, exp)):
            return True
    return False
