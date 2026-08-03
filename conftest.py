"""Shared test fixtures."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent


@pytest.fixture(scope="session", autouse=True)
def database():
    """Make sure the mock database exists before any test runs.

    business.db is generated rather than committed, so a fresh clone (and CI)
    has nothing to query until the setup script has run.
    """
    if not (ROOT / "business.db").exists():
        subprocess.run(
            [sys.executable, str(ROOT / "setup_database.py")],
            check=True,
            cwd=ROOT,
        )


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    """Send audit entries to a temp file so tests never touch the real log."""
    import audit

    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.log")
    return tmp_path / "audit.log"
