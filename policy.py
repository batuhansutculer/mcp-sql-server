"""The access policy layer.

Decides what SQL the agent is allowed to run, and what it is allowed to see in
the results. Rules come from `policy.yaml` so they are configuration rather
than code.

Validation works on a parsed syntax tree rather than on the query text. Text
matching is easy to write and easy to defeat -- it cannot tell a table name
from the same word in a string literal, and it misses tables reached through
subqueries or CTEs. Parsing the statement and inspecting the actual table and
column references it resolves to avoids that whole class of bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

POLICY_PATH = Path(__file__).parent / "policy.yaml"
DIALECT = "sqlite"
MASK = "***"

# Root nodes that represent a read. A `WITH ... SELECT` parses to a Select
# carrying a `with` clause, so CTEs are covered by exp.Select.
_READ_NODES = (exp.Select, exp.Union, exp.Subquery)


class PolicyError(Exception):
    """Raised when a query is refused. The message is shown to the agent."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class Policy:
    allowed_statements: frozenset[str]
    denied_tables: frozenset[str]
    masked_columns: dict[str, frozenset[str]]
    max_rows: int

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> "Policy":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        masked = {
            table.lower(): frozenset(c.lower() for c in columns or [])
            for table, columns in (raw.get("masked_columns") or {}).items()
        }
        return cls(
            allowed_statements=frozenset(
                s.upper() for s in raw.get("allowed_statements") or []
            ),
            denied_tables=frozenset(
                t.lower() for t in raw.get("denied_tables") or []
            ),
            masked_columns=masked,
            max_rows=int(raw.get("max_rows", 500)),
        )

    @property
    def masked_column_names(self) -> frozenset[str]:
        """Every masked column name, regardless of which table it belongs to.

        Output columns are matched by name because a result set does not carry
        table provenance. Erring toward masking a same-named column on another
        table is the safe direction to be wrong in.
        """
        return frozenset().union(*self.masked_columns.values()) if self.masked_columns else frozenset()

    def is_denied(self, table: str) -> bool:
        return table.lower() in self.denied_tables


def validate(sql: str, policy: Policy) -> exp.Expression:
    """Check `sql` against `policy`. Returns the parsed statement, or raises.

    Raising PolicyError rather than returning a bool keeps the reason attached
    to the refusal, so the agent is told what to do differently.
    """
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except sqlglot.ParseError as err:
        raise PolicyError(
            f"Could not parse SQL: {err}",
            hint="Check the syntax and try again.",
        ) from err

    statements = [s for s in statements if s is not None]
    if not statements:
        raise PolicyError("Empty query.")
    if len(statements) > 1:
        raise PolicyError(
            "Only one statement may be run at a time.",
            hint="Split this into separate run_query calls.",
        )

    statement = statements[0]
    _check_statement_type(statement, policy)
    _check_denied_tables(statement, policy)
    _check_masked_column_usage(statement, policy)
    return statement


def _check_statement_type(statement: exp.Expression, policy: Policy) -> None:
    if "SELECT" in policy.allowed_statements and isinstance(statement, _READ_NODES):
        return
    found = type(statement).__name__.upper()
    raise PolicyError(
        f"Only {', '.join(sorted(policy.allowed_statements))} queries are allowed "
        f"(got {found}).",
        hint="This tool is read-only.",
    )


def _check_denied_tables(statement: exp.Expression, policy: Policy) -> None:
    """Refuse if the query resolves to a denied table anywhere.

    CTE names also parse as tables when referenced, so a CTE named after a
    denied table is refused too. That is a false positive, but it fails in the
    safe direction and the alternative leaks.
    """
    referenced = {t.name.lower() for t in statement.find_all(exp.Table) if t.name}
    denied = sorted(referenced & policy.denied_tables)
    if denied:
        raise PolicyError(
            f"Access to {', '.join(denied)} is restricted.",
            hint="This table holds sensitive data and is not available.",
        )


def _check_masked_column_usage(statement: exp.Expression, policy: Policy) -> None:
    """Allow masked columns only as plainly selected columns.

    A masked value that can be filtered on is not masked -- `WHERE email LIKE
    'a%'` recovers it one character at a time. Wrapping it in a function or an
    alias also defeats output masking, which matches on the result column name.
    So anywhere other than a bare projection is refused.
    """
    masked = policy.masked_column_names
    if not masked:
        return

    bare_projections = {
        id(projection)
        for select in statement.find_all(exp.Select)
        for projection in select.expressions
        if isinstance(projection, exp.Column)
    }

    for column in statement.find_all(exp.Column):
        if column.name.lower() in masked and id(column) not in bare_projections:
            raise PolicyError(
                f"Column '{column.name}' is masked and cannot be used in "
                f"filters, functions, or aliases.",
                hint=f"You may select {column.name} directly, but its values are hidden.",
            )


def apply_masking(
    columns: list[str], rows: list[tuple], policy: Policy
) -> list[list]:
    """Replace masked column values in a result set.

    Runs on the rows after the query, so `SELECT *` is covered: the real column
    names come back from the cursor even though they never appeared in the SQL.
    """
    masked = policy.masked_column_names
    masked_positions = {
        i for i, name in enumerate(columns) if name.lower() in masked
    }
    if not masked_positions:
        return [list(row) for row in rows]
    return [
        [MASK if i in masked_positions else value for i, value in enumerate(row)]
        for row in rows
    ]
