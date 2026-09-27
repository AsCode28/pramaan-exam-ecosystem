"""Shared test configuration.

The demo suites exercise failure injection, tamper and reset, all of which are
now gated behind ``DEMO_MODE``. Demo mode is enabled for the whole test
session by default; individual tests that assert the DISABLED behaviour opt out
explicitly with ``monkeypatch.delenv("DEMO_MODE")``.
"""

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest

os.environ.setdefault("DEMO_MODE", "true")


@pytest.fixture(autouse=True)
def _demo_mode_on(monkeypatch):
    """Enable DEMO_MODE for every test unless the test overrides it."""
    if "DEMO_MODE" not in os.environ:
        monkeypatch.setenv("DEMO_MODE", "true")
