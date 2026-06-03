"""MCP server (demo): a thin FastMCP frontend over :class:`CircuitSession`.

Run with stdio transport: ``uv run python mcp_server.py``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.session import CircuitSession

mcp = FastMCP("gate-lifter")
session = CircuitSession()


@mcp.tool()
def read_file(path: str) -> dict:
    """Load a circuit file and make it the current circuit.

    Accepts gate-level Verilog (``.v``/``.sv``/``.verilog``, parsed against the
    default example library) or AIGER (``.aig``/``.aag``).  Returns the parsed
    circuit's statistics.
    """
    return session.load(path)


@mcp.tool()
def print_stats() -> dict:
    """Return statistics for the currently loaded circuit.

    Takes no arguments; reports on whatever was last loaded via ``read_file``.
    Errors if no circuit has been loaded yet.
    """
    return session.stats()


if __name__ == "__main__":
    mcp.run()
