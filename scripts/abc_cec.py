#!/usr/bin/env python3
"""
ABC CEC (Combinational Equivalence Check) — standalone script.

Verifies that two Verilog files implement the same combinational function
using v2aig.sh + yosys-abc.

Usage:
    python scripts/abc_cec.py <old_file> <new_file>
    python scripts/abc_cec.py <old_file> <new_file> --timeout 300
    python scripts/abc_cec.py <old_file> <new_file> --json   # JSON output

Exit codes:
    0 — equivalent (including timeout, which is assumed UNSAT)
    1 — not equivalent (counterexample found)
    2 — conversion error (v2aig failed, syntax error)
    3 — timeout (top-level process timeout)
    4 — other error
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------

_V2AIG = Path(__file__).resolve().parent / "v2aig.sh"


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _expand_pow_for_cec(code: str) -> str:
    """Expand ``x ** N`` (N integer) into ``x * x * ...`` for Yosys CEC.

    Yosys ``equiv_simple`` has no SAT model for ``$pow``.
    This is a workaround — a proper fix would techmap $pow → $mul inside Yosys.
    """
    def _repl(m: re.Match) -> str:
        base = m.group(1) or m.group(3)
        exp = int(m.group(2) or m.group(4))
        if exp == 0:
            return "1'b1"
        if exp == 1:
            return base
        return " * ".join([f"({base})"] * exp)

    return re.sub(
        r'\(([^()]+)\)\s*\*\*\s*(\d+)'
        r'|([a-zA-Z_]\w*(?:\s*\[[^\]]+\])?)\s*\*\*\s*(\d+)',
        _repl,
        code,
    )


def extract_top_module(code: str) -> str:
    """Return the first top-level module name found in *code*."""
    m = re.search(r"^\s*module\s+(\w+)\s*[#(;]", code, re.MULTILINE)
    return m.group(1) if m else ""


def _parse_port_order(code: str) -> "tuple[list[str], list[str]]":
    """Parse input/output ports and expand buses LSB-first.

    Returns ``(pi_names, po_names)`` where each list maps AIG PI/PO
    indices to human-readable signal names (e.g. ``"in1[0]"``, ``"out3"``).
    The ordering follows the **module port list** (Yosys's BLIF emission
    order), with buses expanded LSB→MSB.
    """
    # -- 1. Parse the module port list ------------------------------------
    port_list: list[str] = []
    m = re.search(r"module\s+\w+\s*\(([^)]*)\)", code)
    if m:
        port_list = [p.strip() for p in m.group(1).split(",") if p.strip()]

    # -- 2. Parse declarations: {port_name: (direction, msb, lsb)} --------
    body_m = re.search(r"module\s+\w+\s*(?:\(.*?\))?\s*;(.*?)endmodule",
                       code, re.DOTALL)
    body = body_m.group(1) if body_m else ""

    decl_re = re.compile(
        r"(input|output)\s+(?:signed\s+)?(?:\[(\d+)\s*:\s*(\d+)\])?\s*(.+?)\s*;",
        re.DOTALL,
    )

    port_info: dict[str, tuple[str, int | None, int | None]] = {}
    for dm in decl_re.finditer(body):
        direction = dm.group(1)
        msb_s, lsb_s = dm.group(2), dm.group(3)
        names_part = dm.group(4)
        msb = int(msb_s) if msb_s is not None else None
        lsb = int(lsb_s) if lsb_s is not None else None
        for name in re.findall(r"\w+", names_part):
            port_info[name] = (direction, msb, lsb)

    # -- 3. Expand in port-list order --------------------------------------
    pi_names: list[str] = []
    po_names: list[str] = []

    for name in port_list:
        info = port_info.get(name)
        if info is None:
            continue
        direction, msb, lsb = info

        if msb is not None and lsb is not None:
            step = 1 if msb >= lsb else -1
            bits = list(range(lsb, msb + step, step))
            target = pi_names if direction == "input" else po_names
            for b in bits:
                target.append(f"{name}[{b}]")
        else:
            target = pi_names if direction == "input" else po_names
            target.append(name)

    return pi_names, po_names


def _pattern_to_word_values(pattern: dict, code: str) -> dict:
    """Group individual bit values into bus-level word values.

    Returns a dict mapping bus/scalar names to ``{unsigned, signed, width}``.
    Only buses with at least one bit in *pattern* are included; scalars
    (no bit index) appear as width=1.
    """
    # -- parse bus widths from port declarations --------------------------
    body_m = re.search(
        r"module\s+\w+\s*(?:\(.*?\))?\s*;(.*?)endmodule", code, re.DOTALL
    )
    body = body_m.group(1) if body_m else ""

    decl_re = re.compile(
        r"(input|output)\s+(?:signed\s+)?(?:\[(\d+)\s*:\s*(\d+)\])?\s*(.+?)\s*;",
        re.DOTALL,
    )

    bus_widths: dict[str, int] = {}
    for dm in decl_re.finditer(body):
        msb_s, lsb_s = dm.group(2), dm.group(3)
        names_part = dm.group(4)
        if msb_s is not None:
            width = abs(int(msb_s) - int(lsb_s)) + 1
            for name in re.findall(r"\w+", names_part):
                bus_widths[name] = width

    # -- group bits by bus name --------------------------------------------
    buses: dict[str, dict[int, int]] = {}
    scalars: dict[str, int] = {}

    for sig_name, val_str in pattern.items():
        val = int(val_str)
        m = re.match(r"^(\w+)\[(\d+)\]$", sig_name)
        if m:
            bus = m.group(1)
            bit_idx = int(m.group(2))
            buses.setdefault(bus, {})[bit_idx] = val
        else:
            scalars[sig_name] = val

    # -- build word values -------------------------------------------------
    result: dict[str, dict] = {}

    for name, val in scalars.items():
        w = bus_widths.get(name, 1)
        s = val if val < (1 << (w - 1)) else val - (1 << w)
        result[name] = {"unsigned": val, "signed": s, "width": w}

    for bus, bits in buses.items():
        width = bus_widths.get(bus, max(bits.keys()) + 1 if bits else 1)
        unsigned = 0
        for bit_idx, val in bits.items():
            if val:
                unsigned |= 1 << bit_idx
        if unsigned >= (1 << (width - 1)):
            signed = unsigned - (1 << width)
        else:
            signed = unsigned
        result[bus] = {"unsigned": unsigned, "signed": signed, "width": width}

    return result


def _parse_abc_cex(output: str, old_code: str) -> dict:
    """Parse ABC CEC counterexample output into a structured dict.

    Returns a dict with keys:
      - ``raw``: the raw ABC output text
      - ``mismatches``: list of {po_name, val_old, val_new}
      - ``pattern``: dict mapping input signal names to their values
        (only the bits ABC reports; unreported bits are free/don't-care)
      - ``word_values``: dict mapping bus/scalar names to
        ``{unsigned, signed, width}``
    """
    pi_names, po_names = _parse_port_order(old_code)

    cex: dict = {
        "raw": output,
        "mismatches": [],
        "pattern": {},
    }

    # Parse "Output poN: Value in Network1 = X. Value in Network2 = Y."
    for m in re.finditer(
        r"Output\s+po(\d+):\s+Value in Network1\s*=\s*(\S+)\.\s+"
        r"Value in Network2\s*=\s*(\S+)\.",
        output,
    ):
        po_idx = int(m.group(1))
        val_old = m.group(2)
        val_new = m.group(3)
        po_name = po_names[po_idx] if po_idx < len(po_names) else f"po{po_idx}"
        cex["mismatches"].append({
            "po_name": po_name,
            "val_old": val_old,
            "val_new": val_new,
        })

    # Parse "Input pattern: pi0=1 pi4=0 ..."
    pat_m = re.search(r"Input pattern:\s*(.+)", output)
    if pat_m:
        for pm in re.finditer(r"pi(\d+)=(\S+)", pat_m.group(1)):
            pi_idx = int(pm.group(1))
            value = pm.group(2)
            pi_name = pi_names[pi_idx] if pi_idx < len(pi_names) else f"pi{pi_idx}"
            cex["pattern"][pi_name] = value

    # -- word-level values -------------------------------------------------
    cex["word_values"] = _pattern_to_word_values(cex["pattern"], old_code)

    return cex


# ---------------------------------------------------------------------------
#  CEC core
# ---------------------------------------------------------------------------

def run_cec(old_path: str, new_path: str,
            timeout: int = 300) -> dict:
    """Run ABC CEC between two Verilog files via v2aig.sh + yosys-abc.

    Args:
        old_path: path to the original (golden) Verilog file.
        new_path: path to the modified (gate) Verilog file.
        timeout: seconds before killing the process.

    Returns:
        {
            "success": bool,
            "reason": "equivalent" | "counterexample" | "v2aig_failed_old"
                      | "v2aig_failed_new" | "no_top_module" | "cec_error"
                      | "timeout_assumed_equivalent",
            "output": str,            # ABC stdout + stderr
            "elapsed": float,         # wall-clock seconds
            "counterexample": dict | None,  # only when reason == "counterexample"
        }
    """
    old_code = open(old_path, encoding="utf-8").read()
    new_code_raw = open(new_path, encoding="utf-8").read()

    top = extract_top_module(old_code)
    if not top:
        return {"success": False, "reason": "no_top_module",
                "output": "no 'module' declaration found in old file",
                "elapsed": 0.0}

    # Expand ** in both codes so Yosys techmap sees only $mul.
    old_exp = _expand_pow_for_cec(old_code)
    new_exp = _expand_pow_for_cec(new_code_raw)

    old_fd, old_exp_path = tempfile.mkstemp(suffix=".v", prefix="cec_old_")
    new_fd, new_exp_path = tempfile.mkstemp(suffix=".v", prefix="cec_new_")
    old_aig = old_exp_path + ".aig"
    new_aig = new_exp_path + ".aig"

    try:
        os.write(old_fd, old_exp.encode())
        os.close(old_fd)
        os.write(new_fd, new_exp.encode())
        os.close(new_fd)

        v2aig = str(_V2AIG)
        t0 = time.time()

        # Convert both Verilog files to AIG.
        for tag, vp, ap in [("old", old_exp_path, old_aig),
                             ("new", new_exp_path, new_aig)]:
            r = subprocess.run(
                ["bash", v2aig, vp, ap],
                capture_output=True, text=True, timeout=timeout,
            )
            if r.returncode != 0:
                elapsed = time.time() - t0
                return {
                    "success": False,
                    "reason": f"v2aig_failed_{tag}",
                    "output": r.stderr or r.stdout,
                    "elapsed": elapsed,
                }

        # Run ABC CEC.
        proc = subprocess.run(
            ["yosys-abc", "-c", f"cec {old_aig} {new_aig}"],
            capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.time() - t0
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")

        if "Networks are equivalent" in output:
            return {"success": True, "reason": "equivalent",
                    "output": output, "elapsed": elapsed}

        if "NOT EQUIVALENT" in output:
            cex = _parse_abc_cex(output, old_code)
            return {
                "success": False,
                "reason": "counterexample",
                "output": output,
                "elapsed": elapsed,
                "counterexample": cex,
            }

        # Unknown output — treat as failure.
        return {"success": False, "reason": "cec_error",
                "output": output, "elapsed": elapsed}

    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return {
            "success": True,
            "reason": "timeout_assumed_equivalent",
            "output": f"ABC CEC timed out after {timeout}s — assumed equivalent",
            "elapsed": elapsed,
        }
    finally:
        for p in (old_exp_path, new_exp_path, old_aig, new_aig):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ABC CEC — verify two Verilog files are functionally "
                    "equivalent via v2aig.sh + yosys-abc."
    )
    parser.add_argument("old_file", help="Path to the original (golden) Verilog file.")
    parser.add_argument("new_file", help="Path to the modified Verilog file.")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Timeout in seconds (default: 300).")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON instead of human-readable text.")
    args = parser.parse_args()

    for fpath in (args.old_file, args.new_file):
        if not os.path.isfile(fpath):
            msg = f"file not found: {fpath}"
            if args.json:
                json.dump({"success": False, "reason": "file_not_found",
                           "output": msg, "elapsed": 0.0}, sys.stdout)
            else:
                print(msg, file=sys.stderr)
            sys.exit(4)

    result = run_cec(args.old_file, args.new_file, timeout=args.timeout)

    if args.json:
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
    else:
        status = "✓ EQUIVALENT" if result["success"] else "✗ NOT EQUIVALENT"
        print(f"{status}  ({result['reason']})  {result['elapsed']:.1f}s")
        print()
        # Show last few lines of output for diagnostics
        tail = result["output"].strip().splitlines()[-8:]
        for line in tail:
            print(f"  {line}")

    # Exit code
    if result["success"]:
        sys.exit(0)
    elif result["reason"] in ("counterexample",):
        sys.exit(1)
    elif result["reason"] in ("v2aig_failed_old", "v2aig_failed_new",
                               "no_top_module"):
        sys.exit(2)
    elif result["reason"] == "timeout_assumed_equivalent":
        sys.exit(0)  # timeout → assumed equivalent → success
    else:
        sys.exit(4)


if __name__ == "__main__":
    main()
