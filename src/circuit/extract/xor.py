"""Non-destructive XOR extraction.

Wraps a circuit with the XOR gates discovered in it.  Each :class:`Xor` records
the original nodes that realise one 2-input XOR; the circuit itself is never
modified and all its gates remain traversable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .core import cone_tt, dfs_cone, enumerate_cuts, match_xor
from .viz import write_dot

if TYPE_CHECKING:
    from ..circuit import Circuit


@dataclass
class Xor:
    root: int            # node driving the XOR output
    inputs: list[int]    # the two operand signals
    nodes: list[int]     # original gates realising the XOR


class XorCircuit:
    def __init__(self, circuit: Circuit, xors: list[Xor]) -> None:
        self.circuit = circuit
        self.xors = xors

    def by_root(self) -> dict[int, Xor]:
        return {x.root: x for x in self.xors}

    def __len__(self) -> int:
        return len(self.xors)

    def _chain_depths(self) -> tuple[dict[int, int], dict[int, Xor]]:
        """Longest run of XORs ending at each XOR (chained via shared signals)."""
        out = {x.root: x for x in self.xors}
        depth: dict[int, int] = {}

        def chain(root: int) -> int:
            if root not in depth:
                x = out[root]
                depth[root] = 1 + max(
                    (chain(out[i].root) for i in x.inputs if i in out), default=0)
            return depth[root]

        for x in self.xors:
            chain(x.root)
        return depth, out

    def longest_chain(self) -> list[int]:
        """The longest XOR chain, as signal node ids from source to sink."""
        if not self.xors:
            return []
        depth, out = self._chain_depths()
        cur = max(self.xors, key=lambda x: depth[x.root]).root
        chain = []
        while cur is not None:
            chain.append(cur)
            ups = [i for i in out[cur].inputs if i in out]
            cur = max(ups, key=lambda i: depth[i]) if ups else None
        chain.reverse()
        return chain

    def _name(self, nid: int) -> str:
        return self.circuit.nodes[nid].net or f"#{nid}"

    def summary(self) -> str:
        return f"XorCircuit({self.circuit.name}, xor={len(self.xors)})"

    def report(self) -> str:
        if not self.xors:
            return f"No XOR gates found in {self.circuit.name}."
        chain = self.longest_chain()
        names = [self._name(r) for r in chain]
        path = " -> ".join(names)
        return (
            f"XOR extraction on {self.circuit.name}\n"
            f"  XOR gates found : {len(self.xors)}\n"
            f"  longest chain   : {len(chain)} XORs deep "
            f"(a XOR feeding the next via a shared signal)\n"
            f"  chain signals   :\n    {path}"
        )

    def __repr__(self) -> str:
        return self.summary()

    def to_dot(self, filename: str) -> None:
        groups = [(f"XOR {i}", x.nodes) for i, x in enumerate(self.xors)]
        write_dot(self.circuit, groups, filename)


def extract_xor(circuit: Circuit) -> XorCircuit:
    cuts = enumerate_cuts(circuit, 2)
    xors: list[Xor] = []
    for root in circuit.gate_nodes:
        for cut in cuts.get(root, ()):
            if len(cut) != 2:
                continue
            inputs, nodes = dfs_cone(circuit, root, cut)
            if len(inputs) == 2 and match_xor(cone_tt(circuit, inputs, nodes, root)):
                xors.append(Xor(root, inputs, nodes))
                break
    return XorCircuit(circuit, xors)
