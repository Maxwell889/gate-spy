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
    expr = _SIZED_NUM_RE.sub(_verilog_number_to_int, expr)
    expr = _convert_bit_selects(expr)
    expr = _convert_ternary(expr)
    expr = re.sub(r"\$(signed|unsigned)\s*\(", "(", expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"(?<![=!<>])!(?!=)", " not ", expr)
    return expr


def _convert_bit_selects(expr: str) -> str:
    expr = re.sub(
        r"\b([A-Za-z_]\w*)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]",
        lambda m: f"bitsel({m.group(1)}, {m.group(2)}, {m.group(3)})",
        expr,
    )
    expr = re.sub(
        r"\b([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]",
        lambda m: f"bitsel({m.group(1)}, {m.group(2)}, {m.group(2)})",
        expr,
    )
    return expr


def _find_top_level_ternary(expr: str) -> tuple[int, int] | None:
    depth = 0
    qpos = -1
    nested = 0
    for i, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "?":
            if qpos == -1:
                qpos = i
            else:
                nested += 1
        elif depth == 0 and ch == ":" and qpos != -1:
            if nested:
                nested -= 1
            else:
                return qpos, i
    return None


def _convert_parenthesized_ternaries(expr: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(expr):
        if expr[i] != "(":
            out.append(expr[i])
            i += 1
            continue

        depth = 1
        j = i + 1
        while j < len(expr) and depth:
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
            j += 1
        if depth:
            out.append(expr[i])
            i += 1
            continue
        inner = expr[i + 1:j - 1]
        out.append(f"({_convert_ternary(inner)})")
        i = j
    return "".join(out)


def _convert_ternary(expr: str) -> str:
    expr = _convert_parenthesized_ternaries(expr)
    found = _find_top_level_ternary(expr)
    if found is None:
        return expr
    qpos, cpos = found
    cond = _convert_ternary(expr[:qpos].strip())
    true_expr = _convert_ternary(expr[qpos + 1:cpos].strip())
    false_expr = _convert_ternary(expr[cpos + 1:].strip())
    return f"(({true_expr}) if ({cond}) else ({false_expr}))"


_ALLOWED_NODES = {
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.IfExp, ast.Call,
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
        if isinstance(node, ast.Name) and node.id != "bitsel" and node.id not in env:
            raise KeyError(f"unknown identifier {node.id!r} in expression {expr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id != "bitsel":
                raise ValueError("only bitsel() calls are allowed in expressions")

    def bitsel(value: int, hi: int, lo: int) -> int:
        hi_i, lo_i = int(hi), int(lo)
        if hi_i < lo_i:
            hi_i, lo_i = lo_i, hi_i
        return (int(value) >> lo_i) & ((1 << (hi_i - lo_i + 1)) - 1)

    safe_env = dict(env)
    safe_env["bitsel"] = bitsel
    value = eval(compile(tree, "<hypothesis>", "eval"), {"__builtins__": {}}, safe_env)
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
    found = _find_top_level_ternary(expr)
    if found is not None:
        qpos, cpos = found
        cond = expr[:qpos].strip()
        true_expr = extend_expr_width(expr[qpos + 1:cpos].strip(), widths, target_width)
        false_expr = extend_expr_width(expr[cpos + 1:].strip(), widths, target_width)
        return f"{cond} ? ({true_expr}) : ({false_expr})"
    if target_width <= 1 and re.search(r"[?:]|[<>]=?|==|!=", expr):
        return expr

    def repl(m: re.Match) -> str:
        name = m.group(0)
        if m.end() < len(expr) and expr[m.end()] == "[":
            return name
        if name in keywords or name not in widths:
            return name
        return _extend_name(name, widths[name], target_width)

    return _IDENT_RE.sub(repl, expr)


def _strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    changed = True
    while changed and expr.startswith("(") and expr.endswith(")"):
        changed = False
        depth = 0
        for idx, ch in enumerate(expr):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0 and idx != len(expr) - 1:
                    break
        else:
            expr = expr[1:-1].strip()
            changed = True
    return expr


def _split_top_level(expr: str, sep: str = "+") -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(expr[start:idx].strip())
            start = idx + 1
    parts.append(expr[start:].strip())
    return [part for part in parts if part]


def _normalise_sum_expr(expr: str) -> str:
    return " + ".join(_split_top_level(_strip_outer_parens(expr), "+"))


def _iter_branch_exprs(expr: str) -> list[str]:
    expr = _strip_outer_parens(expr)
    found = _find_top_level_ternary(expr)
    if found is None:
        return [expr]
    qpos, cpos = found
    return (
        _iter_branch_exprs(expr[qpos + 1:cpos])
        + _iter_branch_exprs(expr[cpos + 1:])
    )


def _sum_prefixes(expr: str) -> list[str]:
    parts = _split_top_level(_strip_outer_parens(expr), "+")
    if len(parts) < 2:
        return []
    return [" + ".join(parts[:idx]) for idx in range(2, len(parts) + 1)]


def _sum_term_count(expr: str) -> int:
    return len(_split_top_level(_strip_outer_parens(expr), "+"))


def optimise_shared_wires(assignments: dict[str, str],
                          declarations: str,
                          input_words: dict[str, Word],
                          output_words: dict[str, Word],
                          min_occurrences: int = 2
                          ) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Extract repeated additive subexpressions into local wires.

    This is deliberately conservative: it only shares top-level ``+`` prefixes
    found inside assignment branches.  It is meant to capture contest-style
    repeated arithmetic bases without attempting a full Verilog AST rewrite.
    """
    existing = {
        *input_words.keys(),
        *output_words.keys(),
        *parse_local_widths(declarations).keys(),
    }
    counts: dict[str, int] = {}
    widths: dict[str, int] = {}

    for out_name, expr in assignments.items():
        out_width = output_words[out_name].width
        for branch in _iter_branch_exprs(expr):
            for prefix in _sum_prefixes(branch):
                norm = _normalise_sum_expr(prefix)
                if _sum_term_count(norm) < 2:
                    continue
                counts[norm] = counts.get(norm, 0) + 1
                widths[norm] = max(widths.get(norm, 1), out_width)

    selected = [
        expr for expr, count in counts.items()
        if count >= min_occurrences and _sum_term_count(expr) >= 2
    ]
    selected.sort(key=lambda expr: (_sum_term_count(expr), len(expr), expr))

    rendered_exprs: dict[str, str] = {}
    name_for_expr: dict[str, str] = {}
    wire_lines: list[str] = []
    assign_lines: list[str] = []
    for idx, expr in enumerate(selected):
        name = f"gs_cse{idx}"
        while name in existing:
            idx += 1
            name = f"gs_cse{idx}"
        existing.add(name)
        rhs = expr
        for old, old_name in sorted(rendered_exprs.items(), key=lambda kv: -len(kv[0])):
            rhs = rhs.replace(old, old_name)
        width = widths.get(expr, 1)
        wire_lines.append(f"wire [{width - 1}:0] {name};" if width > 1 else f"wire {name};")
        assign_lines.append(f"assign {name} = {rhs};")
        rendered_exprs[expr] = name
        name_for_expr[expr] = name

    if not name_for_expr:
        return declarations, dict(assignments), {
            "shared_count": 0,
            "shared_wires": [],
        }

    def replace_expr(expr: str) -> str:
        out = expr
        for old, name in sorted(name_for_expr.items(), key=lambda kv: -len(kv[0])):
            out = out.replace(old, name)
        return out

    new_assignments = {
        out_name: replace_expr(expr)
        for out_name, expr in assignments.items()
    }
    shared_decl = "\n".join([*wire_lines, *assign_lines])
    new_declarations = "\n".join(
        part for part in [declarations.strip(), shared_decl] if part
    )
    return new_declarations, new_assignments, {
        "shared_count": len(name_for_expr),
        "shared_wires": [
            {"name": name, "expr": expr, "width": widths.get(expr, 1), "uses": counts.get(expr, 0)}
            for expr, name in name_for_expr.items()
        ],
    }


def _render_user_declarations(declarations: str,
                              widths: dict[str, int],
                              extend_widths: bool = True) -> str:
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
        if extend_widths and target_width:
            rhs = extend_expr_width(rhs, render_widths, target_width)
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
                          declarations: str = "",
                          extend_widths: bool = True) -> str:
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
    rendered_decl = _render_user_declarations(declarations, widths, extend_widths)
    if rendered_decl:
        lines.append(rendered_decl)
    for out_name, word in output_words.items():
        expr = assignments[out_name]
        if extend_widths:
            expr = extend_expr_width(expr, widths, word.width)
        lines.append(f"  assign {out_name} = {expr};")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"
