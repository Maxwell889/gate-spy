"""Cost-guided RTL rewrite helpers.

This module is intentionally conservative.  It proposes bounded local rewrites
and leaves equivalence to the caller's CEC flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scripts.cost import ExprAnalyser, extract_modules, extract_widths, split_assignment, strip_comments


_ASSIGN_RE = re.compile(r"\bassign\s+([^;]*?);", re.DOTALL)
_DECL_INIT_RE = re.compile(
    r"(?P<indent>^[ \t]*)(?P<kind>wire|reg|integer)\s+"
    r"(?P<signed>signed\s+)?(?P<range>\[[^\]]+\]\s*)?"
    r"(?P<body>[^;]*=[^;]*);",
    re.MULTILINE | re.DOTALL,
)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*\b")
_SIGNED_EXT_RE = re.compile(
    r"\$signed\(\s*\{\s*([A-Za-z_][A-Za-z0-9_$]*)\[(\d+)\]\s*,\s*\1\s*\}\s*\)"
)
_ZERO_EXT_RE = re.compile(
    r"\{\s*(\d+)'d0\s*,\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\}"
)


@dataclass(frozen=True)
class RtlAssign:
    lhs: str
    rhs: str
    start: int
    end: int
    cost: int
    lhs_width: int
    kind: str = "assign"
    raw: str = ""
    decl_prefix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lhs": self.lhs,
            "rhs": self.rhs,
            "span": [self.start, self.end],
            "cost": self.cost,
            "lhs_width": self.lhs_width,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class RtlRewriteCandidate:
    candidate_id: str
    strategy: str
    reason: str
    code: str
    estimated_delta: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_code: bool = True) -> dict[str, Any]:
        data = {
            "candidate_id": self.candidate_id,
            "strategy": self.strategy,
            "reason": self.reason,
            "estimated_delta": self.estimated_delta,
            "details": self.details,
        }
        if include_code:
            data["code"] = self.code
        return data


def _top_module_body(code: str) -> tuple[str, str, dict[str, int]]:
    stripped = strip_comments(code)
    modules, top = extract_modules(stripped)
    if top is None:
        return "", "", {}
    body = modules.get(top, "")
    return top, body, extract_widths(body)


def _base_name(ref: str) -> str:
    return ref.strip().split("[", 1)[0].split(".", 1)[0]


def _signal_width(widths: dict[str, int], ref: str) -> int:
    return max(1, int(widths.get(_base_name(ref), 1)))


def _range_width(range_text: str | None) -> int:
    if not range_text:
        return 1
    match = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*", range_text)
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def _expr_cost(widths: dict[str, int], lhs: str, rhs: str) -> int:
    analyser = ExprAnalyser(widths)
    return analyser.count_selects(lhs) + analyser.analyse(rhs)


def _normalize_expr(expr: str) -> str:
    text = re.sub(r"\s+", "", expr)
    changed = True
    while changed and text.startswith("(") and text.endswith(")"):
        changed = False
        depth = 0
        for idx, ch in enumerate(text):
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
                if depth == 0 and idx != len(text) - 1:
                    return text
        if depth == 0:
            text = text[1:-1]
            changed = True
    return text


def _is_trivial_expr(expr: str) -> bool:
    text = _normalize_expr(expr)
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*|\d+(?:'[sdhboSDHBO][0-9A-Fa-f_xXzZ]+)?", text))


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def collect_assignments(code: str) -> tuple[list[RtlAssign], dict[str, int]]:
    _, _, widths = _top_module_body(code)
    assigns: list[RtlAssign] = []
    for match in _ASSIGN_RE.finditer(code):
        stmt = match.group(1)
        lhs, rhs, op = split_assignment(stmt)
        if op is None or not lhs or not rhs:
            continue
        assigns.append(RtlAssign(
            lhs=lhs.strip(),
            rhs=rhs.strip(),
            start=match.start(),
            end=match.end(),
            cost=_expr_cost(widths, lhs, rhs),
            lhs_width=_signal_width(widths, lhs),
            raw=match.group(0),
        ))
    for match in _DECL_INIT_RE.finditer(code):
        body = match.group("body").strip()
        parts = _split_top_level_commas(body)
        if len(parts) != 1:
            continue
        lhs, rhs, op = split_assignment(parts[0])
        if op is None or not lhs or not rhs:
            continue
        lhs_width = _range_width(match.group("range"))
        widths.setdefault(lhs.strip(), lhs_width)
        decl_prefix = (
            f"{match.group('indent')}{match.group('kind')} "
            f"{match.group('signed') or ''}{match.group('range') or ''}{lhs.strip()}"
        )
        assigns.append(RtlAssign(
            lhs=lhs.strip(),
            rhs=rhs.strip(),
            start=match.start(),
            end=match.end(),
            cost=_expr_cost(widths, lhs, rhs),
            lhs_width=lhs_width,
            kind="decl_init",
            raw=match.group(0),
            decl_prefix=decl_prefix,
        ))
    assigns.sort(key=lambda item: item.start)
    return assigns, widths


def _rewrite_assignment(assign: RtlAssign, rhs: str, *,
                        split_decl: bool = False) -> str:
    if assign.kind == "decl_init":
        prefix = assign.decl_prefix.rstrip()
        if split_decl:
            return f"{prefix};\n  assign {assign.lhs} = {rhs};"
        return f"{prefix} = {rhs};"
    return f"assign {assign.lhs} = {rhs};"


def _existing_names(code: str) -> set[str]:
    return set(_IDENT_RE.findall(code))


def _fresh_name(code: str, prefix: str) -> str:
    names = _existing_names(code)
    idx = 0
    while True:
        name = f"{prefix}{idx}"
        if name not in names:
            return name
        idx += 1


def _wire_decl(width: int, name: str, *, signed: bool = False) -> str:
    sign = " signed" if signed else ""
    if width <= 1:
        return f"  wire{sign} {name};"
    return f"  wire{sign} [{width - 1}:0] {name};"


def _signed_literal(width: int, value: int) -> str:
    if value < 0:
        return f"-{width}'sd{abs(value)}"
    return f"{width}'sd{value}"


def _insert_before_first_assign(code: str, lines: list[str]) -> str:
    insertion = "\n".join(lines).rstrip() + "\n"
    assign_match = _ASSIGN_RE.search(code)
    decl_match = _DECL_INIT_RE.search(code)
    starts = [
        match.start()
        for match in (assign_match, decl_match)
        if match is not None
    ]
    if starts:
        start = min(starts)
        return code[:start] + insertion + code[start:]
    endmodule = re.search(r"\bendmodule\b", code)
    if endmodule:
        return code[:endmodule.start()] + insertion + code[endmodule.start():]
    return code + "\n" + insertion


def _replace_spans(code: str, replacements: list[tuple[int, int, str]]) -> str:
    out = code
    for start, end, text in sorted(replacements, key=lambda item: item[0], reverse=True):
        out = out[:start] + text + out[end:]
    return out


def _remove_unused_initializer_decls(code: str) -> str:
    changed = True
    out = code
    while changed:
        changed = False
        for match in list(_DECL_INIT_RE.finditer(out)):
            body = match.group("body").strip()
            parts = _split_top_level_commas(body)
            if len(parts) != 1:
                continue
            lhs, rhs, op = split_assignment(parts[0])
            if op is None or not lhs or not rhs:
                continue
            name = lhs.strip()
            rest = out[:match.start()] + out[match.end():]
            if re.search(rf"\b{re.escape(name)}\b", rest):
                continue
            out = out[:match.start()] + out[match.end():]
            changed = True
            break
    return out


def analyze_rtl_cost(code: str, *, top_n: int = 10) -> dict[str, Any]:
    assigns, widths = collect_assignments(code)
    hotspots = sorted(assigns, key=lambda item: (-item.cost, item.lhs))[:max(0, top_n)]
    duplicate_groups: dict[str, list[RtlAssign]] = {}
    for assign in assigns:
        key = _normalize_expr(assign.rhs)
        if _is_trivial_expr(key):
            continue
        duplicate_groups.setdefault(key, []).append(assign)
    duplicate_exprs = [
        {
            "rhs": group[0].rhs,
            "count": len(group),
            "cost_each": group[0].cost,
            "lhs": [assign.lhs for assign in group],
            "estimated_saving": group[0].cost * (len(group) - 1),
        }
        for group in duplicate_groups.values()
        if len(group) > 1
    ]
    fragments: list[dict[str, Any]] = []
    for pattern_name, regex in (
        ("signed_extension", _SIGNED_EXT_RE),
        ("zero_extension", _ZERO_EXT_RE),
    ):
        counts: dict[str, int] = {}
        for match in regex.finditer(code):
            counts[match.group(0)] = counts.get(match.group(0), 0) + 1
        for text, count in counts.items():
            if count > 1:
                fragments.append({
                    "kind": pattern_name,
                    "fragment": text,
                    "count": count,
                })
    hints: list[str] = []
    if duplicate_exprs:
        hints.append("try exact common-subexpression extraction")
    if any(item["kind"] == "signed_extension" for item in fragments):
        hints.append("try hoisting repeated $signed({sign, word}) extensions")
    if any(item["kind"] == "zero_extension" for item in fragments):
        hints.append("try hoisting repeated zero-extension concatenations")
    if _find_mux_add_groups(assigns):
        hints.append("try factoring mux-plus-common-addition families")
    if any(_concat_affine_expr(assign.rhs, assign.lhs_width) for assign in assigns):
        hints.append("try rewriting concat bit-pattern initializers as affine/modular forms")
    if _proposal_output_relation_factor(code, assigns, limit=1):
        hints.append("try factoring predicates through shared output/difference relations")
    if _proposal_signed_alias_collapse(code, assigns, widths, limit=1):
        hints.append("try collapsing signed alias wires into signed ports/outputs")
    return {
        "assignment_count": len(assigns),
        "hotspots": [assign.to_dict() for assign in hotspots],
        "duplicate_expressions": duplicate_exprs,
        "repeated_fragments": fragments,
        "hints": hints,
        "widths": widths,
    }


def _split_top_level_plus(expr: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "+" and depth == 0:
            parts.append(expr[start:idx].strip())
            start = idx + 1
    parts.append(expr[start:].strip())
    return [part for part in parts if part]


def _strip_one_outer_paren(text: str) -> str:
    stripped = text.strip()
    if not (stripped.startswith("(") and stripped.endswith(")")):
        return stripped
    depth = 0
    for idx, ch in enumerate(stripped):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and idx != len(stripped) - 1:
                return stripped
    return stripped[1:-1].strip()


def _parse_mux_zero(term: str) -> tuple[str, str] | None:
    text = _strip_one_outer_paren(term)
    depth = 0
    qpos = -1
    cpos = -1
    for idx, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0:
            qpos = idx
        elif ch == ":" and depth == 0 and qpos >= 0:
            cpos = idx
            break
    if qpos < 0 or cpos < 0:
        return None
    cond = text[:qpos].strip()
    if_true = text[qpos + 1:cpos].strip()
    if_false = text[cpos + 1:].strip()
    if _normalize_expr(if_false) not in {"0", "1'd0", "8'd0", "16'd0", "32'd0", "64'd0"}:
        if not re.fullmatch(r"\d+'[sdhboSDHBO]0+", _normalize_expr(if_false)):
            return None
    if not cond or not if_true:
        return None
    return cond, if_true


def _find_mux_add_groups(assigns: list[RtlAssign]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for assign in assigns:
        terms = _split_top_level_plus(assign.rhs)
        for idx, term in enumerate(terms):
            parsed = _parse_mux_zero(term)
            if parsed is None:
                continue
            cond, value = parsed
            common = terms[:idx] + terms[idx + 1:]
            if not common:
                continue
            rows.append({
                "assign": assign,
                "cond": cond,
                "value": value,
                "common": common,
                "key": (
                    tuple(sorted(_normalize_expr(part) for part in common)),
                    _normalize_expr(value),
                ),
            })
    groups: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["key"], []).append(row)
    return [group for group in groups.values() if len(group) > 1]


def _proposal_exact_cse(code: str, assigns: list[RtlAssign],
                        *, limit: int) -> list[RtlRewriteCandidate]:
    groups: dict[str, list[RtlAssign]] = {}
    for assign in assigns:
        key = _normalize_expr(assign.rhs)
        if _is_trivial_expr(key) or assign.cost <= 0:
            continue
        groups.setdefault(key, []).append(assign)
    candidates: list[RtlRewriteCandidate] = []
    for group in sorted(groups.values(), key=lambda g: -(g[0].cost * (len(g) - 1))):
        if len(group) < 2:
            continue
        name = _fresh_name(code, "_opt_cse")
        width = max(assign.lhs_width for assign in group)
        lines = [
            _wire_decl(width, name),
            f"  assign {name} = {group[0].rhs};",
        ]
        replaced = _replace_spans(
            code,
            [
                (assign.start, assign.end, _rewrite_assignment(
                    assign, name, split_decl=True))
                for assign in group
            ],
        )
        new_code = _insert_before_first_assign(replaced, lines)
        candidates.append(RtlRewriteCandidate(
            candidate_id=f"R{len(candidates) + 1}",
            strategy="exact_cse",
            reason=f"share repeated RHS used by {len(group)} assignments",
            code=new_code,
            estimated_delta=group[0].cost * (len(group) - 1),
            details={"lhs": [assign.lhs for assign in group], "rhs": group[0].rhs},
        ))
        if len(candidates) >= limit:
            break
    return candidates


def _proposal_extension_hoist(code: str, widths: dict[str, int],
                              *, limit: int) -> list[RtlRewriteCandidate]:
    candidates: list[RtlRewriteCandidate] = []
    fragment_specs: list[tuple[str, str, int, bool, str]] = []
    for match in _SIGNED_EXT_RE.finditer(code):
        word = match.group(1)
        width = int(match.group(2)) + 2
        fragment_specs.append((match.group(0), "signed_extension_hoist", width, True, f"{{{word}[{match.group(2)}], {word}}}"))
    for match in _ZERO_EXT_RE.finditer(code):
        ext_width = int(match.group(1))
        word = match.group(2)
        width = ext_width + _signal_width(widths, word)
        fragment_specs.append((match.group(0), "zero_extension_hoist", width, False, match.group(0)))

    counts: dict[str, tuple[str, int, bool, str, int]] = {}
    for fragment, strategy, width, signed, rhs in fragment_specs:
        if fragment not in counts:
            counts[fragment] = (strategy, width, signed, rhs, 0)
        old = counts[fragment]
        counts[fragment] = (old[0], old[1], old[2], old[3], old[4] + 1)

    for fragment, (strategy, width, signed, rhs, count) in sorted(
        counts.items(), key=lambda item: -item[1][4]
    ):
        if count < 2:
            continue
        prefix = "_opt_sx" if signed else "_opt_zx"
        name = _fresh_name(code, prefix)
        replaced = code.replace(fragment, name)
        lines = [
            _wire_decl(width, name, signed=signed),
            f"  assign {name} = {rhs};",
        ]
        new_code = _insert_before_first_assign(replaced, lines)
        candidates.append(RtlRewriteCandidate(
            candidate_id=f"R{len(candidates) + 1}",
            strategy=strategy,
            reason=f"hoist repeated fragment used {count} times",
            code=new_code,
            estimated_delta=count - 1,
            details={"fragment": fragment, "count": count, "wire": name},
        ))
        if len(candidates) >= limit:
            break
    return candidates


def _proposal_mux_add_factor(code: str, assigns: list[RtlAssign],
                             *, limit: int) -> list[RtlRewriteCandidate]:
    candidates: list[RtlRewriteCandidate] = []
    groups = _find_mux_add_groups(assigns)
    for group in groups[:limit]:
        base_name = _fresh_name(code, "_opt_base")
        with_name = _fresh_name(code + " " + base_name, "_opt_with")
        width = max(row["assign"].lhs_width for row in group)
        common_terms = group[0]["common"]
        common_expr = " + ".join(common_terms)
        value_expr = group[0]["value"]
        lines = [
            _wire_decl(width, base_name),
            _wire_decl(width, with_name),
            f"  assign {base_name} = {common_expr};",
            f"  assign {with_name} = {value_expr} + {base_name};",
        ]
        replacements = []
        for row in group:
            assign = row["assign"]
            cond = row["cond"]
            replacements.append(
                (assign.start, assign.end, _rewrite_assignment(
                    assign, f"{cond} ? {with_name} : {base_name}",
                    split_decl=True))
            )
        replaced = _replace_spans(code, replacements)
        new_code = _insert_before_first_assign(replaced, lines)
        candidates.append(RtlRewriteCandidate(
            candidate_id=f"R{len(candidates) + 1}",
            strategy="mux_add_factor",
            reason="factor common addition shared by mux-zero output family",
            code=new_code,
            estimated_delta=sum(row["assign"].cost for row in group) - len(lines),
            details={
                "lhs": [row["assign"].lhs for row in group],
                "common": common_expr,
                "mux_value": value_expr,
            },
        ))
    return candidates


def _parse_verilog_int(text: str) -> tuple[int, int] | None:
    compact = text.strip().replace("_", "")
    sized = re.fullmatch(r"(\d+)'([bBdDhHoO])([0-9A-Fa-fxXzZ]+)", compact)
    if sized:
        width = int(sized.group(1))
        base_ch = sized.group(2).lower()
        digits = sized.group(3).lower()
        if "x" in digits or "z" in digits:
            return None
        base = {"b": 2, "d": 10, "h": 16, "o": 8}[base_ch]
        return int(digits, base), width
    if re.fullmatch(r"\d+", compact):
        return int(compact), max(1, int(compact).bit_length())
    return None


def _concat_parts(expr: str) -> list[str] | None:
    text = expr.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    depth = 0
    for idx, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and idx != len(text) - 1:
                return None
    return _split_top_level_commas(text[1:-1])


def _concat_affine_expr(expr: str, width: int) -> tuple[str, dict[str, Any]] | None:
    parts = _concat_parts(expr)
    if not parts:
        return None
    bits: list[tuple[str, int | str | None]] = []
    for part in parts:
        parsed_const = _parse_verilog_int(part)
        if parsed_const is not None:
            value, part_width = parsed_const
            for bit_idx in range(part_width - 1, -1, -1):
                bits.append(("const", (value >> bit_idx) & 1))
            continue
        neg = False
        atom = part.strip()
        if atom.startswith("!"):
            neg = True
            atom = atom[1:].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(?:\[\d+\])?", atom):
            return None
        bits.append(("not" if neg else "var", atom))
    if len(bits) < width:
        bits = [("const", 0)] * (width - len(bits)) + bits
    elif len(bits) > width:
        bits = bits[-width:]

    const = 0
    coeffs: dict[str, int] = {}
    for out_idx, (kind, value) in enumerate(reversed(bits)):
        weight = 1 << out_idx
        if kind == "const":
            const += int(value) * weight
            continue
        atom = str(value)
        if kind == "not":
            const += weight
            coeffs[atom] = coeffs.get(atom, 0) - weight
        else:
            coeffs[atom] = coeffs.get(atom, 0) + weight
    if not coeffs:
        return None

    positives = [(name, coeff) for name, coeff in coeffs.items() if coeff > 0]
    negatives = [(name, -coeff) for name, coeff in coeffs.items() if coeff < 0]
    if not positives and not negatives:
        return None

    current = f"{width}'d{const}" if const else ""
    for name, coeff in sorted(positives):
        term = name if coeff == 1 else f"({width}'d{coeff} * {name})"
        current = term if not current else f"({current} + {term})"
    for name, coeff in sorted(negatives):
        term = name if coeff == 1 else f"({width}'d{coeff} * {name})"
        base = current or f"{width}'d0"
        current = f"({base} - {term})"
    return current, {
        "constant": const,
        "coefficients": coeffs,
    }


def _conditional_subtract_from_affine(details: dict[str, Any],
                                      width: int) -> str | None:
    coeffs = details.get("coefficients")
    const = details.get("constant")
    if not isinstance(coeffs, dict) or not isinstance(const, int):
        return None
    bases: dict[str, dict[int, int]] = {}
    for name, coeff in coeffs.items():
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)\[(\d+)\]", str(name))
        if not match:
            return None
        bases.setdefault(match.group(1), {})[int(match.group(2))] = int(coeff)
    if len(bases) != 1:
        return None
    base, bit_coeffs = next(iter(bases.items()))
    if set(bit_coeffs) != {0, 1} or bit_coeffs[0] != -1:
        return None
    high_const = const + bit_coeffs[1] + 2
    low_const = const
    if high_const < 0 or low_const < 0:
        return None
    return f"(({base}[1] ? {width}'d{high_const} : {width}'d{low_const}) - {base})"


def _modular_subtract_from_affine(details: dict[str, Any]) -> tuple[int, str] | None:
    coeffs = details.get("coefficients")
    const = details.get("constant")
    if not isinstance(coeffs, dict) or not isinstance(const, int):
        return None
    bases: dict[str, dict[int, int]] = {}
    for name, coeff in coeffs.items():
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)\[(\d+)\]", str(name))
        if not match:
            return None
        bases.setdefault(match.group(1), {})[int(match.group(2))] = int(coeff)
    if len(bases) != 1:
        return None
    base, bit_coeffs = next(iter(bases.items()))

    # Conservative v1: detect the common two-bit pattern produced by assigning
    # a wrapped 3-bit subtraction into a wider word.  Example:
    #   {2'd0, x[1], x[1], !x[0]} == zero_extend(3'd1 - x)
    if set(bit_coeffs) != {0, 1} or const < 0:
        return None
    tmp_width = 3
    if bit_coeffs[0] != -1 or bit_coeffs[1] != (1 << tmp_width) - 2:
        return None
    if const >= (1 << tmp_width):
        return None
    return tmp_width, f"{tmp_width}'d{const} - {base}"


def _proposal_concat_affine(code: str, assigns: list[RtlAssign],
                            *, limit: int) -> list[RtlRewriteCandidate]:
    candidates: list[RtlRewriteCandidate] = []
    for assign in assigns:
        converted = _concat_affine_expr(assign.rhs, assign.lhs_width)
        if converted is None:
            continue
        new_rhs, details = converted
        alternatives = []
        modular = _modular_subtract_from_affine(details)
        if modular is not None:
            tmp_width, tmp_rhs = modular
            tmp_name = _fresh_name(code, "_opt_modsub")
            if assign.kind == "decl_init":
                replacement = "\n".join([
                    _wire_decl(tmp_width, tmp_name),
                    f"  assign {tmp_name} = {tmp_rhs};",
                    _rewrite_assignment(assign, tmp_name),
                ])
            else:
                replacement = "\n".join([
                    _wire_decl(tmp_width, tmp_name),
                    f"  assign {tmp_name} = {tmp_rhs};",
                    _rewrite_assignment(assign, tmp_name),
                ])
            new_code = _replace_spans(code, [(assign.start, assign.end, replacement)])
            candidates.append(RtlRewriteCandidate(
                candidate_id=f"R{len(candidates) + 1}",
                strategy="concat_modular_subtract",
                reason="rewrite concat bit-pattern into explicit narrow modular subtract",
                code=new_code,
                estimated_delta=assign.cost,
                details={
                    "lhs": assign.lhs,
                    "old_rhs": assign.rhs,
                    "tmp_width": tmp_width,
                    "tmp_rhs": tmp_rhs,
                    **details,
                },
            ))
            if len(candidates) >= limit:
                return candidates
        conditional = _conditional_subtract_from_affine(details, assign.lhs_width)
        if conditional is not None:
            alternatives.append((
                "concat_conditional_subtract",
                "rewrite concat bit-pattern into conditional subtract over the source word",
                conditional,
            ))
        alternatives.append((
            "concat_affine",
            "rewrite concat of input bits/inversions into lower-cost affine form",
            new_rhs,
        ))
        for strategy, reason, rhs in alternatives:
            new_stmt = _rewrite_assignment(assign, rhs)
            new_code = _replace_spans(code, [(assign.start, assign.end, new_stmt)])
            candidates.append(RtlRewriteCandidate(
                candidate_id=f"R{len(candidates) + 1}",
                strategy=strategy,
                reason=reason,
                code=new_code,
                estimated_delta=assign.cost,
                details={"lhs": assign.lhs, "old_rhs": assign.rhs, "new_rhs": rhs, **details},
            ))
            if len(candidates) >= limit:
                return candidates
    return candidates


_PLAIN_DECL_RE = re.compile(
    r"(?P<indent>^[ \t]*)(?P<kind>input|output|wire|reg)\s+"
    r"(?P<signed>signed\s+)?(?P<range>\[[^\]]+\]\s*)?"
    r"(?P<body>[^;=]*);",
    re.MULTILINE,
)


def _mark_declared_signed(code: str, *, kind: str, signals: set[str]) -> str:
    if not signals:
        return code

    def _handle(match: re.Match[str]) -> str:
        if match.group("kind") != kind:
            return match.group(0)
        body = match.group("body").strip()
        names = [part.strip() for part in body.split(",") if part.strip()]
        if not names or not any(name in signals for name in names):
            return match.group(0)
        if match.group("signed"):
            return match.group(0)
        signed_names = [name for name in names if name in signals]
        other_names = [name for name in names if name not in signals]
        indent = match.group("indent")
        range_text = match.group("range") or ""
        lines = [
            f"{indent}{kind} signed {range_text}{', '.join(signed_names)};"
        ]
        if other_names:
            lines.append(f"{indent}{kind} {range_text}{', '.join(other_names)};")
        return "\n".join(lines)

    return _PLAIN_DECL_RE.sub(_handle, code)


def _replace_identifier(code: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, code)


def _proposal_signed_alias_collapse(code: str,
                                    assigns: list[RtlAssign],
                                    widths: dict[str, int],
                                    *,
                                    limit: int) -> list[RtlRewriteCandidate]:
    if limit <= 0:
        return []
    definitions = {assign.lhs: assign.rhs for assign in assigns}
    by_rhs = {_normalize_expr(assign.rhs): assign for assign in assigns}
    replacements: list[tuple[int, int, str]] = []
    renames: dict[str, str] = {}
    signed_inputs: set[str] = set()
    signed_outputs: set[str] = set()

    for alias in assigns:
        if alias.kind != "decl_init" or "signed" not in alias.raw:
            continue
        rhs_base = _base_name(alias.rhs)
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", alias.rhs.strip())
            and rhs_base in widths
            and _signal_width(widths, rhs_base) == alias.lhs_width
        ):
            signed_inputs.add(rhs_base)
            renames[alias.lhs] = rhs_base
            replacements.append((alias.start, alias.end, ""))
            continue

        normalized_alias = _normalize_expr(alias.lhs)
        out_assign = by_rhs.get(normalized_alias)
        if out_assign is None or out_assign.kind != "assign":
            continue
        if _signal_width(widths, out_assign.lhs) != alias.lhs_width:
            continue
        signed_outputs.add(_base_name(out_assign.lhs))
        renames[alias.lhs] = out_assign.lhs
        replacements.append((alias.start, alias.end, ""))
        replacements.append((
            out_assign.start,
            out_assign.end,
            _rewrite_assignment(out_assign, alias.rhs),
        ))

    if not renames:
        return []

    new_code = _replace_spans(code, replacements)
    for old, new in sorted(renames.items(), key=lambda item: -len(item[0])):
        new_code = _replace_identifier(new_code, old, new)
    new_code = _mark_declared_signed(new_code, kind="input", signals=signed_inputs)
    new_code = _mark_declared_signed(new_code, kind="output", signals=signed_outputs)
    new_code = re.sub(r"\n{3,}", "\n\n", new_code)

    if new_code == code:
        return []
    return [RtlRewriteCandidate(
        candidate_id="R1",
        strategy="signed_alias_collapse",
        reason="collapse signed alias wires into signed inputs/outputs and reuse outputs in predicates",
        code=new_code,
        estimated_delta=len(renames),
        details={
            "aliases": renames,
            "signed_inputs": sorted(signed_inputs),
            "signed_outputs": sorted(signed_outputs),
        },
    )]


def _parse_add_const(expr: str) -> tuple[str, int] | None:
    text = _strip_one_outer_paren(expr)
    match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_$]*)\s*([+-])\s*(\d+)'[dD](\d+)", text)
    if match:
        value = int(match.group(4))
        return match.group(1), value if match.group(2) == "+" else -value
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", text):
        return text, 0
    return None


def _add_const_options(expr: str, definitions: dict[str, str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for candidate in (expr, definitions.get(expr, expr)):
        parsed = _parse_add_const(candidate)
        if parsed is not None and parsed not in out:
            out.append(parsed)
    return out


def _parse_binary(expr: str, ops: tuple[str, ...]) -> tuple[str, str, str] | None:
    text = _strip_one_outer_paren(expr)
    depth = 0
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            for op in sorted(ops, key=len, reverse=True):
                if text.startswith(op, idx):
                    return text[:idx].strip(), op, text[idx + len(op):].strip()
        idx += 1
    return None


def _proposal_output_relation_factor(code: str, assigns: list[RtlAssign],
                                     *, limit: int) -> list[RtlRewriteCandidate]:
    definitions = {assign.lhs: assign.rhs for assign in assigns}
    wide_outputs: list[tuple[RtlAssign, str, str, int]] = []
    for assign in assigns:
        parsed = _parse_binary(assign.rhs, ("-",))
        if parsed is None:
            continue
        word, _, base_expr = parsed
        base = _parse_add_const(definitions.get(base_expr, base_expr))
        if base is None:
            continue
        base_word, base_const = base
        wide_outputs.append((assign, word, base_word, base_const))

    candidates: list[RtlRewriteCandidate] = []
    replacements: list[tuple[int, int, str]] = []
    helper_lines: list[str] = []
    used_comparators: set[str] = set()
    helper_index = 0

    for out_assign, word, base_word, base_const in wide_outputs:
        helper = _fresh_name(code + " ".join(helper_lines), f"_opt_diff{helper_index}_")
        helper_index += 1
        helper_lines.extend([
            _wire_decl(out_assign.lhs_width, helper, signed=True),
            f"  assign {helper} = {out_assign.rhs};",
        ])
        replacements.append((
            out_assign.start,
            out_assign.end,
            _rewrite_assignment(out_assign, helper, split_decl=True),
        ))

        for cmp_assign in assigns:
            if cmp_assign is out_assign or cmp_assign.lhs in used_comparators:
                continue
            parsed = _parse_binary(cmp_assign.rhs, (">=", "<=", ">", "<"))
            if parsed is None:
                continue
            lhs, op, rhs = parsed
            lhs_options = _add_const_options(lhs, definitions)
            rhs_options = _add_const_options(rhs, definitions)
            new_rhs = ""
            rhs_base = next((item for item in rhs_options if item[0] == base_word), None)
            lhs_base = next((item for item in lhs_options if item[0] == base_word), None)
            rhs_word = next((item for item in rhs_options if item[0] == word), None)
            lhs_word = next((item for item in lhs_options if item[0] == word), None)
            if lhs_word and rhs_base and op in (">=", ">"):
                threshold = rhs_base[1] - base_const - lhs_word[1]
                if op == ">=":
                    new_rhs = f"{helper} > {_signed_literal(out_assign.lhs_width, threshold - 1)}"
                else:
                    new_rhs = f"{helper} > {_signed_literal(out_assign.lhs_width, threshold)}"
            elif (
                lhs_base
                and rhs_word
                and op in (">=", ">")
            ):
                threshold = -(base_const + rhs_word[1] - lhs_base[1])
                if op == ">=":
                    new_rhs = f"{helper} < {_signed_literal(out_assign.lhs_width, threshold + 1)}"
                else:
                    new_rhs = f"{helper} < {_signed_literal(out_assign.lhs_width, threshold)}"
            elif lhs_word and rhs_base and op in ("<=", "<"):
                threshold = rhs_base[1] - base_const - lhs_word[1]
                if op == "<=":
                    new_rhs = f"{helper} < {_signed_literal(out_assign.lhs_width, threshold + 1)}"
                else:
                    new_rhs = f"{helper} < {_signed_literal(out_assign.lhs_width, threshold)}"
            elif (
                lhs_base
                and rhs_word
                and op in ("<=", "<")
            ):
                threshold = -(base_const + rhs_word[1] - lhs_base[1])
                if op == "<=":
                    new_rhs = f"{helper} > {_signed_literal(out_assign.lhs_width, threshold - 1)}"
                else:
                    new_rhs = f"{helper} > {_signed_literal(out_assign.lhs_width, threshold)}"
            if not new_rhs:
                continue
            replacements.append((
                cmp_assign.start,
                cmp_assign.end,
                _rewrite_assignment(cmp_assign, new_rhs, split_decl=True),
            ))
            used_comparators.add(cmp_assign.lhs)

    if not helper_lines or not used_comparators:
        return []
    new_code = _replace_spans(code, replacements)
    new_code = _insert_before_first_assign(new_code, helper_lines)
    new_code = _remove_unused_initializer_decls(new_code)
    candidates.append(RtlRewriteCandidate(
        candidate_id="R1",
        strategy="output_relation_factor",
        reason="share wide output difference and express related predicates from it",
        code=new_code,
        estimated_delta=len(used_comparators),
        details={"predicate_count": len(used_comparators)},
    ))
    return candidates[:limit]


def propose_rtl_rewrites(code: str, *,
                         strategies: str = "all",
                         max_candidates: int = 8) -> list[RtlRewriteCandidate]:
    assigns, widths = collect_assignments(code)
    wanted = {
        item.strip()
        for item in strategies.split(",")
        if item.strip()
    }
    if not wanted or "all" in wanted:
        wanted = {
            "exact_cse",
            "extension_hoist",
            "mux_add_factor",
            "concat_affine",
            "concat_modular_subtract",
            "concat_conditional_subtract",
            "output_relation_factor",
            "signed_alias_collapse",
        }
    candidates: list[RtlRewriteCandidate] = []
    if "exact_cse" in wanted:
        candidates.extend(_proposal_exact_cse(
            code, assigns, limit=max_candidates - len(candidates)))
    if len(candidates) < max_candidates and "extension_hoist" in wanted:
        candidates.extend(_proposal_extension_hoist(
            code, widths, limit=max_candidates - len(candidates)))
    if len(candidates) < max_candidates and "mux_add_factor" in wanted:
        candidates.extend(_proposal_mux_add_factor(
            code, assigns, limit=max_candidates - len(candidates)))
    if len(candidates) < max_candidates and (
        "concat_affine" in wanted
        or "concat_conditional_subtract" in wanted
        or "concat_modular_subtract" in wanted
    ):
        candidates.extend(_proposal_concat_affine(
            code, assigns, limit=max_candidates - len(candidates)))
    if len(candidates) < max_candidates and "output_relation_factor" in wanted:
        candidates.extend(_proposal_output_relation_factor(
            code, assigns, limit=max_candidates - len(candidates)))
    if len(candidates) < max_candidates and "signed_alias_collapse" in wanted:
        candidates.extend(_proposal_signed_alias_collapse(
            code, assigns, widths, limit=max_candidates - len(candidates)))

    out: list[RtlRewriteCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.code in seen or candidate.code == code:
            continue
        seen.add(candidate.code)
        out.append(RtlRewriteCandidate(
            candidate_id=f"R{len(out) + 1}",
            strategy=candidate.strategy,
            reason=candidate.reason,
            code=candidate.code,
            estimated_delta=candidate.estimated_delta,
            details=candidate.details,
        ))
        if len(out) >= max_candidates:
            break
    return out
