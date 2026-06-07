#!/usr/bin/env python3
"""
Calculate Verilog RTL cost according to ICCAD 2022 Problem A evaluation criteria.

Based on: "2022 CAD Contest Problem A: Learning Arithmetic Operations from
Gate-Level Circuit" (Chou et al., ICCAD 2022).

Cost rules:
  TABLE I  — Verilog operators:
    {} {{}}    Concatenation / replication    1 per symbol
    [] [:]     Bit-select / part-select       1
    + - * / ** Arithmetic                     1
    %          Modulus                        1
    > >= < <=  Relational                     1
    !          Logical negation               1
    &&         Logical and                    1
    ||         Logical or                     1
    == != === !==  Equality                   1
    ~          Bit-wise negation              1 per bit
    & | ^ ^~ ~^   Bit-wise (per bit) / Reduction (flat 1)
    << >> <<< >>>  Shifts                     1
    ?:         Conditional                    1

  TABLE II — Verilog keywords:
    if           1
    case/casex/casez  2 per item (default excluded)
    primitive gate instantiation  1
    submodule instantiation  module_cost of submodule
    (most other keywords cost 0)

Usage:
    python scripts/calc_verilog_cost.py <verilog_file>
    python scripts/calc_verilog_cost.py <verilog_file> --debug
"""

import re
import sys
import os
import argparse
from typing import Dict, List, Optional, Tuple


# =========================================================================
#  Comment stripping
# =========================================================================

