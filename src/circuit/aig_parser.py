"""AIGER (And-Inverter Graph) parser — binary ``aig`` and ASCII ``aag``.

An AIG uses only two-input AND gates plus inverters (folded into each literal's
sign bit), so the graph is built from just the ``AND`` and ``NOT`` cells of a
library (default: ``examples/example.lib``).  Format reference: ``aiger/FORMAT``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .node import PI, PO, CONST0, CONST1, _CONST_NET

if TYPE_CHECKING:
    from .circuit import Circuit

# example.lib lives at the repository root (../../examples/example.lib).
DEFAULT_LIB_PATH = Path(__file__).resolve().parents[2] / "examples" / "example.lib"


# ---------------------------------------------------------------------------
# Parsed representation
# ---------------------------------------------------------------------------

class _Aig:
    """The raw contents of an AIGER file, before circuit construction."""

    def __init__(self) -> None:
        self.M = self.I = self.L = self.O = self.A = 0
        self.outputs: list[int] = []            # output literals, in order
        self.and_rhs: dict[int, tuple[int, int]] = {}  # var -> (rhs0, rhs1) lits
        self.in_syms: dict[int, str] = {}        # input position -> symbol name
        self.out_syms: dict[int, str] = {}       # output position -> symbol name


# ---------------------------------------------------------------------------
# Binary delta decoding
# ---------------------------------------------------------------------------

def _decode_uint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode one little-endian 7-bit-per-byte unsigned int (see FORMAT).

    Returns ``(value, next_pos)``.
    """
    x = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("unexpected EOF while decoding AND gate")
        ch = data[pos]
        pos += 1
        x |= (ch & 0x7F) << shift
        if not (ch & 0x80):
            return x, pos
        shift += 7


# ---------------------------------------------------------------------------
# Symbol table / comments
# ---------------------------------------------------------------------------

def _parse_symbols(text: str, aig: _Aig) -> None:
    """Read an optional symbol table, stopping at the comment section ('c')."""
    for line in text.splitlines():
        if not line:
            continue
        if line[0] == "c":          # comment section — nothing useful past here
            break
        if line[0] in "ilo":
            head, _, name = line.partition(" ")
            try:
                idx = int(head[1:])
            except ValueError:
                continue
            if line[0] == "i":
                aig.in_syms[idx] = name
            elif line[0] == "o":
                aig.out_syms[idx] = name
            # 'l' (latch) symbols are ignored — latches are unsupported.


# ---------------------------------------------------------------------------
# Header + format-specific bodies
# ---------------------------------------------------------------------------

def _read_header(data: bytes) -> tuple[bytes, _Aig, int]:
    """Parse the ``aig``/``aag`` header line.

    Returns ``(format_id, aig, body_offset)`` where *body_offset* is the byte
    index just past the header's newline.
    """
    nl = data.find(b"\n")
    if nl == -1:
        raise ValueError("not an AIGER file: no header line found")
    fields = data[:nl].split()
    if len(fields) != 6 or fields[0] not in (b"aig", b"aag"):
        raise ValueError(f"invalid AIGER header: {data[:nl]!r}")

    aig = _Aig()
    aig.M, aig.I, aig.L, aig.O, aig.A = (int(f) for f in fields[1:])
    if aig.L != 0:
        raise ValueError(
            "sequential AIGER files (latches) are not supported; "
            "only combinational AND/NOT graphs can be parsed"
        )
    return fields[0], aig, nl + 1


def _read_binary_body(data: bytes, pos: int, aig: _Aig) -> None:
    """Parse the body of a binary ``aig`` file from byte offset *pos*."""
    # Output literals: O lines of ASCII decimal.
    for _ in range(aig.O):
        end = data.find(b"\n", pos)
        if end == -1:
            raise ValueError("unexpected EOF in output section")
        aig.outputs.append(int(data[pos:end]))
        pos = end + 1

    # AND gates: A pairs of delta-encoded RHS literals; LHS is implicit.
    first_and_var = aig.I + aig.L + 1
    for k in range(aig.A):
        delta0, pos = _decode_uint(data, pos)
        delta1, pos = _decode_uint(data, pos)
        lhs = 2 * (first_and_var + k)
        rhs0 = lhs - delta0
        rhs1 = rhs0 - delta1
        aig.and_rhs[first_and_var + k] = (rhs0, rhs1)

    # Whatever remains is the optional symbol table + comments (ASCII).
    _parse_symbols(data[pos:].decode("latin-1"), aig)


