
from mcp.server.fastmcp import FastMCP
import sqlite3

mcp = FastMCP("Business Database Server")

@mcp.tool()
def list_tables() -> str:
    """List all tables in the business database."""
    connection = sqlite3.connect("business.db")
    cursor = connection.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    connection.close()
    return str(tables)

@mcp.tool()
def describe_table(table_name: str) -> str:
    """Show the columns and their types for a given table."""
    connection = sqlite3.connect("business.db")
    cursor = connection.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()
    connection.close()
    return str(columns)

@mcp.tool()
def run_query(sql: str) -> str:
    """Run a read-only SQL SELECT query against the business database and return the results."""
    # Normalize for checking: lowercase and strip surrounding whitespace
    cleaned = sql.strip().lower()

    # Guardrail 1: only allow SELECT queries (block DELETE, DROP, UPDATE, INSERT, etc.)
    if not cleaned.startswith("select"):
        return "Error: only read-only SELECT queries are allowed."

    # Guardrail 2: block access to the sensitive payment_methods table
    if "payment_methods" in cleaned:
        return "Error: access to payment data is restricted."

    # If it passed both checks, run it
    connection = sqlite3.connect("business.db")
    cursor = connection.cursor()
    cursor.execute(sql)
    rows = cursor.fetchall()
    connection.close()
    return str(rows)

if __name__ == "__main__":
    mcp.run()
