"""Tests for :mod:`src.library` — Liberty parsing, Cell logic, and Library API."""

import pytest
from src.library import Library, Cell, _eval_boolean


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def example_lib():
    """Load the bundled example technology library."""
    return Library.from_file("examples/example.lib")


# ---------------------------------------------------------------------------
# _eval_boolean unit tests
# ---------------------------------------------------------------------------

class TestEvalBoolean:
    """Internal boolean expression evaluator."""

    def test_and(self):
        assert _eval_boolean("A * B", {"A": 1, "B": 1}) == 1
        assert _eval_boolean("A * B", {"A": 1, "B": 0}) == 0
        assert _eval_boolean("A & B", {"A": 1, "B": 1}) == 1

    def test_or(self):
        assert _eval_boolean("A + B", {"A": 0, "B": 0}) == 0
        assert _eval_boolean("A + B", {"A": 0, "B": 1}) == 1
        assert _eval_boolean("A | B", {"A": 0, "B": 1}) == 1

    def test_xor(self):
        assert _eval_boolean("A ^ B", {"A": 0, "B": 0}) == 0
        assert _eval_boolean("A ^ B", {"A": 1, "B": 0}) == 1
        assert _eval_boolean("A ^ B", {"A": 1, "B": 1}) == 0

    def test_not_prefix(self):
        assert _eval_boolean("!A", {"A": 0}) == 1
        assert _eval_boolean("!A", {"A": 1}) == 0

    def test_not_postfix(self):
        assert _eval_boolean("A'", {"A": 0}) == 1
        assert _eval_boolean("A'", {"A": 1}) == 0

    def test_precedence_not_binds_tighter_than_and(self):
        # !A * B  should be (!A) * B,  not !(A * B)
        assert _eval_boolean("!A * B", {"A": 0, "B": 1}) == 1  # 1 * 1 = 1
        assert _eval_boolean("!A * B", {"A": 1, "B": 1}) == 0  # 0 * 1 = 0

    def test_precedence_and_binds_tighter_than_or(self):
        # A + B * C  should be  A + (B * C)
        assert _eval_boolean("A + B * C", {"A": 0, "B": 1, "C": 1}) == 1
        assert _eval_boolean("A + B * C", {"A": 0, "B": 0, "C": 1}) == 0

    def test_precedence_or_binds_tighter_than_xor(self):
        # A ^ B + C  should be A ^ (B + C)
        assert _eval_boolean("A ^ B + C", {"A": 0, "B": 0, "C": 0}) == 0
        assert _eval_boolean("A ^ B + C", {"A": 0, "B": 1, "C": 0}) == 1

    def test_parentheses_override(self):
        assert _eval_boolean("(A + B) * C", {"A": 0, "B": 1, "C": 1}) == 1
        assert _eval_boolean("(A + B) * C", {"A": 0, "B": 0, "C": 1}) == 0

    def test_double_negation(self):
        assert _eval_boolean("!A'", {"A": 0}) == 0
        assert _eval_boolean("!!A", {"A": 1}) == 1


# ---------------------------------------------------------------------------
# Library parsing
# ---------------------------------------------------------------------------

class TestLibraryParse:
    """Parsing Liberty-format cell descriptions."""

    def test_from_file_loads_all_cells(self, example_lib):
        assert len(example_lib) == 8
        assert example_lib.name == "example"

    def test_all_cell_names(self, example_lib):
        expected = {"NOT", "BUF", "AND", "NAND", "OR", "NOR", "XOR", "XNOR"}
        assert set(example_lib.cells) == expected

    def test_cell_has_correct_pins(self, example_lib):
        and_cell = example_lib["AND"]
        assert and_cell.name == "AND"
        assert and_cell.inputs == ["A", "B"]
        assert and_cell.output == "Y"

    def test_cell_has_function_string(self, example_lib):
        assert example_lib["AND"].function == "(A * B)"
        assert example_lib["OR"].function == "(A + B)"
        assert example_lib["NOT"].function == "A'"
        assert example_lib["XOR"].function == "(A ^ B)"

    def test_from_string_minimal(self):
        text = """
        library(mini) {
          cell(NOT) {
            pin(A) { direction: input; }
            pin(Y) { direction: output; function: "A'"; }
          }
        }
        """
        lib = Library.from_string(text)
        assert lib.name == "mini"
        assert len(lib) == 1
        assert "NOT" in lib
        assert lib["NOT"].function == "A'"

    def test_contains_and_getitem(self, example_lib):
        assert "AND" in example_lib
        assert "MISSING" not in example_lib
        assert isinstance(example_lib["XOR"], Cell)

    def test_missing_cell_raises_keyerror(self, example_lib):
        with pytest.raises(KeyError):
            example_lib["NONEXISTENT"]

    def test_repr(self, example_lib):
        r = repr(example_lib)
        assert r.startswith("Library(")
        assert "example" in r


# ---------------------------------------------------------------------------
# Cell logic (truth tables)
# ---------------------------------------------------------------------------

class TestCellLogic:
    """Each cell's ``make_logic()`` should match the expected truth table."""

    def _check(self, lib, cell_name, expected_outputs):
        """*expected_outputs* is the result column for inputs in binary-count order."""
        cell = lib[cell_name]
        logic = cell.make_logic()
        results = [logic(a, b) for a, b in [(0,0), (0,1), (1,0), (1,1)]]
        assert results == expected_outputs, f"{cell_name}: {results} != {expected_outputs}"

    def test_and(self, example_lib):   self._check(example_lib, "AND",  [0,0,0,1])
    def test_nand(self, example_lib):  self._check(example_lib, "NAND", [1,1,1,0])
    def test_or(self, example_lib):    self._check(example_lib, "OR",   [0,1,1,1])
    def test_nor(self, example_lib):   self._check(example_lib, "NOR",  [1,0,0,0])
    def test_xor(self, example_lib):   self._check(example_lib, "XOR",  [0,1,1,0])
    def test_xnor(self, example_lib):  self._check(example_lib, "XNOR", [1,0,0,1])

    def test_not_unary(self, example_lib):
        cell = example_lib["NOT"]
        logic = cell.make_logic()
        assert logic(0) == 1
        assert logic(1) == 0

    def test_buf_unary(self, example_lib):
        cell = example_lib["BUF"]
        logic = cell.make_logic()
        assert logic(0) == 0
        assert logic(1) == 1

    def test_make_logic_without_function_raises(self):
        cell = Cell(name="BROKEN")
        with pytest.raises(ValueError, match="no output function"):
            cell.make_logic()

    def test_logic_fn_name(self, example_lib):
        logic = example_lib["AND"].make_logic()
        assert logic.__name__ == "AND_logic"

    def test_logic_fn_with_extra_kwargs_raises(self, example_lib):
        logic = example_lib["AND"].make_logic()
        with pytest.raises(TypeError):
            logic(A=1, B=0)  # positional-only