def strip_comments(text: str) -> str:
    """Remove /* */ block comments and // line comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
    return text


# =========================================================================
#  Width extraction from declarations
# =========================================================================

def extract_widths(module_body: str) -> Dict[str, int]:
    """
    Parse input / output / wire / reg / integer declarations and return
    {signal_name: bit_width}.  Scalars default to 1.
    """
    widths: Dict[str, int] = {}

    decl_re = re.compile(
        r"""
        (?:input|output|inout|wire|reg|integer)\b
        \s+
        (?:signed\s+)?
        (?:\[(\d+)\s*:\s*(\d+)\])?
        \s*
        ((?:\w+\s*,\s*)*\w+)
        \s*;
        """,
        re.VERBOSE,
    )

    for m in decl_re.finditer(module_body):
        msb_s, lsb_s = m.group(1), m.group(2)
        names = m.group(3)

        w = abs(int(msb_s) - int(lsb_s)) + 1 if (msb_s and lsb_s) else 1

        for name in re.findall(r"\w+", names):
            if name not in widths:
                widths[name] = w

    return widths


# =========================================================================
#  Module extraction
# =========================================================================

def extract_modules(text: str) -> Tuple[Dict[str, str], Optional[str]]:
    """
    Return ({module_name: body_text}, top_module_name).
    *body_text* is the text between the 'module ... ;' header and 'endmodule'.
    """
    modules: Dict[str, str] = {}
    top_module: Optional[str] = None

    pat = re.compile(
        r"module\s+(\w+)\s*(?:\(.*?\))?\s*;(.*?)endmodule",
        re.DOTALL,
    )

    for m in pat.finditer(text):
        name = m.group(1)
        body = m.group(2)
        modules[name] = body
        if top_module is None:
            top_module = name

    return modules, top_module


# =========================================================================
#  Helper: split "lhs = rhs" or "lhs <= rhs" safely
# =========================================================================

def split_assignment(stmt: str) -> Tuple[str, str, Optional[str]]:
    """
    Split a Verilog assignment statement into (lhs, rhs, operator).

    Returns ('', stmt, None) if no assignment operator is found
    (e.g. the statement is a bare expression).
    """
    depth = 0
    i = 0
    while i < len(stmt):
        ch = stmt[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            if ch == "<" and i + 1 < len(stmt) and stmt[i + 1] == "=":
                return stmt[:i].strip(), stmt[i + 2:].strip(), "<="
            if ch == "=":
                # must not be part of ==, <=, >=, !=, ===, !==
                if i + 1 < len(stmt) and stmt[i + 1] == "=":
                    i += 1  # skip ==
                elif i > 0 and stmt[i - 1] in "<>=!":
                    pass  # <=, >=, !=, === (already handled)
                else:
                    return stmt[:i].strip(), stmt[i + 1:].strip(), "="
        i += 1

    return "", stmt, None


# =========================================================================
#  Expression tokeniser & parser
# =========================================================================

_TOKEN_SPEC = [
    ("REPL_START",  r"\{(\d+)\{"),             # {n{
    ("LBRACE",      r"\{(?!\d+\{)"),             # {  (concatenation open)
    ("RBRACE",      r"\}"),                      # }
    ("LBRACKET",    r"\["),                      # [
    ("RBRACKET",    r"\]"),                      # ]
    ("XNOR",        r"\^~|~\^"),                 # ^~  ~^
    ("LSHIFT_A",    r"<<<"),                     # <<<
    ("RSHIFT_A",    r">>>"),                     # >>>
    ("LSHIFT",      r"<<"),                      # <<
    ("RSHIFT",      r">>"),                      # >>
    ("LE",          r"<="),                      # <=
    ("GE",          r">="),                      # >=
    ("EQ",          r"==="),                     # ===
    ("NEQ_CASE",    r"!=="),                     # !==
    ("EQ2",         r"=="),                      # ==
    ("NEQ",         r"!="),                      # !=
    ("LAND",        r"&&"),                      # &&
    ("LOR",         r"\|\|"),                    # ||
    ("LT",          r"<"),                       # <
    ("GT",          r">"),                       # >
    ("PLUS",        r"\+"),                      # +
    ("MINUS",       r"-"),                       # -
    ("STARSTAR",    r"\*\*"),                    # **
    ("STAR",        r"\*"),                      # *
    ("SLASH",       r"/"),                       # /
    ("PERCENT",     r"%"),                       # %
    ("BANG",        r"!"),                       # !
    ("TILDE",       r"~"),                       # ~
    ("AMP",         r"&"),                       # &
    ("PIPE",        r"\|"),                      # |
    ("CARET",       r"\^"),                      # ^
    ("QUESTION",    r"\?"),                      # ?
    ("COLON",       r":"),                       # :
    ("COMMA",       r","),                      # ,
    ("LPAREN",      r"\("),                      # (
    ("RPAREN",      r"\)"),                      # )
    ("SIZED_NUM",   r"\b\d+'[bBdDhHoO]\w+\b"),     # 8'b0001, 26'd123
    ("NUMBER",      r"\b\d+\b"),                 # 123
    ("SYSTEM_FCALL", r"\$[a-zA-Z_]\w*"),         # $signed, $unsigned, ...
    ("IDENT",       r"\b[a-zA-Z_]\w*\b"),        # identifier
    ("WS",          r"\s+"),                     # whitespace
    ("UNKNOWN",     r"."),                       # catch-all
]

_TOKEN_RE = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC)
)


def tokenize(expr: str) -> List[Tuple[str, str]]:
    """Tokenize a Verilog expression, skipping whitespace."""
    tokens = []
    for m in _TOKEN_RE.finditer(expr):
        kind = m.lastgroup
        value = m.group()
        if kind == "WS":
            continue
        tokens.append((kind, value))
    return tokens


class ExprAnalyser:
    """Precedence-climbing expression parser that accumulates operator costs."""

    def __init__(self, widths: Dict[str, int]):
        self.widths = widths
        self.tokens: List[Tuple[str, str]] = []
        self.pos = 0

    # ---- public API -----------------------------------------------------

    def analyse(self, expr_text: str) -> int:
        """Return the total operator cost of *expr_text*."""
        if not expr_text.strip():
            return 0
        self.tokens = tokenize(expr_text)
        self.pos = 0
        cost, _ = self._parse_expr(0)
        return cost

    def count_selects(self, text: str) -> int:
        """Count only bit-select / part-select operators ([], [:]) in *text*."""
        if not text.strip():
            return 0
        self.tokens = tokenize(text)
        self.pos = 0
        return self._count_selects_in_tokens()

    def _count_selects_in_tokens(self) -> int:
        """Walk tokens and count LBRACKET...RBRACKET groups."""
        cost = 0
        i = 0
        while i < len(self.tokens):
            kind = self.tokens[i][0]
            if kind == "LBRACKET":
                cost += 1  # each [...] costs 1 regardless of bit or part
                # skip to matching RBRACKET
                depth = 1
                i += 1
                while i < len(self.tokens) and depth > 0:
                    k = self.tokens[i][0]
                    if k == "LBRACKET":
                        depth += 1
                    elif k == "RBRACKET":
                        depth -= 1
                    i += 1
                continue
            i += 1
        return cost

    # ---- token helpers --------------------------------------------------

    def _peek(self) -> Optional[Tuple[str, str]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> Optional[Tuple[str, str]]:
        t = self._peek()
        if t:
            self.pos += 1
        return t

    def _expect(self, kind: str) -> Tuple[str, str]:
        t = self._next()
        if t is None:
            raise ValueError(f"Expected {kind}, got EOF")
        if t[0] != kind:
            raise ValueError(f"Expected {kind}, got {t}")
        return t

    # ---- precedence table -----------------------------------------------

    @staticmethod
    def _prec(kind: str) -> int:
        return {
            "LOR": 1, "LAND": 2,
            "PIPE": 3, "CARET": 4, "XNOR": 4,
            "AMP": 5,
            "EQ2": 6, "NEQ": 6, "EQ": 6, "NEQ_CASE": 6,
            "LT": 7, "GT": 7, "LE": 7, "GE": 7,
            "LSHIFT": 8, "RSHIFT": 8, "LSHIFT_A": 8, "RSHIFT_A": 8,
            "PLUS": 9, "MINUS": 9,
            "STAR": 10, "SLASH": 10, "PERCENT": 10, "STARSTAR": 11,
        }.get(kind, 0)

    # ---- main parser ----------------------------------------------------

    def _parse_expr(self, min_prec: int) -> Tuple[int, int]:
        """Parse expression, return (cost, bit_width)."""
        cost, width = self._parse_unary()

        while True:
            t = self._peek()
            if t is None:
                break

            kind = t[0]

            # Conditional  ? ... :
            if kind == "QUESTION":
                self._next()
                true_c, _ = self._parse_expr(0)
                self._expect("COLON")
                false_c, false_w = self._parse_expr(0)
                cost += true_c + false_c + 1
                width = false_w
                continue

            if kind == "COLON":
                break  # end of a conditional branch

            prec = self._prec(kind)
            if prec <= min_prec:
                break

            # Binary operators  → consume RHS and add cost
            if kind in ("PLUS", "MINUS", "STAR", "SLASH", "PERCENT", "STARSTAR"):
                self._next()
                rhs_c, rhs_w = self._parse_unary()
                cost += rhs_c + 1
                width = max(width, rhs_w)
                continue

            if kind in ("EQ2", "NEQ", "EQ", "NEQ_CASE"):
                self._next()
                rhs_c, _ = self._parse_unary()
                cost += rhs_c + 1
                width = 1
                continue

            if kind in ("LT", "GT", "LE", "GE"):
                self._next()
                rhs_c, _ = self._parse_unary()
                cost += rhs_c + 1
                width = 1
                continue

            if kind in ("LAND", "LOR"):
                self._next()
                rhs_c, _ = self._parse_unary()
                cost += rhs_c + 1
                width = 1
                continue

            if kind in ("LSHIFT", "RSHIFT", "LSHIFT_A", "RSHIFT_A"):
                self._next()
                rhs_c, _ = self._parse_unary()
                cost += rhs_c + 1
                # width stays the same (LHS width)
                continue

            # Bit-wise binary: & | ^ ^~ ~^   (per bit)
            if kind in ("AMP", "PIPE", "CARET", "XNOR"):
                self._next()
                rhs_c, rhs_w = self._parse_unary()
                bw = max(width, rhs_w)
                cost += rhs_c + bw
                width = bw
                continue

            break

        return cost, width

    def _parse_unary(self) -> Tuple[int, int]:
        """Parse unary operators + primary."""
        t = self._peek()
        if t is None:
            return 0, 1

        kind, _value = t

        # Unary + / -
        if kind in ("PLUS", "MINUS"):
            self._next()
            nxt = self._peek()
            on_literal = nxt and nxt[0] in ("NUMBER", "SIZED_NUM")
            c, w = self._parse_unary()
            return c + (0 if on_literal else 1), w

        # Bit-wise negation  ~
        if kind == "TILDE":
            self._next()
            # Check for ~&, ~|, ~^  (reduction NAND/NOR/XNOR)
            nxt = self._peek()
            if nxt and nxt[0] in ("AMP", "PIPE", "CARET"):
                self._next()  # consume & | ^
                c, w = self._parse_unary()
                return c + 1, w  # reduction → flat 1
            else:
                c, w = self._parse_unary()
                return c + w, w  # bit-wise NOT → 1 per bit

        # Logical negation  !
        if kind == "BANG":
            self._next()
            c, _ = self._parse_unary()
            return c + 1, 1

        # Reduction:  &  |  ^  ^~  ~^   (unary prefix)
        if kind in ("AMP", "PIPE", "CARET", "XNOR"):
            if self._in_unary_context():
                self._next()
                c, w = self._parse_unary()
                return c + 1, w  # reduction → flat 1
            else:
                return self._parse_primary()

        return self._parse_primary()

    def _in_unary_context(self) -> bool:
        """Return True if current position expects a unary (reduction) operator."""
        if self.pos == 0:
            return True
        # Find previous non-WS token
        for i in range(self.pos - 1, -1, -1):
            prev = self.tokens[i][0]
            return prev in (
                "LPAREN", "LBRACKET", "LBRACE", "REPL_START",
                "COMMA", "QUESTION", "COLON",
                "PLUS", "MINUS", "STAR", "SLASH", "PERCENT",
                "TILDE", "BANG", "AMP", "PIPE", "CARET", "XNOR",
                "LAND", "LOR", "EQ2", "NEQ", "EQ", "NEQ_CASE",
                "LT", "GT", "LE", "GE",
                "LSHIFT", "RSHIFT", "LSHIFT_A", "RSHIFT_A",
                "STARSTAR",
            )
        return True

    def _parse_primary(self) -> Tuple[int, int]:
        """Parse a primary: ident[sel], number, (expr), {concat}, {n{repl}}."""
        t = self._peek()
        if t is None:
            return 0, 1

        kind, value = t

        if kind == "LPAREN":
            self._next()
            c, w = self._parse_expr(0)
            self._expect("RPAREN")
            return c, w

        if kind == "LBRACE":
            return self._parse_concat()

        if kind == "REPL_START":
            return self._parse_replication()

        if kind == "IDENT":
            self._next()
            name = value
            width = self.widths.get(name, 1)
            cost = 0

            # Optional bit/part select: ident[ ... ]
            if self._peek() and self._peek()[0] == "LBRACKET":
                sel_c, sel_w = self._parse_select()
                cost += sel_c
                width = sel_w

            return cost, width

        if kind == "SYSTEM_FCALL":
            self._next()  # consume $func
            cost = 0
            width = 1
            # Parse (args) if present
            if self._peek() and self._peek()[0] == "LPAREN":
                self._next()  # consume (
                while True:
                    nxt = self._peek()
                    if nxt is None or nxt[0] == "RPAREN":
                        break
                    c, w = self._parse_expr(0)
                    cost += c
                    width = max(width, w)
                    nxt = self._peek()
                    if nxt and nxt[0] == "COMMA":
                        self._next()
                    elif nxt and nxt[0] == "RPAREN":
                        break
                self._expect("RPAREN")
            return cost, width

        if kind in ("NUMBER", "SIZED_NUM"):
            self._next()
            w = 1
            if kind == "SIZED_NUM":
                m = re.match(r"(\d+)'[bBdDhHoO]", value)
                if m:
                    w = int(m.group(1))
            return 0, w

        # Unknown token — skip
        self._next()
        return 0, 1

    def _parse_select(self) -> Tuple[int, int]:
        """Parse [expr] or [expr:expr], cost 1."""
        self._expect("LBRACKET")
        c1, _ = self._parse_expr(0)

        if self._peek() and self._peek()[0] == "COLON":
            self._next()
            c2, w2 = self._parse_expr(0)
            self._expect("RBRACKET")
            return c1 + c2 + 1, w2
        else:
            self._expect("RBRACKET")
            return c1 + 1, 1

    def _parse_concat(self) -> Tuple[int, int]:
        """Parse {a, b, c, ...} — concatenation. Cost = number of symbols."""
        self._expect("LBRACE")
        total_cost = 0
        total_width = 0
        symbol_count = 0

        while True:
            nxt = self._peek()
            if nxt is None or nxt[0] == "RBRACE":
                break

            c, w = self._parse_expr(0)
            total_cost += c
            total_width += w
            symbol_count += 1

            nxt = self._peek()
            if nxt and nxt[0] == "COMMA":
                self._next()
            elif nxt and nxt[0] == "RBRACE":
                break

        self._expect("RBRACE")
        total_cost += symbol_count  # 1 per symbol
        return total_cost, total_width

    def _parse_replication(self) -> Tuple[int, int]:
        """Parse {n{...}} — replication. Cost = 1 per symbol in inner expression."""
        t = self._next()  # REPL_START  e.g. "{3{"
        m = re.match(r"\{(\d+)\{", t[1])
        got = t[1]
        assert m, "Expected {n{, got %s" % got
        repeat = int(m.group(1))

        total_cost = 0
        inner_width = 0
        symbol_count = 0

        while True:
            nxt = self._peek()
            if nxt is None:
                break
            if nxt[0] == "RBRACE":
                break

            c, w = self._parse_expr(0)
            total_cost += c
            inner_width += w
            symbol_count += 1

            nxt = self._peek()
            if nxt and nxt[0] == "COMMA":
                self._next()
            elif nxt and nxt[0] == "RBRACE":
                break

        # Consume }} (two RBRACE tokens) or } (one RBRACE)
        self._expect("RBRACE")
        if self._peek() and self._peek()[0] == "RBRACE":
            self._next()

        total_cost += symbol_count  # 1 per symbol
        return total_cost, inner_width * repeat


# =========================================================================
#  Module cost analyser
# =========================================================================

# Verilog primitive gate keywords (TABLE II)
_PRIMITIVES = {"and", "nand", "nor", "not", "buf", "or", "xor", "xnor"}

# Verilog keywords that are NOT submodule names
_RESERVED = (
    _PRIMITIVES
    | {
        "module", "endmodule", "input", "output", "inout",
        "wire", "reg", "integer", "assign", "always",
        "if", "else", "begin", "end", "case", "casex", "casez",
        "default", "endcase", "for", "signed", "unsigned",
        "parameter", "function", "endfunction", "task", "endtask",
        "generate", "endgenerate", "genvar", "posedge", "negedge",
    }
)


class ModuleCostAnalyser:
    """Analyse a single Verilog module body and compute its operator / keyword cost."""

    def __init__(self, module_body: str, all_module_names: set):
        """
        Parameters
        ----------
        module_body : str
            The text of the module body (between 'module ... ;' and 'endmodule').
        all_module_names : set of str
            Names of all modules defined in the file, for submodule detection.
        """
        self.body = module_body
        self.all_module_names = all_module_names
        self.widths = extract_widths(module_body)
        self.cost = 0

    # ---- top-level entry point ------------------------------------------

    def calculate(self) -> int:
        self.cost = 0

        # We process the body in a specific order to avoid double-counting.
        # Each step removes what it consumed from a working copy.
        working = self.body

        # 1. Continuous assignments
        working = self._process_assigns(working)

        # 1.5 Net declarations with initializers (wire/reg name = expr;)
        working = self._process_net_decl_assigns(working)

        # 2. Always blocks
        working = self._process_always_blocks(working)

        # 3. Case / casex / casez outside always blocks
        working = self._process_case_outside_always(working)

        # 4. Primitive gate instantiations
        working = self._process_primitives(working)

        # 5. Submodule instantiations
        self._process_submodules(working)

        # 6. if keywords outside always blocks
        self.cost += len(re.findall(r"\bif\b", working))

        return self.cost

    # ---- continuous assignments -----------------------------------------

    def _process_assigns(self, text: str) -> str:
        """
        Process 'assign lhs = rhs;' statements.
        Returns text with assign statements removed.
        """
        assign_re = re.compile(r"assign\s+([^;]*?);", re.DOTALL)

        def _handle(m):
            stmt = m.group(1)
            lhs, rhs, _ = split_assignment(stmt)
            analyser = ExprAnalyser(self.widths)
            c = analyser.count_selects(lhs)
            c += analyser.analyse(rhs)
            self.cost += c
            return ""  # remove from working text

        return assign_re.sub(_handle, text)

    # ---- net declarations with initializers -------------------------------

    @staticmethod
    def _split_top_level_commas(text: str) -> list:
        """Split *text* on commas that are not inside (), [], or {}."""
        parts = []
        depth = 0
        start = 0
        for i, ch in enumerate(text):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(text[start:i].strip())
                start = i + 1
        parts.append(text[start:].strip())
        return parts

    def _process_net_decl_assigns(self, text: str) -> str:
        """Process ``wire/reg name = expr;`` declarations with initializers.

        These are functionally equivalent to ``wire name; assign name = expr;``
        but the cost script's assign-handler misses them because they don't
        start with ``assign``.
        """
        # Match:  wire/reg/integer [signed] [range] body ;
        # Use [^;]+ to capture the full declaration body including commas
        # inside concatenation / function-call brackets.
        decl_re = re.compile(
            r"(?:wire|reg|integer)\s+(?:signed\s+)?(?:\[.*?\]\s*)?\s*([^;]+)\s*;",
            re.DOTALL,
        )

        def _handle(m):
            body = m.group(1).strip()
            # Only process initializer declarations (those containing '=')
            if "=" not in body:
                return m.group(0)  # keep plain wire/reg declarations unchanged

            # Split on top-level commas (between multiple init declarations)
            for part in self._split_top_level_commas(body):
                lhs, rhs, op = split_assignment(part.strip())
                if op is not None and rhs:
                    analyser = ExprAnalyser(self.widths)
                    self.cost += analyser.count_selects(lhs)
                    self.cost += analyser.analyse(rhs)
            return ""

        return decl_re.sub(_handle, text)

    # ---- always blocks --------------------------------------------------

    def _process_always_blocks(self, text: str) -> str:
        """
        Process 'always @(...) begin ... end' blocks.
        Returns text with always blocks removed.
        """
        always_re = re.compile(
            r"always\s*@\s*\([^)]*\)\s*(.*?)end\b",
            re.DOTALL,
        )

        def _handle(m):
            block = m.group(1)
            # Strip optional leading 'begin'
            block = re.sub(r"^\s*begin\b", "", block, count=1)
            self._process_procedural(block)
            return ""

        return always_re.sub(_handle, text)

    def _process_procedural(self, block: str) -> None:
        """Process the body of an always block."""
        working = block

        # --- if keywords (1 each) ---
        if_count = len(re.findall(r"\bif\b", working))
        self.cost += if_count

        # --- case / casex / casez ---
        for case_kw in ("casex", "casez", "case"):
            case_re = re.compile(
                rf"\b{case_kw}\b\s*\(([^)]*)\)\s*(.*?)endcase\b",
                re.DOTALL,
            )

            def _handle_case(m):
                cond = m.group(1)
                items_body = m.group(2)
                # Count operators in case condition
                analyser = ExprAnalyser(self.widths)
                self.cost += analyser.analyse(cond)
                # Count case items (2 per non-default item)
                self._count_case_items(items_body)
                # Recurse into case item statements
                self._process_procedural(items_body)
                return ""

            working = case_re.sub(_handle_case, working)

        # --- parse if conditions for operators ---
        for m in re.finditer(r"if\s*\(\s*([^)]+?)\s*\)", working, re.DOTALL):
            analyser = ExprAnalyser(self.widths)
            self.cost += analyser.analyse(m.group(1))

        # --- operators in assignment RHS ---
        # Scan character-by-character for <= or = at top depth level,
        # extract the RHS until the next top-level ';'.
        # This automatically handles assignments inside if/else/case/default.
        self._scan_assignments(working)

    def _scan_assignments(self, text: str) -> None:
        """
        Find every assignment operator (<= or =) at brace-depth 0 in *text*,
        extract the RHS, and count its operators.
        """
        i = 0
        depth = 0
        n = len(text)

        while i < n:
            ch = text[i]

            if ch in "([{":
                depth += 1
                i += 1
                continue
            if ch in ")]}":
                depth = max(0, depth - 1)
                i += 1
                continue

            if depth == 0:
                # Non-blocking assignment:  <=
                if ch == "<" and i + 1 < n and text[i + 1] == "=":
                    i = self._extract_and_analyse_rhs(text, i + 2)
                    continue

                # Blocking assignment:  =  (not ==, !=, <=, >=, ===, !==)
                if ch == "=":
                    # Skip if part of ==, === (next char is =)
                    if i + 1 < n and text[i + 1] == "=":
                        i += 2
                        continue
                    # Skip if part of <=, >=, != (previous char)
                    if i > 0 and text[i - 1] in "<>=!":
                        i += 1
                        continue
                    i = self._extract_and_analyse_rhs(text, i + 1)
                    continue

            i += 1

    def _extract_and_analyse_rhs(self, text: str, start: int) -> int:
        """
        Starting at *start* (just after = or <=), find the end of the RHS
        (next top-level ';'), then parse and count its operators.
        Returns the index after the ';' (or end of text).
        """
        end = start
        depth = 0
        n = len(text)
        while end < n:
            ch = text[end]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif ch == ";" and depth == 0:
                break
            end += 1

        rhs = text[start:end].strip()
        if rhs:
            analyser = ExprAnalyser(self.widths)
            self.cost += analyser.analyse(rhs)

        return end + 1 if end < n else n

    def _count_case_items(self, case_body: str) -> None:
        """
        Count case items in *case_body* (2 per non-default item).

        A case item is:  value, value, ... : statement
        The 'default' item is excluded.
        """
        # Strategy: find colons that separate case items from their statements.
        # We split on ':' that are at the "top level" (not inside parens/braces).
        depth = 0
        items = []
        current = ""
        for ch in case_body:
            if ch in "([{":
                depth += 1
                current += ch
            elif ch in ")]}":
                depth -= 1
                current += ch
            elif ch == ":" and depth == 0:
                items.append(current.strip())
                current = ""
            else:
                current += ch

        # The first "item" is the value list before the first ':'
        # Each colon introduces a new item's value list (for subsequent items)
        non_default = [it for it in items if "default" not in it.lower()]
        self.cost += len(non_default) * 2

    # ---- case outside always --------------------------------------------

    def _process_case_outside_always(self, text: str) -> str:
        """Handle case/casex/casez blocks that appear outside always blocks."""
        for case_kw in ("casex", "casez", "case"):
            case_re = re.compile(
                rf"\b{case_kw}\b\s*\(([^)]*)\)\s*(.*?)endcase\b",
                re.DOTALL,
            )

            def _handle(m):
                cond = m.group(1)
                items_body = m.group(2)
                analyser = ExprAnalyser(self.widths)
                self.cost += analyser.analyse(cond)
                self._count_case_items(items_body)
                return ""

            text = case_re.sub(_handle, text)

        return text

    # ---- primitive gate instantiations ----------------------------------

    def _process_primitives(self, text: str) -> str:
        """
        Count primitive gate instantiations (1 each).

        Pattern:  prim_type  instance_name  ( ... ) ;
        """
        for prim in sorted(_PRIMITIVES, key=len, reverse=True):
            # Match: prim_type identifier ( ... ) ;
            inst_re = re.compile(
                rf"\b{prim}\s+(\w+)\s*\(.*?\)\s*;",
                re.DOTALL,
            )
            count = len(inst_re.findall(text))
            self.cost += count

        return text

    # ---- submodule instantiations ---------------------------------------

    def _process_submodules(self, text: str) -> None:
        """
        Count submodule instantiations.

        Pattern:  module_name  instance_name  ( ... ) ;
        where module_name is one of the known module names (not a primitive or keyword).
        """
        inst_re = re.compile(
            r"\b(\w+)\s+(\w+)\s*\((.*?)\)\s*;",
            re.DOTALL,
        )
        for m in inst_re.finditer(text):
            sub_name = m.group(1)
            if sub_name in _RESERVED:
                continue
            if sub_name not in self.all_module_names:
                continue
            # Each instantiation of sub_name will add its module_cost;
            # we defer the multiplication to the caller.
            # For now we record it via a special counter.
            self.cost += 0  # placeholder — handled by caller (see calc_cost)
            # Actually we can't know the cost here.
            # The caller (calc_verilog_cost) will handle this.

        # We need a different approach: return the list of submodule references
        # and let the caller multiply.  Store them for later.
        self._sub_instantiations: List[str] = []
        for m in inst_re.finditer(text):
            sub_name = m.group(1)
            if sub_name in _RESERVED:
                continue
            if sub_name in self.all_module_names:
                self._sub_instantiations.append(sub_name)


# =========================================================================
#  Top-level cost calculator
# =========================================================================

def calc_verilog_cost(filepath: str, debug: bool = False) -> int:
    """
    Calculate ICCAD 2022 Problem A cost for a Verilog RTL file.

    Returns
    -------
    int
        Cost of the top-level module.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read()

    text = strip_comments(raw)
    modules, top_module = extract_modules(text)

    if top_module is None:
        print("Error: no module found.", file=sys.stderr)
        return 0

    all_names = set(modules.keys())

    # Compute module costs (bottom-up via recursion with memoisation).
    module_costs: Dict[str, int] = {}
    _stack: List[str] = []

    def calc_one(name: str) -> int:
        if name in module_costs:
            return module_costs[name]
        if name in _stack:
            print(f"Warning: circular dependency involving '{name}'", file=sys.stderr)
            return 0
        _stack.append(name)

        body = modules.get(name, "")
        if not body:
            module_costs[name] = 0
            _stack.pop()
            return 0

        analyser = ModuleCostAnalyser(body, all_names)
        base_cost = analyser.calculate()

        # Add submodule instantiation costs
        sub_cost = 0
        for sub_name in analyser._sub_instantiations:
            sub_cost += calc_one(sub_name)

        total = base_cost + sub_cost
        module_costs[name] = total
        _stack.pop()
        return total

    result = calc_one(top_module)

    if debug:
        print(f"Module costs:", file=sys.stderr)
        for name in sorted(module_costs):
            marker = " <-- top" if name == top_module else ""
            print(f"  {name}: {module_costs[name]}{marker}", file=sys.stderr)
        print(f"Total cost: {result}", file=sys.stderr)

    return result


# =========================================================================
#  CLI
# =========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Calculate Verilog RTL cost per ICCAD 2022 Problem A."
    )
    parser.add_argument(
        "verilog_file",
        help="Path to the Verilog RTL file to evaluate.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print per-module cost breakdown to stderr.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.verilog_file):
        print(f"Error: file not found: {args.verilog_file}", file=sys.stderr)
        sys.exit(1)

    cost = calc_verilog_cost(args.verilog_file, debug=args.debug)
    print(cost)


if __name__ == "__main__":
    main()
