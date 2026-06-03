"""Non-destructive half/full adder extraction.

A half adder pairs a XOR (sum) with an AND (carry) over the same two inputs; a
full adder pairs a XOR3 (sum) with a MAJ (carry) over the same three inputs.  A
pair is accepted only when the two cones form a closed region: every internal
signal is consumed inside it and only the two roots escape (as outputs or to
external loads).  The matched gates stay in the original circuit; an
:class:`Adder` just records which nodes form the adder.

Because NOT is a node here, an inverter that reads a cone-internal signal from
outside counts as an external load and blocks the pairing — stricter than the
edge-negation model, by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .core import (
    cone_tt, dfs_cone, enumerate_cuts, match_and, match_maj, match_xor, match_xor3,
)
from .viz import write_dot

if TYPE_CHECKING:
    from ..circuit import Circuit


@dataclass
class Adder:
    kind: str            # "HA" or "FA"
    sum_root: int
    carry_root: int
    inputs: list[int]
    nodes: list[int]     # original gates in both cones


class AdderCircuit:
    def __init__(self, circuit: Circuit, adders: list[Adder]) -> None:
        self.circuit = circuit
        self.adders = adders

    def half_adders(self) -> list[Adder]:
        return [a for a in self.adders if a.kind == "HA"]

    def full_adders(self) -> list[Adder]:
        return [a for a in self.adders if a.kind == "FA"]

    def by_root(self) -> dict[int, Adder]:
        return {r: a for a in self.adders for r in (a.sum_root, a.carry_root)}

    def __len__(self) -> int:
        return len(self.adders)

    def _outputs_to_adder(self, adders: list[Adder]) -> dict[int, Adder]:
        out: dict[int, Adder] = {}
        for a in adders:
            out[a.sum_root] = a
            out[a.carry_root] = a
        return out

    def trees(self) -> list[list[Adder]]:
        """Adders grouped into connected trees (linked by carry/sum signals)."""
        out = self._outputs_to_adder(self.adders)
        index = {id(a): i for i, a in enumerate(self.adders)}
        adj: dict[int, set[int]] = {i: set() for i in range(len(self.adders))}
        for i, a in enumerate(self.adders):
            for leaf in a.inputs:
                src = out.get(leaf)
                if src is not None and index[id(src)] != i:
                    j = index[id(src)]
                    adj[i].add(j)
                    adj[j].add(i)

        seen: set[int] = set()
        trees: list[list[Adder]] = []
        for start in range(len(self.adders)):
            if start in seen:
                continue
            stack, comp = [start], []
            seen.add(start)
            while stack:
                u = stack.pop()
                comp.append(self.adders[u])
                for v in adj[u]:
                    if v not in seen:
                        seen.add(v)
                        stack.append(v)
            trees.append(comp)
        trees.sort(key=len, reverse=True)
        return trees

    def _depth(self, tree: list[Adder]) -> int:
        out = self._outputs_to_adder(tree)
        memo: dict[int, int] = {}

        def depth(a: Adder) -> int:
            if id(a) not in memo:
                memo[id(a)] = 1 + max(
                    (depth(out[l]) for l in a.inputs if l in out), default=0)
            return memo[id(a)]

        return max((depth(a) for a in tree), default=0)

    def _name(self, nid: int) -> str:
        return self.circuit.nodes[nid].net or f"#{nid}"

    def _signals(self, nids: list[int], limit: int = 12) -> str:
        def natural(name: str):
            return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]

        names = sorted((self._name(n) for n in nids), key=natural)
        if len(names) <= limit:
            return ", ".join(names) or "(none)"
        return ", ".join(names[:limit]) + f", (+{len(names) - limit} more)"

    def _boundary(self, tree: list[Adder]) -> tuple[list[int], list[int]]:
        """Signals entering / leaving an adder tree (its operands and results)."""
        produced, nodes = set(), set()
        for a in tree:
            produced.update((a.sum_root, a.carry_root))
            nodes.update(a.nodes)
        outputs = set(self.circuit.output_nets)

        inputs, seen = [], set()
        for a in tree:
            for sig in a.inputs:
                if sig not in produced and sig not in seen:
                    seen.add(sig)
                    inputs.append(sig)

        results = [sig for sig in produced
                   if self.circuit.nodes[sig].net in outputs
                   or any(f not in nodes and not self.circuit.nodes[f].is_po
                          for f in self.circuit.nodes[sig].fanouts)]
        return inputs, results

    def summary(self) -> str:
        return (f"AdderCircuit({self.circuit.name}, "
                f"HA={len(self.half_adders())}, FA={len(self.full_adders())})")

    def report(self) -> str:
        if not self.adders:
            return f"No adders found in {self.circuit.name}."
        trees = self.trees()
        big = trees[0]
        fa = sum(1 for a in big if a.kind == "FA")
        inputs, results = self._boundary(big)
        return "\n".join([
            f"Adder extraction on {self.circuit.name}",
            f"  adders found : {len(self.adders)} "
            f"({len(self.full_adders())} full, {len(self.half_adders())} half)",
            f"  adder trees  : {len(trees)} (groups linked by carry/sum signals)",
            f"  largest tree :",
            f"    adders     : {len(big)} ({fa} full, {len(big) - fa} half)",
            f"    carry depth: {self._depth(big)}",
            f"    operands in ({len(inputs)}) : {self._signals(inputs)}",
            f"    results out ({len(results)}): {self._signals(results)}",
        ])

    def __repr__(self) -> str:
        return self.summary()

    def to_dot(self, filename: str) -> None:
        groups = [(f"{a.kind} {i}", a.nodes) for i, a in enumerate(self.adders)]
        write_dot(self.circuit, groups, filename)


def extract_adders(circuit: Circuit) -> AdderCircuit:
    cuts = enumerate_cuts(circuit, 3)
    # cut -> (sum candidates, carry candidates); each candidate is (root, nodes)
    ha: dict[frozenset, tuple[list, list]] = {}
    fa: dict[frozenset, tuple[list, list]] = {}

    for root in circuit.gate_nodes:
        for cut in cuts.get(root, ()):
            n = len(cut)
            if n not in (2, 3):
                continue
            inputs, nodes = dfs_cone(circuit, root, cut)
            if len(inputs) != n:
                continue
            tt = cone_tt(circuit, inputs, nodes, root)
            if n == 2:
                sums, carries = ha.setdefault(cut, ([], []))
                if match_xor(tt):
                    sums.append((root, nodes))
                elif match_and(tt):
                    carries.append((root, nodes))
            else:
                sums, carries = fa.setdefault(cut, ([], []))
                if match_xor3(tt):
                    sums.append((root, nodes))
                elif match_maj(tt):
                    carries.append((root, nodes))

    output_nets = set(circuit.output_nets)

    def escapes(nid: int, region: set[int]) -> bool:
        if circuit.nodes[nid].net in output_nets:
            return True
        return any(f not in region and not circuit.nodes[f].is_po
                   for f in circuit.nodes[nid].fanouts)

    def closed(s_nodes, c_nodes, s_root, c_root) -> bool:
        region = set(s_nodes) | set(c_nodes)
        roots = {s_root, c_root}
        return all(escapes(net, region) == (net in roots) for net in region)

    adders: list[Adder] = []
    consumed: set[int] = set()

    def pair(table, kind):
        for cut, (sums, carries) in table.items():
            for s_root, s_nodes in sums:
                for c_root, c_nodes in carries:
                    if s_root in consumed or c_root in consumed:
                        continue
                    if not closed(s_nodes, c_nodes, s_root, c_root):
                        continue
                    adders.append(Adder(kind, s_root, c_root, sorted(cut),
                                        sorted(set(s_nodes) | set(c_nodes))))
                    consumed.update((s_root, c_root))

    pair(fa, "FA")
    pair(ha, "HA")
    return AdderCircuit(circuit, adders)
