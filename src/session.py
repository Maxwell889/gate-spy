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
from .circuit.subgraph import extract_subgraph
from .circuit.verilog_writer import write_verilog

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
        return self._load_report(fmt)

    def _load_report(self, fmt: str) -> str:
        c = self.circuit
        assert c is not None

        def compact(nets: list[str]) -> str:
            import re
            bus: dict[str, list[int]] = {}
            scalars: list[str] = []
            for n in nets:
                m = re.match(r"(.+)\[(\d+)\]$", n)
                if m:
                    bus.setdefault(m.group(1), []).append(int(m.group(2)))
                else:
                    scalars.append(n)
            parts = []
            for b, idx in bus.items():
                idx.sort()
                parts.append(f"{b}[{idx[0]}..{idx[-1]}] ({len(idx)})")
            parts.extend(scalars)
            return ", ".join(parts) if parts else "(none)"

        pi = compact(c.input_nets)
        po = compact(c.output_nets)
        verilog = fmt == "verilog"
        naming = (
            "    Internal signal names are Yosys-generated wires (e.g. _0001_) "
            "from the netlist source."
            if verilog else
            "    Internal signal names are synthetic (e.g. n33 for AND gates, "
            "n33_n for their inverters)."
        )
        return "\n".join([
            f"Loaded {fmt} file {self.source}",
            f"    {c.summary()}",
            f"    Input ports ({len(c.input_nets)})  : {pi}",
            f"    Output ports ({len(c.output_nets)}) : {po}",
            naming,
        ])

    def _current(self) -> Circuit:
        if self.circuit is None:
            raise NoCircuitLoadedError(
                "no circuit loaded; call read_file(path) first"
            )
        return self.circuit

    def summary(self) -> str:
        """One-line summary of the current circuit, or raise if none is loaded."""
        return self._current().summary()

    def xor_stats(self, detail: bool = False) -> str:
        """Extract XOR gates from the current circuit and report chain structure."""
        return extract_xor(self._current()).report(detail=detail)

    def adder_stats(self, detail: bool = False) -> str:
        """Extract half/full adders and report the largest adder tree."""
        return extract_adders(self._current()).report(detail=detail)

    def node_info(self, ref: str, depth: int = 2, detail: bool = False) -> str:
        return self._current().describe_node(ref, depth, detail=detail)

    def extract_subcircuit(self, inputs: list[str], outputs: list[str],
                           out_path: str = "subgraph.v") -> str:
        """Extract a closed subgraph, write as Verilog (``.v``) or AIG (``.aig``).

        The format is chosen by the file extension of *out_path*.  ``.aig``
        output runs ``scripts/v2aig.sh`` as a post-processing step.
        """
        import os
        import subprocess
        import tempfile
        from pathlib import Path

        sub = extract_subgraph(self._current(), inputs, outputs)
        ext = os.path.splitext(out_path)[1].lower()

        if ext == ".aig":
            with tempfile.NamedTemporaryFile(suffix=".v", delete=False) as tf:
                tf_v = tf.name
            write_verilog(sub, tf_v)
            script = Path(__file__).resolve().parents[1] / "scripts" / "v2aig.sh"
            subprocess.run(
                ["bash", str(script), tf_v, out_path,
                 "-l", str(DEFAULT_LIB_PATH)],
                check=True, capture_output=True, text=True,
            )
            os.unlink(tf_v)
            aig_c = Circuit.from_aig_file(out_path)
            return (
                f"Subgraph extracted from {self.source}\n"
                f"    written to  : {out_path}\n"
                f"    {aig_c.summary()}"
            )
        else:
            write_verilog(sub, out_path)
            return (
                f"Subgraph extracted from {self.source}\n"
                f"    written to  : {out_path}\n"
                f"    {sub.summary()}"
            )
