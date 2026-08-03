"""An MCP server exposing a business database to an AI agent.

Every tool call goes through two independent boundaries:

  1. The policy layer (policy.py), which validates the parsed query against
     policy.yaml before it reaches the database.
  2. A read-only database connection, so a write that somehow got past the
     policy layer would still fail at the driver.

Neither depends on how the model was prompted. Every attempt, allowed or
refused, is written to the audit log.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import audit
from policy import Policy, PolicyError, apply_masking, validate

mcp = FastMCP("Business Database Server")

# Resolve the database next to this file, so the server finds it regardless of
# the working directory it was launched from.
DB_PATH = Path(__file__).parent / "business.db"

POLICY = Policy.load()


def _connect() -> sqlite3.Connection:
    """Open the database read-only.

    `mode=ro` is the second line of defence: even a write that slipped past the
    policy layer fails here. It also refuses to create the file if it is
    missing, instead of silently opening an empty database.

    Callers wrap this in contextlib.closing. A sqlite3 connection used directly
    as a context manager manages the transaction, not the connection -- it does
    not close, which leaks a handle on every failed query.
    """
    return sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)


def _ok(**payload) -> str:
    return json.dumps(payload, default=str)


def _error(message: str, hint: str | None = None) -> str:
    payload = {"error": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload)


def _missing_database() -> str | None:
    if DB_PATH.exists():
        return None
    return _error(
        "The database has not been created yet.",
        hint="Run `uv run setup_database.py` to create and seed it.",
    )


def _real_tables(cursor: sqlite3.Cursor) -> list[str]:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


@mcp.tool()
def list_tables() -> str:
    """List the tables in the business database.

    Returns each table name and whether the policy allows reading it. Tables
    marked accessible=false exist but are restricted; do not query them.
    """
    if problem := _missing_database():
        return problem

    with closing(_connect()) as connection:
        tables = _real_tables(connection.cursor())

    audit.record(tool="list_tables", decision="allowed", row_count=len(tables))
    return _ok(
        tables=[
            {"name": name, "accessible": not POLICY.is_denied(name)}
            for name in tables
        ]
    )


@mcp.tool()
def describe_table(table_name: str) -> str:
    """Show the columns and types of a table.

    Columns marked masked=true can be selected, but their values are hidden and
    they cannot be used in WHERE clauses or expressions.
    """
    if problem := _missing_database():
        return problem

    if POLICY.is_denied(table_name):
        audit.record(
            tool="describe_table",
            decision="denied",
            sql=table_name,
            reason="restricted table",
        )
        return _error(f"Access to {table_name} is restricted.")

    with closing(_connect()) as connection:
        cursor = connection.cursor()
        known = _real_tables(cursor)

        # Match against the tables that actually exist rather than
        # interpolating the argument straight into the PRAGMA. The value
        # reaching SQL is then one the database gave us, not one the caller
        # chose.
        match = next((t for t in known if t.lower() == table_name.lower()), None)
        if match is None:
            audit.record(
                tool="describe_table",
                decision="denied",
                sql=table_name,
                reason="unknown table",
            )
            return _error(
                f"No table named '{table_name}'.",
                hint=f"Available tables: {', '.join(known)}.",
            )

        cursor.execute(f"PRAGMA table_info({match})")
        columns = cursor.fetchall()

    masked = POLICY.masked_columns.get(match.lower(), frozenset())
    audit.record(tool="describe_table", decision="allowed", sql=match)
    return _ok(
        table=match,
        columns=[
            {"name": row[1], "type": row[2], "masked": row[1].lower() in masked}
            for row in columns
        ],
    )


@mcp.tool()
def run_query(sql: str) -> str:
    """Run a read-only SQL SELECT query and return the results.

    Results come back as column names plus rows. Restricted tables and masked
    columns are enforced by the server; if a query is refused, the reason
    explains what to change.
    """
    if problem := _missing_database():
        return problem

    try:
        validate(sql, POLICY)
    except PolicyError as refusal:
        audit.record(
            tool="run_query", decision="denied", sql=sql, reason=refusal.message
        )
        return _error(refusal.message, refusal.hint)

    try:
        with closing(_connect()) as connection:
            cursor = connection.cursor()
            cursor.execute(sql)
            columns = [description[0] for description in cursor.description or []]
            # One more than the cap, so truncation can be reported honestly
            # rather than the agent assuming it saw everything.
            rows = cursor.fetchmany(POLICY.max_rows + 1)
    except sqlite3.Error as err:
        audit.record(
            tool="run_query", decision="error", sql=sql, reason=str(err)
        )
        return _error(
            f"Query failed: {err}",
            hint="Call list_tables or describe_table to check names, then retry.",
        )

    truncated = len(rows) > POLICY.max_rows
    rows = rows[: POLICY.max_rows]

    audit.record(
        tool="run_query", decision="allowed", sql=sql, row_count=len(rows)
    )
    return _ok(
        columns=columns,
        rows=apply_masking(columns, rows, POLICY),
        row_count=len(rows),
        truncated=truncated,
    )


if __name__ == "__main__":
    mcp.run()
