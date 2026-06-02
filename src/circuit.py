"""Gate-level netlist as a DAG of single-output nodes.

:class:`Circuit` parses the structural Verilog ``yosys`` emits after
``flatten`` + tech mapping (see ``examples/test.v``) — gate instances and
``assign`` statements over bit-selects, concatenations and sized constants.

Everything is modelled at **bit granularity**: a *net* is one wire bit and each
node drives exactly one bit (PI, PO, constant, or a library gate). A gate
missing from the given :class:`~src.library.Library` causes an error exit.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from .library import Cell, Library, LogicFn

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
        circuit._parse(_strip_meta(text))
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

    def _parse(self, text: str) -> None:
        header = re.search(r"\bmodule\s+(\S+)\s*\(.*?\)\s*;", text, re.DOTALL)
        if not header:
            raise ValueError("no module declaration found")
        self.name = _clean_id(header.group(1))

        body_end = text.rfind("endmodule")
        body = text[header.end():body_end if body_end != -1 else len(text)]

        widths: dict[str, tuple[int, int]] = {}      # bus base name -> (msb, lsb)
        gates: list[tuple[str, str, dict[str, str]]] = []   # (cell, inst, conns)
        assigns: list[tuple[str, str]] = []          # (lhs, rhs)
        in_ports: list[str] = []                     # raw port signal text
        out_ports: list[str] = []

        for stmt in _split_statements(body):
            stmt = stmt.strip()
            if not stmt:
                continue

            decl = re.match(r"(input|output|wire)\b\s*(\[\s*\d+\s*:\s*\d+\s*\])?\s*(.+)",
                            stmt, re.DOTALL)
            if decl:
                kind, rng, names = decl.group(1), decl.group(2), decl.group(3)
                bounds = _parse_range(rng) if rng else None
                for name in _split_top(names.rstrip(";")):
                    base = _clean_id(name)
                    if bounds:
                        widths[base] = bounds
                    signal = name.strip()
                    if kind == "input":
                        in_ports.append(signal)
                    elif kind == "output":
                        out_ports.append(signal)
                continue

            assign = re.match(r"assign\s+(.+?)\s*=\s*(.+)", stmt, re.DOTALL)
            if assign:
                assigns.append((assign.group(1).strip(),
                                assign.group(2).strip().rstrip(";").strip()))
                continue

            gate = re.match(r"(\w+)\s+(\\?\S+)\s*\((.*)\)\s*;?\s*$", stmt, re.DOTALL)
            if gate:
                cell_name, inst = gate.group(1), _clean_id(gate.group(2))
                if cell_name not in self.lib:
                    sys.exit(f"Error: gate '{cell_name}' (instance '{inst}') "
                             f"is not defined in library '{self.lib.name}'")
                conns = {pin: net for pin, net in
                         re.findall(r"\.(\w+)\s*\(\s*(.*?)\s*\)", gate.group(3), re.DOTALL)}
                gates.append((cell_name, inst, conns))
                continue
            # Unknown statement (e.g. stray `endmodule`) — ignore.

        # 1. Primary input bits become source nodes.
        for signal in in_ports:
            for net in _expand(signal, widths):
                self.input_nets.append(net)
                self._driver.setdefault(net, self._add_node(PI, net))

        # 2. Continuous assigns define net aliases, bit by bit.
        for lhs, rhs in assigns:
            lbits, rbits = _expand(lhs, widths), _expand(rhs, widths)
            if len(lbits) != len(rbits):
                print(f"Warning: width mismatch in assign {lhs!r} = {rhs!r} "
                      f"({len(lbits)} vs {len(rbits)} bits)", file=sys.stderr)
            for lbit, rbit in zip(lbits, rbits):
                self._alias[lbit] = rbit

        # 3. Gate instances: create the node driving each output bit.
        gate_inputs: list[tuple[int, Cell, dict[str, str]]] = []
        for cell_name, inst, conns in gates:
            cell = self.lib[cell_name]
            out_net = _single_net(conns.get(cell.output, ""), widths)
            nid = self._add_node(cell_name, out_net, cell=cell, logic=cell.make_logic())
            if out_net:
                self._driver[out_net] = nid
            gate_inputs.append((nid, cell, conns))

        # 4. Resolve every gate's fan-in to driver node ids.
        for nid, cell, conns in gate_inputs:
            self.nodes[nid].inputs = [
                self._resolve(_single_net(conns.get(pin, ""), widths))
                for pin in cell.inputs
            ]

        # 5. Primary output bits become sink nodes fed by their driver.
        for signal in out_ports:
            for net in _expand(signal, widths):
                self.output_nets.append(net)
                src = self._resolve(net)
                self._add_node(PO, net, inputs=[src])

        # 6. Wire up fan-out edges.
        for node in self.nodes.values():
            for src in node.inputs:
                self.nodes[src].fanouts.append(node.node_id)

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


# ----------------------------------------------------------------------
# Verilog text helpers
# ----------------------------------------------------------------------

# A sized literal such as 4'hF, 2'b0x, 11'h000.
_LITERAL = re.compile(r"^(\d+)'([bBoOdDhH])([0-9a-fA-FxXzZ_]+)$")

# One signal term: an (escaped) identifier with an optional bit/part select.
_TERM = re.compile(
    r"""\s*
        (?:(?P<esc>\\\S+)|(?P<name>[A-Za-z_][\w$.]*))
        \s*(?:\[\s*(?P<sel>\d+(?:\s*:\s*\d+)?)\s*\])?
        \s*$""",
    re.VERBOSE,
)


def _strip_meta(text: str) -> str:
    """Remove comments and ``(* ... *)`` attributes from Verilog source."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"\(\*.*?\*\)", "", text, flags=re.DOTALL)
    return text


