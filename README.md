# mcp-sql-server

A Model Context Protocol (MCP) server that lets an AI assistant (Claude) query a
business database in plain language — safely.

This is a self-contained prototype of a pattern I build in professional work:
exposing structured business data to an AI agent through well-defined tools, with
**server-side guardrails** that control what the agent is allowed to read and do.
The database and data here are mock, so the whole thing can be public; the design
mirrors real production tooling I've built to connect Claude to live business systems.

## What it does

The server exposes three tools to Claude over MCP:

| Tool | Purpose |
|------|---------|
| `list_tables()` | Lists the tables in the database |
| `describe_table(name)` | Returns the columns and types of a table |
| `run_query(sql)` | Runs a **read-only** SQL query and returns the results |

With these, Claude can explore the schema and answer real questions about the data —
for example, *"Which customer spent the most?"* — by inspecting the tables, writing
the SQL itself, and running it through `run_query`.

The mock database models a small business: `customers`, `products`, `orders`, and a
sensitive `payment_methods` table (used to demonstrate the access guardrail below).

## The guardrail (the point of the project)

`run_query` does not blindly execute whatever SQL the model generates. Before running
anything, it inspects the query and refuses it if it:

1. **Is not a `SELECT`** — blocks `DELETE`, `DROP`, `UPDATE`, `INSERT`, etc.
   The tool is read-only by design.
2. **References the `payment_methods` table** — sensitive data is walled off.

```python
cleaned = sql.strip().lower()
if not cleaned.startswith("select"):
    return "Error: only read-only SELECT queries are allowed."
if "payment_methods" in cleaned:
    return "Error: access to payment data is restricted."
```

The important property: this is enforced **server-side, in code** — not as a prompt
instruction to the model. Prompt-based restrictions can be talked around; a server-side
check holds regardless of what the model is prompted to do. So even if Claude is asked
to read payment data or drop a table, the tool refuses before any query touches the
database.

This is the same principle that matters when connecting AI to real financial or customer
systems: the agent gets access to exactly what it needs, and the boundaries don't depend
on the model behaving.

## How to run it

**Requirements:** Python 3.10+, [uv](https://docs.astral.sh/uv/), and Claude Desktop.

1. Install dependencies:
   ```bash
   uv add "mcp[cli]>=1.0,<2.0"
   ```

2. Create and seed the database:
   ```bash
   uv run setup_database.py
   ```

3. Register the server with Claude Desktop. Add this to
   `claude_desktop_config.json` (Settings → Developer → Edit Config), using the
   absolute path to this folder:
   ```json
   {
     "mcpServers": {
       "mcp-sql-server": {
         "command": "uv",
         "args": ["--directory", "/absolute/path/to/mcp-sql-server", "run", "server.py"]
       }
     }
   }
   ```

4. Fully restart Claude Desktop, then ask it something like
   *"Which customer spent the most?"* and watch it query the database through the tools.

## Testing the guardrail

The guardrail can be tested directly, without Claude, to confirm it's enforced in code:

```bash
uv run test_guard.py
```

Three assertions cover the cases that matter:

| Query | Expected |
|-------|----------|
| `SELECT * FROM payment_methods` | refused — restricted data |
| `DROP TABLE orders` | refused — not a `SELECT` |
| `SELECT name FROM customers` | returns rows |

```
PASS  test_restricted_table_is_blocked
PASS  test_non_select_is_blocked
PASS  test_permitted_select_returns_rows

All 3 guardrail tests passed.
```

The tests call `run_query` directly — no model involved — so a broken guardrail
fails the run rather than printing something that merely looks wrong. They're
plain `assert`s, so `pytest` works too.

## Demo

Once the server is connected, you can ask Claude questions in plain language and it
will explore the schema, write the SQL itself, and run it through the tools.

**Asking an analytical question:**

> **You:** Which customer spent the most?
>
> **Claude:** *(calls `list_tables`, then `describe_table` on the relevant tables, then
> `run_query` with a join across `orders`, `products`, and `customers`)*
>
> Anna Schmidt (Berlin) spent the most — €1,097.00 across 2 orders. Full ranking:
> Anna Schmidt €1,097.00 · Luca Rossi €899.00 · Marie Dubois €447.00 · Tom Becker €299.00.

Claude worked out on its own that answering this required joining three tables and
computing `quantity × price`, since orders don't store a total.

**Hitting the guardrail:**

> **You:** Show me the payment methods table.
>
> **Claude:** The schema is readable, but the rows came back with *"access to payment
> data is restricted"* — the restriction is enforced server-side in the tool, so the
> data isn't returned.

> **You:** Delete all the orders.
>
> **Claude:** That's rejected by the server — `run_query` only permits read-only
> `SELECT` statements, so a `DELETE` never reaches the database.

The guardrail holds regardless of how the request is phrased, because the check runs
in the server code — not as an instruction the model could be talked out of.

## Limitations and hardening

This is a prototype, and the guardrail is deliberately simple — a text-based check on
the incoming SQL. That handles the common cases cleanly, but it has known edges I'd
address before anything like this went to production:

- **The guardrail matches on query text.** Blocking non-`SELECT` statements and the
  `payment_methods` keyword covers the obvious cases, but text matching can be worked
  around by sufficiently creative queries. A production version would parse the SQL
  properly (validate the statement type and referenced tables/columns from a parsed
  AST) rather than string-matching.
- **`describe_table` interpolates input into the query.** For a read-only `PRAGMA`
  this is low-risk, but it's the same string-building pattern that causes SQL
  injection elsewhere. Production code should avoid building SQL from untrusted input.
- **Access control lives in application code.** The strongest boundary isn't in the
  tool at all — it's in the database. In production I'd enforce it at the data layer:
  connect with a read-only role, and use table/column permissions so sensitive data is
  unreachable regardless of what query is sent. Defense in depth, not a single check.

The point of the prototype is the *principle* — that the boundary is enforced in code,
server-side, and doesn't depend on the model behaving. The hardening above is how you'd
make that principle robust for real financial or customer data.

## Files

| File | Purpose |
|------|---------|
| `server.py` | The MCP server and its tools |
| `setup_database.py` | Creates and seeds the mock SQLite database |
| `test_guard.py` | Tests the guardrail logic directly |
| `business.db` | The generated SQLite database (created by the setup script) |

## Stack

Python · SQLite · Model Context Protocol (MCP) · Anthropic Claude

## License

MIT — see [LICENSE](LICENSE).