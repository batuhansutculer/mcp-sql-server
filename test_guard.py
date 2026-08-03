"""Tests for the run_query guardrail.

These call the tool function directly — no model involved — to confirm the
restrictions are enforced in server code rather than by prompting.

Run with `uv run test_guard.py`, or under pytest.
"""

from server import run_query


def test_restricted_table_is_blocked():
    """Queries touching payment_methods are refused before reaching the database."""
    result = run_query("SELECT * FROM payment_methods")
    assert result == "Error: access to payment data is restricted.", result


def test_non_select_is_blocked():
    """Destructive statements are refused: the tool is read-only by design."""
    result = run_query("DROP TABLE orders")
    assert result == "Error: only read-only SELECT queries are allowed.", result


def test_permitted_select_returns_rows():
    """An ordinary read still works — the guardrail blocks, it doesn't break the tool."""
    result = run_query("SELECT name FROM customers")
    assert "Anna Schmidt" in result, result


TESTS = [
    test_restricted_table_is_blocked,
    test_non_select_is_blocked,
    test_permitted_select_returns_rows,
]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"PASS  {test.__name__}")
    print(f"\nAll {len(TESTS)} guardrail tests passed.")
