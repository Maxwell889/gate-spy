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
    default example library) or AIGER (``.aig``/``.aag``).  Returns a summary of
    the parsed circuit.
    """
    return session.load(path)


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
