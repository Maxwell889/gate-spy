#!/usr/bin/env python3
"""
Yosys CEC (Combinational Equivalence Check) — standalone script.

Verifies that two Verilog files implement the same combinational function.

Usage:
    python scripts/cec.py <old_file> <new_file>
    python scripts/cec.py <old_file> <new_file> --timeout 300
    python scripts/cec.py <old_file> <new_file> --json   # JSON output

Exit codes:
    0 — equivalent
    1 — not equivalent
    2 — syntax error
    3 — timeout
    4 — other Yosys error
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time


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


# ---------------------------------------------------------------------------
#  CEC core
# ---------------------------------------------------------------------------

def run_cec(old_path: str, new_path: str,
            timeout: int = 120) -> dict:
    """Run Yosys CEC between two Verilog files.

    Args:
        old_path: path to the original (golden) Verilog file.
        new_path: path to the modified (gate) Verilog file.
        timeout: seconds before killing Yosys.

    Returns:
        {
            "success": bool,
            "reason": "equivalent" | "not_equivalent" | "syntax_error"
                      | "timeout" | "yosys_error" | "unknown",
            "output": str,        # Yosys stdout + stderr
            "elapsed": float,     # wall-clock seconds
        }
    """
    old_code = open(old_path, encoding="utf-8").read()
    new_code_raw = open(new_path, encoding="utf-8").read()
    new_code = _expand_pow_for_cec(new_code_raw)

    top_old = extract_top_module(old_code)
    top_new = extract_top_module(new_code) or top_old
    if not top_old:
        return {"success": False, "reason": "no_top_module",
                "output": "no 'module' declaration found in old file",
                "elapsed": 0.0}

    # Write expanded new code to a temp file so Yosys reads the processed version.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".v",
                                     prefix="cec_exp_", delete=False) as tf:
        tf.write(new_code)
        new_exp_path = tf.name

    try:
        script = "\n".join([
            f"read_verilog -sv {old_path}",
            "hierarchy -auto-top; proc; opt; flatten; opt",
            "design -stash gold",
            f"read_verilog -sv {new_exp_path}",
            "hierarchy -auto-top; proc; opt; flatten; opt",
            "design -stash gate",
            f"design -copy-from gold -as gold {top_old}",
            f"design -copy-from gate -as gate {top_old}",
            "equiv_make gold gate equiv",
            "equiv_simple -short",
            "equiv_induct -seq 0",
            "equiv_status -assert",
        ])
        t0 = time.time()
        proc = subprocess.run(
            ["yosys", "-s", "/dev/stdin"],
            input=script, capture_output=True, text=True,
            timeout=timeout,
        )
        elapsed = time.time() - t0
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        output_lower = output.lower()

        if proc.returncode != 0:
            if "syntax error" in output_lower or "parser error" in output_lower:
                return {"success": False, "reason": "syntax_error",
                        "output": output, "elapsed": elapsed}
            if any(w in output_lower for w in ("fail", "disproved", "not equivalent")):
                return {"success": False, "reason": "not_equivalent",
                        "output": output, "elapsed": elapsed}
            return {"success": False, "reason": "yosys_error",
                    "output": output, "elapsed": elapsed}

        if any(w in output_lower for w in ("success", "proved", "equivalent")):
            return {"success": True, "reason": "equivalent",
                    "output": output, "elapsed": elapsed}
        if any(w in output_lower for w in ("fail", "disproved", "not equivalent")):
            return {"success": False, "reason": "not_equivalent",
                    "output": output, "elapsed": elapsed}
        return {"success": False, "reason": "unknown",
                "output": output, "elapsed": elapsed}
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return {"success": False, "reason": "timeout",
                "output": f"CEC timed out after {timeout}s",
                "elapsed": elapsed}
    finally:
        try:
            os.unlink(new_exp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Yosys CEC — verify two Verilog files are functionally equivalent."
    )
    parser.add_argument("old_file", help="Path to the original (golden) Verilog file.")
    parser.add_argument("new_file", help="Path to the modified Verilog file.")
    parser.add_argument("--timeout", type=int, default=180,
                        help="Yosys timeout in seconds (default: 180).")
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
    elif result["reason"] == "syntax_error":
        sys.exit(2)
    elif result["reason"] == "timeout":
        sys.exit(3)
    elif result["reason"] == "not_equivalent":
        sys.exit(1)
    else:
        sys.exit(4)


if __name__ == "__main__":
    main()
