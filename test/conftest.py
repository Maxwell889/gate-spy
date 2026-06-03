"""Shared pytest configuration.

Adds a ``--out-dir`` option so test artifacts (notably generated ``.dot``
files) can be collected in a chosen directory for inspection, instead of the
default per-test temporary directory.
"""

from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--out-dir", action="store", default=None, metavar="DIR",
        help="Directory to collect test artifacts (e.g. generated .dot files). "
             "If omitted, a per-test temporary directory is used.",
    )


@pytest.fixture
def out_dir(request, tmp_path) -> Path:
    """Directory for a test's output artifacts.

    Returns ``--out-dir`` (created if needed, shared across tests) when given,
    otherwise the per-test ``tmp_path``.
    """
    chosen = request.config.getoption("--out-dir")
    if chosen:
        d = Path(chosen)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return tmp_path
