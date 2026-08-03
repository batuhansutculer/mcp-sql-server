"""Tests for the MCP tools.

These call the tool functions directly -- no model involved -- so the tests
confirm the boundaries hold in code rather than by prompting.
"""

import json
import sqlite3
from dataclasses import replace

import pytest

import server
from policy import MASK


def call(tool, *args) -> dict:
    """Invoke a tool and parse its JSON response."""
    return json.loads(tool(*args))


class TestListTables:
    def test_lists_tables_and_marks_restricted_ones(self):
        result = call(server.list_tables)
        by_name = {t["name"]: t["accessible"] for t in result["tables"]}

        assert by_name["customers"] is True
        # Restricted tables are still listed. The agent knowing a table exists
        # but is off-limits beats it guessing and burning turns on refusals.
        assert by_name["payment_methods"] is False


class TestDescribeTable:
    def test_returns_columns_and_flags_masked_ones(self):
        result = call(server.describe_table, "customers")
        masked = {c["name"]: c["masked"] for c in result["columns"]}

        assert masked["name"] is False
        assert masked["email"] is True

    def test_restricted_table_is_refused(self):
        assert "restricted" in call(server.describe_table, "payment_methods")["error"]

    def test_unknown_table_returns_an_error_with_a_hint(self):
        result = call(server.describe_table, "nope")
        assert "No table named" in result["error"]
        assert "customers" in result["hint"]

    def test_injected_table_name_is_refused(self):
        """The argument is matched against real tables, not interpolated blind."""
        result = call(server.describe_table, "customers); DROP TABLE orders;--")
        assert "error" in result


class TestRunQuery:
    def test_returns_columns_and_rows(self):
        result = call(server.run_query, "SELECT name, city FROM customers ORDER BY id")

        assert result["columns"] == ["name", "city"]
        assert result["rows"][0] == ["Anna Schmidt", "Berlin"]
        assert result["row_count"] == 4
        assert result["truncated"] is False

    def test_join_across_tables(self):
        result = call(
            server.run_query,
            """
            SELECT c.name, SUM(p.price * o.quantity) AS total
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            JOIN products p ON o.product_id = p.id
            GROUP BY c.name ORDER BY total DESC
            """,
        )
        assert result["rows"][0] == ["Anna Schmidt", 1097.0]

    def test_masked_values_are_hidden_but_rows_still_return(self):
        result = call(server.run_query, "SELECT name, email FROM customers")

        assert all(row[1] == MASK for row in result["rows"])
        assert result["row_count"] == 4

    def test_select_star_masks_too(self):
        """Masking runs on the result set, so `*` is covered.

        The column never appears in the SQL, but the cursor reports its real
        name -- which is why masking is applied to output, not to the query.
        """
        result = call(server.run_query, "SELECT * FROM customers")
        email_index = result["columns"].index("email")
        assert all(row[email_index] == MASK for row in result["rows"])

    def test_restricted_table_is_refused(self):
        assert "restricted" in call(server.run_query, "SELECT * FROM payment_methods")["error"]

    def test_write_is_refused(self):
        assert "error" in call(server.run_query, "DROP TABLE orders")

    def test_bad_sql_returns_an_error_rather_than_raising(self):
        """A model writing a wrong table name is normal, not exceptional.

        The tool has to hand back something the model can recover from, and
        must not leak a connection doing it.
        """
        result = call(server.run_query, "SELECT * FROM nonexistent_table")
        assert "Query failed" in result["error"]
        assert "describe_table" in result["hint"]

    def test_results_are_capped(self, monkeypatch):
        # Policy is frozen, so swap in a replacement rather than mutating it.
        monkeypatch.setattr(
            server, "POLICY", replace(server.POLICY, max_rows=2)
        )
        result = call(server.run_query, "SELECT * FROM customers")

        assert result["row_count"] == 2
        assert result["truncated"] is True


class TestReadOnlyConnection:
    """The second boundary, independent of the policy layer."""

    def test_connection_rejects_writes(self):
        """Even bypassing the policy entirely, the database refuses to change.

        This is the defence-in-depth claim, tested rather than asserted.
        """
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection = server._connect()
            try:
                connection.execute("DELETE FROM orders")
            finally:
                connection.close()

    def test_orders_table_is_intact_afterwards(self):
        assert call(server.run_query, "SELECT COUNT(*) FROM orders")["rows"][0][0] == 5


class TestAuditLog:
    def test_refusals_are_recorded_with_a_reason(self, isolated_audit_log):
        call(server.run_query, "SELECT * FROM payment_methods")
        entry = json.loads(isolated_audit_log.read_text(encoding="utf-8").strip())

        assert entry["decision"] == "denied"
        assert entry["tool"] == "run_query"
        assert "restricted" in entry["reason"]
        assert entry["sql"] == "SELECT * FROM payment_methods"

    def test_successful_queries_are_recorded_with_a_row_count(self, isolated_audit_log):
        call(server.run_query, "SELECT * FROM customers")
        entry = json.loads(isolated_audit_log.read_text(encoding="utf-8").strip())

        assert entry["decision"] == "allowed"
        assert entry["row_count"] == 4

    def test_entries_are_one_json_object_per_line(self, isolated_audit_log):
        call(server.list_tables)
        call(server.run_query, "SELECT 1")
        lines = isolated_audit_log.read_text(encoding="utf-8").strip().splitlines()

        assert len(lines) == 2
        assert all(json.loads(line)["timestamp"] for line in lines)