def _clean_id(name: str) -> str:
    r"""Normalise an identifier: drop the leading escape ``\`` and surrounding space."""
    name = name.strip()
    return name[1:].strip() if name.startswith("\\") else name


def _parse_range(text: str) -> tuple[int, int]:
    """Parse a ``[msb:lsb]`` declaration into an ``(msb, lsb)`` tuple."""
    msb, lsb = re.findall(r"\d+", text)
    return int(msb), int(lsb)


def _split_statements(text: str) -> list[str]:
    """Split a module body into statements on top-level ``;``.

    Depth is tracked across ``()`` and ``{}`` so multi-line gate instances and
    concatenations stay intact.
    """
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif ch == ";" and depth == 0:
            out.append(text[start:i + 1])
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _split_top(text: str) -> list[str]:
    """Split a comma list at top level (ignoring commas inside ``{}``/``[]``)."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if text[start:].strip():
        parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _parse_literal(tok: str) -> list[str] | None:
    """Expand a sized Verilog literal to per-bit constant nets, MSB first.

    Returns ``None`` if *tok* is not a literal.
    """
    m = _LITERAL.match(tok.strip())
    if not m:
        return None
    width, base, digits = int(m.group(1)), m.group(2).lower(), m.group(3).replace("_", "")

    if base == "b":
        bits = digits
    elif base == "d":
        bits = format(int(digits), "b")
    else:
        per = 3 if base == "o" else 4
        chunks = []
        for d in digits:
            chunks.append("x" * per if d in "xXzZ"
                          else format(int(d, 8 if base == "o" else 16), f"0{per}b"))
        bits = "".join(chunks)

    bits = bits.replace("z", "x").replace("Z", "x")
    # Normalise to exactly `width` bits, MSB first.
    bits = bits[-width:].rjust(width, "0") if len(bits) >= width else bits.rjust(width, "0")
    return [_CONST_NET.get(b, _CONST_NET["x"]) for b in bits]


def _expand(expr: str, widths: dict[str, tuple[int, int]]) -> list[str]:
    """Expand a signal expression into a flat list of bit nets (MSB first)."""
    expr = expr.strip()
    if expr.startswith("{"):
        inner = expr[1:expr.rfind("}")]
        bits: list[str] = []
        for part in _split_top(inner):
            bits.extend(_expand(part, widths))
        return bits

    literal = _parse_literal(expr)
    if literal is not None:
        return literal

    m = _TERM.match(expr)
    if not m:
        raise ValueError(f"cannot parse signal expression: {expr!r}")
    base = _clean_id(m.group("esc") or m.group("name"))
    sel = m.group("sel")

    if sel is None:
        bounds = widths.get(base)
        if bounds:  # bare bus reference -> all its bits, MSB first
            msb, lsb = bounds
            step = -1 if msb >= lsb else 1
            return [f"{base}[{i}]" for i in range(msb, lsb + step, step)]
        return [base]  # scalar

    if ":" in sel:
        msb, lsb = (int(x) for x in sel.split(":"))
        step = -1 if msb >= lsb else 1
        return [f"{base}[{i}]" for i in range(msb, lsb + step, step)]
    return [f"{base}[{int(sel)}]"]


def _single_net(expr: str, widths: dict[str, tuple[int, int]]) -> str:
    """Expand a connection expected to be one bit, returning that net name."""
    if not expr.strip():
        return ""
    bits = _expand(expr, widths)
    if len(bits) != 1:
        raise ValueError(f"expected a 1-bit connection, got {len(bits)}: {expr!r}")
    return bits[0]
