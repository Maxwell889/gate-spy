"""Test fixtures."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def out_dir():
    with tempfile.TemporaryDirectory(prefix="gate-spy-test-") as d:
        yield Path(d)
