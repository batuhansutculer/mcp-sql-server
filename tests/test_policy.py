"""Tests for the policy layer.

These exercise validation directly, with no database and no model involved.
"""

import pytest

from policy import MASK, Policy, PolicyError, apply_masking, validate

POLICY = Policy.load()


def refuse(sql: str) -> str:
    """Assert the query is refused, and return the reason given."""
    with pytest.raises(PolicyError) as caught:
        validate(sql, POLICY)
    return caught.value.message


class TestPermitted:
    """Ordinary reads still work -- the policy blocks, it doesn't break the tool."""

    def test_simple_select(self):
        validate("SELECT * FROM customers", POLICY)

    def test_join_and_aggregate(self):
        validate(
            """
            SELECT c.name, SUM(p.price * o.quantity) AS total
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            JOIN products p ON o.product_id = p.id
            GROUP BY c.name
            """,
            POLICY,
        )

    def test_cte_is_allowed(self):
        """Regression: a text check on `startswith("select")` rejects this.

        WITH ... SELECT is an ordinary read and Claude writes it routinely, so
        refusing it was a correctness bug, not extra safety.
        """
        validate(
            "WITH totals AS (SELECT customer_id FROM orders) SELECT * FROM totals",
            POLICY,
        )

    def test_masked_column_may_be_selected_plainly(self):
        validate("SELECT name, email FROM customers", POLICY)


class TestStatementType:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE orders",
            "DELETE FROM orders",
            "UPDATE customers SET name = 'x'",
            "INSERT INTO customers VALUES (9, 'x', 'x', 'x', 'x')",
            "CREATE TABLE evil (id INTEGER)",
            "ALTER TABLE customers ADD COLUMN x TEXT",
        ],
    )
    def test_writes_are_refused(self, sql):
        assert "only select" in refuse(sql).lower()


class TestRestrictedTables:
    """A denied table must stay unreachable however it is referenced."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM payment_methods",
            "SELECT * FROM PAYMENT_METHODS",
            "SELECT * FROM customers WHERE id IN (SELECT customer_id FROM payment_methods)",
            "SELECT c.name FROM customers c JOIN payment_methods p ON p.customer_id = c.id",
            "WITH leak AS (SELECT * FROM payment_methods) SELECT * FROM leak",
            "SELECT * FROM customers UNION SELECT * FROM payment_methods",
            'SELECT * FROM "payment_methods"',
        ],
    )
    def test_restricted_table_is_unreachable(self, sql):
        assert "restricted" in refuse(sql).lower()

    def test_schema_catalogue_is_restricted(self):
        """sqlite_master leaks the CREATE statement of every table."""
        assert "restricted" in refuse("SELECT sql FROM sqlite_master").lower()

    def test_table_name_in_a_string_literal_is_not_a_reference(self):
        """The AST distinguishes a table from a word that merely looks like one.

        A text-matching guard refuses this; there is no table reference here.
        """
        validate("SELECT name FROM customers WHERE name != 'payment_methods'", POLICY)


class TestMaskedColumns:
    """Masking only holds if the value cannot be recovered indirectly."""

    def test_filtering_on_a_masked_column_is_refused(self):
        """`WHERE email LIKE 'a%'` recovers the value one character at a time."""
        assert "masked" in refuse(
            "SELECT name FROM customers WHERE email LIKE 'a%'"
        ).lower()

    def test_wrapping_a_masked_column_in_a_function_is_refused(self):
        assert "masked" in refuse("SELECT upper(email) FROM customers").lower()

    def test_aliasing_a_masked_column_is_refused(self):
        """An alias changes the output column name, defeating output masking."""
        assert "masked" in refuse("SELECT email AS contact FROM customers").lower()

    def test_ordering_by_a_masked_column_is_refused(self):
        assert "masked" in refuse(
            "SELECT name FROM customers ORDER BY email"
        ).lower()


class TestMalformedInput:
    def test_multiple_statements_are_refused(self):
        assert "one statement" in refuse(
            "SELECT * FROM customers; DROP TABLE orders"
        ).lower()

    def test_empty_query_is_refused(self):
        assert refuse("")

    def test_unparseable_sql_is_refused_not_crashed(self):
        assert refuse("SELECT FROM WHERE (((")


class TestApplyMasking:
    def test_masked_values_are_replaced(self):
        rows = [("Anna", "anna@example.com")]
        assert apply_masking(["name", "email"], rows, POLICY) == [["Anna", MASK]]

    def test_unmasked_columns_pass_through(self):
        rows = [("Anna", "Berlin")]
        assert apply_masking(["name", "city"], rows, POLICY) == [["Anna", "Berlin"]]
