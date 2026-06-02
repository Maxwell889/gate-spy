"""Node dataclass and bit-level constants for the circuit DAG."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..library import Cell, LogicFn

# Node kinds that are not library gates.
PI = "PI"        # primary input bit
PO = "PO"        # primary output bit
CONST0 = "CONST0"
CONST1 = "CONST1"
CONSTX = "CONSTX"  # don't-care; evaluates to 0 during simulation
_SOURCE_KINDS = frozenset({PI, CONST0, CONST1, CONSTX})

# Canonical net keys for constant bits produced by literal expansion.
_CONST_NET = {"0": "$const0", "1": "$const1", "x": "$constx"}
_CONST_KIND = {"$const0": CONST0, "$const1": CONST1, "$constx": CONSTX}


@dataclass
class Node:
    """One bit-wide node in the circuit DAG."""

    node_id: int
    kind: str                       # PI / PO / CONST* / a cell name
    net: str = ""                   # the net (bit) this node drives / names
    inputs: list[int] = field(default_factory=list)   # upstream node ids
    fanouts: list[int] = field(default_factory=list)  # downstream node ids
    cell: Cell | None = None
    logic: LogicFn | None = None

    @property
    def is_source(self) -> bool:
        return self.kind in _SOURCE_KINDS

    @property
    def is_pi(self) -> bool:
        return self.kind == PI

    @property
    def is_po(self) -> bool:
        return self.kind == PO

    @property
    def is_gate(self) -> bool:
        return self.cell is not None
