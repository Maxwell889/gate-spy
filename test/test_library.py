"""Tests for :mod:`src.library` — Liberty parsing and Cell logic."""

import pytest

from src.library import Library, Cell, _eval_boolean


@pytest.fixture
def lib():
    return Library.from_file("examples/example.lib")


def test_eval_boolean():
    assert _eval_boolean("A * B", {"A": 1, "B": 1}) == 1
    assert _eval_boolean("A + B", {"A": 0, "B": 1}) == 1
    assert _eval_boolean("A ^ B", {"A": 1, "B": 1}) == 0
    assert _eval_boolean("!A", {"A": 0}) == 1 and _eval_boolean("A'", {"A": 1}) == 0
    # precedence NOT > AND > OR > XOR, with parentheses overriding
    assert _eval_boolean("!A * B", {"A": 0, "B": 1}) == 1
    assert _eval_boolean("A + B * C", {"A": 0, "B": 0, "C": 1}) == 0
    assert _eval_boolean("A ^ B + C", {"A": 0, "B": 1, "C": 0}) == 1
    assert _eval_boolean("(A + B) * C", {"A": 0, "B": 0, "C": 1}) == 0
    assert _eval_boolean("!!A", {"A": 1}) == 1


def test_library_parse(lib):
    assert lib.name == "example" and len(lib) == 8
    assert set(lib.cells) == {"NOT", "BUF", "AND", "NAND", "OR", "NOR", "XOR", "XNOR"}
    assert lib["AND"].inputs == ["A", "B"] and lib["AND"].output == "Y"
    assert lib["AND"].function == "(A * B)" and lib["NOT"].function == "A'"
    assert "AND" in lib and "MISSING" not in lib
    with pytest.raises(KeyError):
        lib["NONEXISTENT"]


def test_from_string_minimal():
    text = """library(mini) {
      cell(NOT) { pin(A) { direction: input; }
                  pin(Y) { direction: output; function: "A'"; } } }"""
    lib = Library.from_string(text)
    assert lib.name == "mini" and len(lib) == 1 and lib["NOT"].function == "A'"


@pytest.mark.parametrize("gate, tt", [
    ("AND", [0, 0, 0, 1]), ("NAND", [1, 1, 1, 0]), ("OR", [0, 1, 1, 1]),
    ("NOR", [1, 0, 0, 0]), ("XOR", [0, 1, 1, 0]), ("XNOR", [1, 0, 0, 1]),
])
def test_binary_gate_logic(lib, gate, tt):
    logic = lib[gate].make_logic()
    assert [logic(a, b) for a, b in [(0, 0), (0, 1), (1, 0), (1, 1)]] == tt


def test_unary_logic_and_errors(lib):
    assert lib["NOT"].make_logic()(0) == 1 and lib["BUF"].make_logic()(1) == 1
    assert lib["AND"].make_logic().__name__ == "AND_logic"
    with pytest.raises(ValueError, match="no output function"):
        Cell(name="BROKEN").make_logic()
    with pytest.raises(TypeError):
        lib["AND"].make_logic()(A=1, B=0)  # positional-only