def _read_ascii_body(data: bytes, pos: int, aig: _Aig) -> None:
    """Parse the body of an ASCII ``aag`` file from byte offset *pos*."""
    lines = data[pos:].decode("latin-1").split("\n")
    i = 0

    def next_line() -> str:
        nonlocal i
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        if i >= len(lines):
            raise ValueError("unexpected EOF in AIGER body")
        line = lines[i]
        i += 1
        return line

    # Inputs and latches are listed explicitly; their literals are positional
    # (2,4,... for inputs) so we can simply skip them.
    for _ in range(aig.I):
        next_line()
    for _ in range(aig.L):
        next_line()
    for _ in range(aig.O):
        aig.outputs.append(int(next_line().split()[0]))
    for _ in range(aig.A):
        lhs, rhs0, rhs1 = (int(x) for x in next_line().split()[:3])
        aig.and_rhs[lhs // 2] = (rhs0, rhs1)

    _parse_symbols("\n".join(lines[i:]), aig)


# ---------------------------------------------------------------------------
# Circuit construction
# ---------------------------------------------------------------------------

def _build(circuit: Circuit, aig: _Aig) -> None:
    """Materialise *aig* into *circuit* using its AND and NOT cells."""
    for needed in ("AND", "NOT"):
        if needed not in circuit.lib:
            sys.exit(f"Error: library '{circuit.lib.name}' lacks the '{needed}' "
                     f"cell required to build an AIG")
    and_cell = circuit.lib["AND"]
    not_cell = circuit.lib["NOT"]
    and_logic = and_cell.make_logic()
    not_logic = not_cell.make_logic()

    var_node: dict[int, int] = {}     # AIG variable index -> driver node id
    const_node: dict[int, int] = {}   # 0/1 -> node id
    neg_node: dict[int, int] = {}     # variable -> NOT node id (its inverter)

    def const(bit: int) -> int:
        if bit not in const_node:
            kind = CONST1 if bit else CONST0
            const_node[bit] = circuit._add_node(kind, _CONST_NET[str(bit)])
        return const_node[bit]

    def build_var(var: int) -> int:
        """Return the node id driving (the positive form of) *var*."""
        if var in var_node:
            return var_node[var]
        rhs0, rhs1 = aig.and_rhs[var]    # an AND output we haven't built yet
        inputs = [lit_node(rhs0), lit_node(rhs1)]
        nid = circuit._add_node("AND", f"n{var}", cell=and_cell,
                                logic=and_logic, inputs=inputs)
        var_node[var] = nid
        return nid

    def lit_node(lit: int) -> int:
        """Return the node id producing the value of literal *lit*."""
        if lit == 0:
            return const(0)
        if lit == 1:
            return const(1)
        var = lit >> 1
        base = build_var(var)
        if lit & 1:                      # negated -> route through an inverter
            if var not in neg_node:
                neg_node[var] = circuit._add_node(
                    "NOT", f"n{var}_n", cell=not_cell, logic=not_logic,
                    inputs=[base])
            return neg_node[var]
        return base

    # 1. Inputs -> PI source nodes (variables 1..I).
    for pos in range(aig.I):
        net = aig.in_syms.get(pos) or f"i{pos}"
        var_node[pos + 1] = circuit._add_node(PI, net)
        circuit.input_nets.append(net)

    # 2. Every AND gate.  Building in ascending variable order keeps recursion
    #    shallow: a gate's fan-in variables are smaller (guaranteed in binary,
    #    usual in ASCII) and thus already cached by the time we reach it.
    for var in sorted(aig.and_rhs):
        build_var(var)

    # 3. Outputs -> PO sink nodes fed by their (possibly inverted) literal.
    for pos, lit in enumerate(aig.outputs):
        net = aig.out_syms.get(pos) or f"o{pos}"
        circuit.output_nets.append(net)
        circuit._add_node(PO, net, inputs=[lit_node(lit)])

    # 4. Fan-out edges.
    for node in circuit.nodes.values():
        for src in node.inputs:
            circuit.nodes[src].fanouts.append(node.node_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_aig(circuit: Circuit, data: bytes) -> None:
    """Parse AIGER *data* (binary or ASCII) into *circuit*."""
    fmt, aig, pos = _read_header(data)
    if fmt == b"aig":
        _read_binary_body(data, pos, aig)
    else:
        _read_ascii_body(data, pos, aig)
    circuit.name = "aig"
    _build(circuit, aig)
