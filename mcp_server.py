"""MCP server (demo): a thin FastMCP frontend over :class:`CircuitSession`.

Run with stdio transport: ``uv run python mcp_server.py``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.session import CircuitSession

mcp = FastMCP("gate-lifter")
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
def get_node(node: str, depth: int = 2) -> str:
    """Inspect a signal/node of the current circuit.

    ``node`` can be:
    - a port signal name (e.g. ``"Out[5]"``, ``"IN1[0]"``, ``"a"``)
    - an internal signal name (e.g. ``"_0001_"`` for Verilog, ``"n33"`` for AIGER)
    - a numeric node id

    Returns the node's kind, basic info, and its fan-in / fan-out cones up to
    ``depth`` levels (default 2).  Internal names are discovered by walking the
    fan-in / fan-out of ports or other nodes — start from a port and follow the
    names reported in each neighbour listing.
    """
    return session.node_info(node, depth)


@mcp.tool()
def print_xor_stats() -> str:
    """Extract XOR gates from the current circuit and report their chains.

    Reports the XOR count, the longest XOR chain (the signals it threads
    through), and how XORs distribute by chain depth.
    """
    return session.xor_stats()


@mcp.tool()
def print_adder_stats() -> str:
    """Extract half/full adders from the current circuit and report adder trees.

    Reports the HA/FA counts and the largest connected adder tree (its size,
    composition, carry-depth, and the outputs it drives).
    """
    return session.adder_stats()


if __name__ == "__main__":
    mcp.run()
