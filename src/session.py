"""Stateful, framework-agnostic service layer.

:class:`CircuitSession` holds the "current circuit" and parses file paths into
:class:`Circuit` objects (dispatching on extension).  It takes plain strings and
returns plain dicts, so any frontend (MCP, CLI, HTTP) can sit on top.
"""

from __future__ import annotations

import os

from .library import Library
from .circuit import Circuit, extract_adders, extract_xor
from .circuit.aig_parser import DEFAULT_LIB_PATH

# File extensions we know how to parse.
_VERILOG_EXTS = {".v", ".sv", ".verilog"}
_AIGER_EXTS = {".aig", ".aag"}


class NoCircuitLoadedError(RuntimeError):
    """Raised when an operation needs a circuit but none has been loaded."""


class CircuitSession:
    """Holds the currently loaded circuit and the operations over it."""

    def __init__(self) -> None:
        self.circuit: Circuit | None = None
        self.source: str | None = None
        self._lib_cache: Library | None = None

    # -- internals ------------------------------------------------------

    def _lib(self) -> Library:
        """The default Liberty library (``example.lib``), loaded once."""
        if self._lib_cache is None:
            self._lib_cache = Library.from_file(str(DEFAULT_LIB_PATH))
        return self._lib_cache

    # -- operations -----------------------------------------------------

    def load(self, path: str) -> str:
        """Parse *path* into the current circuit and return its summary.

        Dispatches on the file extension: ``.v``/``.sv``/``.verilog`` are read
        as gate-level Verilog (against the default library), ``.aig``/``.aag``
        as AIGER.  Raises ``FileNotFoundError`` / ``ValueError`` for a missing
        path or an unsupported extension.
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such file: {path}")

        ext = os.path.splitext(path)[1].lower()
        if ext in _VERILOG_EXTS:
            circuit = Circuit.from_file(path, self._lib())
            fmt = "verilog"
        elif ext in _AIGER_EXTS:
            circuit = Circuit.from_aig_file(path)
            fmt = "aiger"
        else:
            supported = ", ".join(sorted(_VERILOG_EXTS | _AIGER_EXTS))
            raise ValueError(
                f"unsupported file type {ext!r} for {path!r}; "
                f"expected one of: {supported}"
            )

        self.circuit = circuit
        self.source = path
        return f"Loaded {fmt} file {path}\n{circuit.summary()}"

    def _current(self) -> Circuit:
        if self.circuit is None:
            raise NoCircuitLoadedError(
                "no circuit loaded; call read_file(path) first"
            )
        return self.circuit

    def summary(self) -> str:
        """One-line summary of the current circuit, or raise if none is loaded."""
        return self._current().summary()

    def xor_stats(self) -> str:
        """Extract XOR gates from the current circuit and report chain structure."""
        return extract_xor(self._current()).report()

    def adder_stats(self) -> str:
        """Extract half/full adders and report the largest adder tree."""
        return extract_adders(self._current()).report()
