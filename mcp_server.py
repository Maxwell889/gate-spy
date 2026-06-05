"""MCP server (demo): a thin FastMCP frontend over :class:`CircuitSession`.

Run with stdio transport: ``uv run python mcp_server.py``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.session import CircuitSession

mcp = FastMCP("gate-spy")
session = CircuitSession()


@mcp.tool()
def read_file(path: str) -> str:
    """Load a circuit file and make it the current circuit.

    Accepts gate-level Verilog (``.v``/``.sv``/``.verilog``, parsed against the
    default example library) or AIGER (``.aig``/``.aag``).  Returns the circuit
    summary and the input/output port signal names — use those as starting points
    with ``get_node`` to explore the circuit neighbourhood.

    Naming conventions:
    - Ports are listed as ``base[0..N-1] (N)`` for buses or plain names for scalars.
    - For Verilog: internal wires keep their Yosys-generated names (e.g. _0001_).
    - For AIGER: internal nodes are named ``n{v}`` for AND gates and ``n{v}_n``
      for their dedicated inverters (literal negation produces one NOT per gate).
    """
    return session.load(path)


@mcp.tool()
def get_node(node: str, depth: int = 2, detail: bool = False) -> str:
    """Inspect a signal/node of the current circuit.

    ``node`` can be:
    - a port signal name (e.g. ``"Out[5]"``, ``"IN1[0]"``, ``"a"``)
    - an internal signal name (e.g. ``"_0001_"`` for Verilog, ``"n33"`` for AIGER)
    - a numeric node id

    Returns the node's kind, basic info, and its fan-in / fan-out cones up to
    ``depth`` levels (default 2).  When ``detail`` is True, all neighbours are
    listed; otherwise at most 16 per level (excess shown as "(+N more)").
    """
    return session.node_info(node, depth, detail=detail)


@mcp.tool()
def extract_subgraph(inputs: list[str], outputs: list[str],
                     out_path: str = "subgraph.v") -> str:
    """Extract a closed subgraph between the given input/output signals.

    ``inputs``/``outputs`` are lists of signal names (use port names from
    ``read_file`` output or internal names discovered via ``get_node``).  The
    subgraph is validated to be self-contained and written to ``out_path``.
    The format is chosen by the file extension:

    - ``.v`` (default) — structural Verilog with library cells
    - ``.aig`` — binary AIGER, via ``scripts/v2aig.sh``

    Returns a summary of the written circuit.
    """
    return session.extract_subcircuit(inputs, outputs, out_path)


@mcp.tool()
def find_cone(signals: list[str],
              direction: str,
              stop_at: list[str] | None = None,
              depth: int = -1,
              detail: bool = False) -> str:
    """Trace the fan-in (``backward``) or fan-out (``forward``) cone of a set of
    starting signals, returning the cone layer-by-layer.

    Use this to discover the boundary of a sub-circuit:

    - ``direction="backward"`` traces **upstream** (fan-in / inputs) to find
      which primary inputs (or ``stop_at`` signals) a set of signals depends on.
      Example: given 36 adder-tree result signals, ``backward`` tells you those
      results ultimately depend on ``io_a[0:10], io_b[0:10]`` — the mantissa bits.
    - ``direction="forward"`` traces **downstream** (fan-out) to find where a
      set of signals converges, e.g. which product-bit signals emerge before
      normalisation logic.

    ``stop_at`` is a list of signal names where traversal halts — the signals
    are included in the report but not expanded further.  This lets you "clip"
    the cone at a known boundary (e.g. the normalisation shifter inputs).

    ``depth`` caps the number of levels (0 = start signals only, -1 = unlimited).
    ``detail=True`` lists every node at every layer; otherwise each layer is
    capped at 16 entries with an overflow count.

    Returns a structured, layer-by-layer report: a header with parameters, one
    section per distance level, a boundary-signal breakdown grouped by *why*
    traversal stopped, and a compact summary with bus ranges.
    """
    return session.cone_report(signals, direction, stop_at=stop_at,
                               depth=depth, detail=detail)


@mcp.tool()
def simulate(pattern_num: int = 100,
             fixed_inputs: dict[str, int] | None = None,
             watch: list[str] | None = None,
             seed: int | None = None) -> str:
    """Simulate the current circuit and report bus values across input patterns.

    With no arguments, applies ``pattern_num`` (default 100) random input
    patterns and reports, for each pattern, every output bus as a decimal
    integer (and each input bus, so the rows are interpretable).

    Pin specific inputs with ``fixed_inputs`` — a mapping whose keys are:
    - a bus base (``"IN1"``) → the integer is spread over its bits (MSB..LSB),
    - a single bit (``"IN1[0]"``), or
    - a scalar port name.
    Pinned bits stay constant across patterns; the rest are randomised. If every
    input bit is pinned, exactly one (deterministic) pattern is run.

    Use ``watch`` to surface internal signals: pass a single net name
    (``"_0007_"``, ``"n33"``) to see its raw bit, or an internal bus base
    (``"sum"``) to gather its bits into an integer column.

    ``seed`` makes the random patterns reproducible.

    Inputs are validated, not silently coerced. An unknown input name, a single
    bit set to anything but 0/1, or a bus integer that overflows its width all
    raise an error naming the input and its allowed range. When a bus is only
    partially pinned, the report header flags it with a NOTE (its remaining bits
    are randomised), so a partial column is never mistaken for a chosen value.
    """
    return session.simulate(pattern_num, fixed_inputs, watch, seed)


@mcp.tool()
def print_xor_stats(detail: bool = False) -> str:
    """Extract XOR gates from the current circuit and report their chains.

    Reports the XOR count, the longest XOR chain (the signals it threads
    through).  The ``detail`` flag is accepted for API consistency.
    """
    return session.xor_stats(detail=detail)


@mcp.tool()
def print_adder_stats(detail: bool = False) -> str:
    """Extract half/full adders from the current circuit and report adder trees.

    Reports the HA/FA counts and the largest connected adder tree (its size,
    composition, carry-depth, and the outputs it drives).  With ``detail=True``,
    all boundary signals are listed without truncation.
    """
    return session.adder_stats(detail=detail)


if __name__ == "__main__":
    mcp.run()
