"""Hypothesis rendering and sample checking for word-level RTL recovery."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from .sampling import Sample
from .words import Word

_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_SIZED_NUM_RE = re.compile(r"\b(\d+)'([bBoOdDhH])([0-9a-fA-F_xXzZ]+)\b")
_DECL_RE = re.compile(
    r"\b(?:wire|reg|logic)\s+(?:signed\s+)?(?:\[(\d+)\s*:\s*(\d+)\])?\s*(.+?)\s*;",
    re.DOTALL,
)
_ASSIGN_RE = re.compile(r"\bassign\s+(.+?)\s*=\s*(.+?)\s*;", re.DOTALL)


@dataclass
class HypothesisRecord:
    id: int
    assignments: dict[str, str]
    declarations: str = ""
    rtl: str = ""
    sample_status: str = "not_run"
    sample_mismatches: list[dict[str, Any]] = field(default_factory=list)
    cec_status: str = "not_run"
    cec: dict[str, Any] = field(default_factory=dict)
    cost: int | None = None
    note: str = ""


def _verilog_number_to_int(match: re.Match) -> str:
    _width, base, digits = match.groups()
    digits = digits.replace("_", "").replace("x", "0").replace("X", "0")
    digits = digits.replace("z", "0").replace("Z", "0")
    radix = {"b": 2, "o": 8, "d": 10, "h": 16}[base.lower()]
    return str(int(digits or "0", radix))


def _to_python_expr(expr: str) -> str:
    if "?" in expr:
        raise ValueError("sample evaluator does not support Verilog ternary ?: yet")
    expr = _SIZED_NUM_RE.sub(_verilog_number_to_int, expr)
    expr = re.sub(r"\$(signed|unsigned)\s*\(", "(", expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"(?<![=!<>])!(?!=)", " not ", expr)
    return expr


_ALLOWED_NODES = {
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor, ast.Invert,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
}


def eval_expr(expr: str, env: dict[str, int]) -> int:
    """Evaluate a side-effect-free arithmetic expression over word values."""
    py_expr = _to_python_expr(expr)
    tree = ast.parse(py_expr, mode="eval")
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ValueError(f"unsupported expression construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in env:
            raise KeyError(f"unknown identifier {node.id!r} in expression {expr!r}")
    value = eval(compile(tree, "<hypothesis>", "eval"), {"__builtins__": {}}, env)
    return int(value)


def parse_local_widths(declarations: str) -> dict[str, int]:
    widths: dict[str, int] = {}
    for m in _DECL_RE.finditer(declarations or ""):
        msb_s, lsb_s, names = m.groups()
        width = abs(int(msb_s) - int(lsb_s)) + 1 if msb_s else 1
        for name in re.findall(r"\b[A-Za-z_]\w*\b", names):
            widths[name] = width
    return widths


def parse_internal_assigns(declarations: str) -> list[tuple[str, str]]:
    assigns: list[tuple[str, str]] = []
    for m in _ASSIGN_RE.finditer(declarations or ""):
        lhs = m.group(1).strip()
        rhs = m.group(2).strip()
        if re.match(r"^[A-Za-z_]\w*$", lhs):
            assigns.append((lhs, rhs))
    return assigns


def check_samples(assignments: dict[str, str],
                  declarations: str,
                  samples: list[Sample],
                  output_words: dict[str, Word]) -> tuple[str, list[dict[str, Any]]]:
    """Evaluate a hypothesis on samples and return ``(status, mismatches)``."""
    local_widths = parse_local_widths(declarations)
    internal_assigns = parse_internal_assigns(declarations)
    mismatches: list[dict[str, Any]] = []

    try:
        for idx, sample in enumerate(samples):
            env = dict(sample.inputs)

            for lhs, rhs in internal_assigns:
                width = local_widths.get(lhs)
                mask = (1 << width) - 1 if width else None
                value = eval_expr(rhs, env)
                env[lhs] = value & mask if mask is not None else value

            for out_name, expr in assignments.items():
                if out_name not in output_words:
                    raise KeyError(f"{out_name!r} is not an output word")
                word = output_words[out_name]
                got = eval_expr(expr, env) & word.mask
                env[out_name] = got
                expected = sample.outputs[out_name] & word.mask
                if got != expected:
                    mismatches.append({
                        "sample": idx,
                        "output": out_name,
                        "expected": expected,
                        "actual": got,
                        "inputs": dict(sample.inputs),
                    })
                    if len(mismatches) >= 8:
                        return "mismatch", mismatches
    except Exception as exc:
        return "error", [{"error": str(exc)}]

    return "pass", mismatches


def _decl(kind: str, word: Word) -> str:
    if word.width == 1:
        return f"  {kind} {word.name};"
    return f"  {kind} [{word.width - 1}:0] {word.name};"


def _extend_name(name: str, width: int, target_width: int) -> str:
    if target_width <= 0 or width == target_width:
        return name
    if width < target_width:
        return f"{{{target_width - width}'b0, {name}}}"
    return f"{name}[{target_width - 1}:0]"


def extend_expr_width(expr: str, widths: dict[str, int], target_width: int) -> str:
    """Zero-extend known word identifiers for safer Verilog expression width."""
    keywords = {"assign", "wire", "reg", "logic", "input", "output", "signed", "unsigned"}

    def repl(m: re.Match) -> str:
        name = m.group(0)
        if name in keywords or name not in widths:
            return name
        return _extend_name(name, widths[name], target_width)

    return _IDENT_RE.sub(repl, expr)


def _render_user_declarations(declarations: str, widths: dict[str, int]) -> str:
    if not declarations.strip():
        return ""

    local_widths = parse_local_widths(declarations)
    render_widths = {**widths, **local_widths}
    out: list[str] = []
    pos = 0
    for m in _ASSIGN_RE.finditer(declarations):
        before = declarations[pos:m.start()].strip()
        if before:
            out.append(before)
        lhs, rhs = m.group(1).strip(), m.group(2).strip()
        target_width = local_widths.get(lhs, widths.get(lhs, 0))
        rhs = extend_expr_width(rhs, render_widths, target_width) if target_width else rhs
        out.append(f"assign {lhs} = {rhs};")
        pos = m.end()
    tail = declarations[pos:].strip()
    if tail:
        out.append(tail)
    return "\n".join("  " + line.strip() for line in out if line.strip())


def render_hypothesis_rtl(module_name: str,
                          input_words: dict[str, Word],
                          output_words: dict[str, Word],
                          assignments: dict[str, str],
                          declarations: str = "") -> str:
    """Render a temporary word-level RTL module for hypothesis checking."""
    missing = [name for name in output_words if name not in assignments]
    if missing:
        raise ValueError(
            "hypothesis must assign every output word for CEC: "
            + ", ".join(missing)
        )

    ports = [*input_words.keys(), *output_words.keys()]
    widths = {
        **{name: word.width for name, word in input_words.items()},
        **{name: word.width for name, word in output_words.items()},
        **parse_local_widths(declarations),
    }
    lines = [f"module {module_name}({', '.join(ports)});"]
    lines.extend(_decl("input", word) for word in input_words.values())
    lines.extend(_decl("output", word) for word in output_words.values())
    rendered_decl = _render_user_declarations(declarations, widths)
    if rendered_decl:
        lines.append(rendered_decl)
    for out_name, word in output_words.items():
        expr = extend_expr_width(assignments[out_name], widths, word.width)
        lines.append(f"  assign {out_name} = {expr};")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"
