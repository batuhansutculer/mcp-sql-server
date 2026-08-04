# mcp-sql-server

[![tests](https://github.com/batuhansutculer/mcp-sql-server/actions/workflows/tests.yml/badge.svg)](https://github.com/batuhansutculer/mcp-sql-server/actions/workflows/tests.yml)

A Model Context Protocol (MCP) server that lets an AI assistant (Claude) query a
business database in plain language — behind a policy-enforced access layer.

This is a self-contained prototype of a pattern I build in professional work:
exposing structured business data to an AI agent through well-defined tools, where
what the agent may read is decided **server-side, in configuration**, not by the
prompt. The database and data here are mock, so the whole thing can be public; the
design mirrors real production tooling I've built to connect Claude to live
business systems.

## Architecture

```mermaid
flowchart LR
    C["Claude<br/>(via demo.py or Claude Desktop)"] -->|tool call over MCP| S["server.py<br/>list_tables · describe_table · run_query"]
    S --> P{"policy layer<br/>parses the SQL<br/>checks policy.yaml"}
    P -->|refused| E["error + reason<br/>returned to the model"]
    P -->|allowed| D[("business.db<br/>opened read-only")]
    D --> M["output masking"]
    M --> S
    P -.-> L[("audit.log")]
    E -.-> L
    M -.-> L
```

Two independent boundaries sit between the model and the data. The policy layer
refuses queries before they run; the connection is opened read-only, so a write
that somehow got past the policy layer still fails at the driver. Every attempt,
allowed or refused, is written to the audit log.

## The tools

| Tool | Purpose |
|------|---------|
| `list_tables()` | Lists tables, each flagged with whether the policy allows reading it |
| `describe_table(name)` | Returns columns and types, each flagged as masked or not |
| `run_query(sql)` | Validates a read-only query against the policy, runs it, masks the results |

With these, Claude explores the schema and answers real questions by writing the
SQL itself — for example, *"Which customer spent the most?"* requires joining three
tables and computing `quantity × price`, since orders don't store a total.

The mock database models a small business: `customers`, `products`, `orders`, and a
sensitive `payment_methods` table.

## The policy layer

Access rules live in [`policy.yaml`](policy.yaml), not in code:

```yaml
allowed_statements: [SELECT]
denied_tables:
  - payment_methods
  - sqlite_master
masked_columns:
  customers: [email]
max_rows: 500
```

The same server enforces a different policy per deployment — analyst, support,
admin — without touching `server.py`.

### Validation runs on a parsed query, not on the text

The obvious implementation is a string check: does the query start with `select`,
does it contain `payment_methods`. That is easy to write and wrong in both
directions. It rejects `WITH ... SELECT`, which is an ordinary read that Claude
writes routinely. And it cannot tell a table reference from the same word inside a
string literal.

So the query is parsed with [`sqlglot`](https://github.com/tobymao/sqlglot) and the
resolved table references are checked against the policy:

```python
parsed = sqlglot.parse_one(sql, dialect="sqlite")
referenced = {t.name.lower() for t in parsed.find_all(exp.Table)}
if denied := referenced & policy.denied_tables:
    raise PolicyError(f"Access to {', '.join(denied)} is restricted.")
```

A restricted table is then unreachable however it's referenced — directly, through
a join, a subquery, a `UNION`, or a CTE. Those cases are in the test suite.

### Masking fails closed

Hiding a column's values only works if they can't be recovered indirectly.
`WHERE email LIKE 'a%'` reads a masked value one character at a time; an alias or a
function wrapper changes the output column name and defeats masking that matches on
it. So a masked column may appear only as a plainly selected column, and anything
else is refused rather than silently allowed.

Masking itself is applied to the result set rather than the query, which is what
makes `SELECT *` work: the column never appears in the SQL, but the cursor reports
its real name.

### The boundary that doesn't depend on application code

The connection is opened read-only at the driver:

```python
sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)
```

This is the layer that holds if the policy layer has a bug. A test bypasses the
policy entirely and confirms the database still refuses to change.

### Audit log

Every tool call appends one JSON object to `audit.log` — the query, the decision,
and the reason for a refusal:

```json
{"timestamp": "2026-08-03T21:44:02+00:00", "tool": "run_query", "decision": "denied",
 "sql": "SELECT * FROM payment_methods", "reason": "Access to payment_methods is restricted."}
```

An access-control layer you can't review after the fact is hard to trust, and
*"show me what the agent actually ran"* is the first question anyone asks when an
AI system touches customer or financial data. JSON Lines so it's queryable:

```bash
cat audit.log | jq 'select(.decision == "denied")'
```

## What the tools return

Real output from the tools (see [`tests/`](tests/) for these as assertions).

**An analytical query** — results come back as columns and rows, so the model isn't
inferring the schema from tuple positions:

```json
{"columns": ["name", "total"],
 "rows": [["Anna Schmidt", 1097.0], ["Luca Rossi", 899.0],
          ["Marie Dubois", 447.0], ["Tom Becker", 299.0]],
 "row_count": 4, "truncated": false}
```

**A masked column** — the rows still return, the values don't:

```json
{"columns": ["name", "email"],
 "rows": [["Anna Schmidt", "***"], ["Luca Rossi", "***"],
          ["Marie Dubois", "***"], ["Tom Becker", "***"]],
 "row_count": 4, "truncated": false}
```

**A restricted table, reached through a subquery:**

```
SELECT name FROM customers WHERE id IN (SELECT customer_id FROM payment_methods)
```
```json
{"error": "Access to payment_methods is restricted.",
 "hint": "This table holds sensitive data and is not available."}
```

**An attempt to filter on a masked column:**

```json
{"error": "Column 'email' is masked and cannot be used in filters, functions, or aliases.",
 "hint": "You may select email directly, but its values are hidden."}
```

**A write:**

```json
{"error": "Only SELECT queries are allowed (got DROP).", "hint": "This tool is read-only."}
```

Refusals carry a reason and a hint because the message is part of the interface —
the model has to be able to work out what to do instead. The same applies to
ordinary failures: a query against a table that doesn't exist returns an error
naming the available tables, not a traceback.

## A real session

Transcribed from Claude Desktop, with the corresponding [`audit.log`](#audit-log)
entries alongside.

**"Which customer spent the most?"** — Claude called `list_tables`, then
`describe_table` on `customers`, `orders` and `products`, then wrote this
unprompted:

```sql
SELECT c.id, c.name, c.city,
       COUNT(DISTINCT o.id) AS num_orders,
       SUM(o.quantity)      AS units,
       ROUND(SUM(o.quantity * p.price), 2) AS total_spent
FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN products  p ON p.id = o.product_id
GROUP BY c.id, c.name, c.city
ORDER BY total_spent DESC;
```

> **Anna Schmidt** (Berlin) — 1,097.00 across 2 orders, 3 units. Then Luca Rossi
> 899.00, Marie Dubois 447.00, Tom Becker 299.00.
>
> *"`orders` has no price column, so spend is `quantity × products.price`."*

It worked out on its own that the answer needed a three-table join and a derived
total, because `orders` stores no monetary value.

**"Show me the customers table with their emails"** — the query is allowed and
all four rows return, with `email` as `***`. Claude's reading of it:

> *"`email` is flagged `masked: true` in the server's column policy, so it can be
> selected but the values are redacted server-side... the masking happens in the
> MCP server, not something I can query around from this side."*

**"Which customers have gmail addresses?"** — refused, and Claude reconstructed
why the rule exists rather than looking for a way around it:

> *"A `WHERE email LIKE '%gmail%'` filter would leak the masked column's contents
> one bit at a time — I'd learn who's on Gmail without ever seeing a value, and
> the same trick generalizes (`LIKE 'a%'`, `LIKE 'ab%'`, and so on) until you've
> reconstructed the addresses character by character."*

That is exactly the attack the fail-closed rule exists to stop, described by the
model that just got blocked by it.

**"Show me the payment methods"** and **"Delete all the orders"** — both declined
**before any query was sent**. Claude read `accessible: false` from `list_tables`
and stopped:

> *"`payment_methods` comes back from `list_tables` as `accessible: false`... So
> I'll respect that rather than fire off a `SELECT` to watch it bounce."*

There are no audit entries for either attempt, because no query was made. That is
the `accessible` flag doing its job: publishing the boundary in the tool contract
means the agent doesn't burn a turn discovering it. Worth being precise about what
this proves — here the *agent* declined; the *server's* enforcement of those two
rules is demonstrated by the refusal above, by the bypass tests, and by the
read-only connection test.

### Audit log

The same session, as recorded server-side:

```json
{"tool": "list_tables",    "decision": "allowed", "row_count": 4}
{"tool": "describe_table", "decision": "allowed", "sql": "customers"}
{"tool": "describe_table", "decision": "allowed", "sql": "orders"}
{"tool": "describe_table", "decision": "allowed", "sql": "products"}
{"tool": "run_query",      "decision": "allowed", "sql": "SELECT c.id, c.name, ... ORDER BY total_spent DESC LIMIT 10", "row_count": 4}
{"tool": "run_query",      "decision": "allowed", "sql": "SELECT id, name, email, city, signup_date FROM customers ORDER BY id", "row_count": 4}
{"tool": "run_query",      "decision": "denied",  "sql": "SELECT id, name, city FROM customers WHERE email LIKE '%gmail%' ORDER BY id", "reason": "Column 'email' is masked and cannot be used in filters, functions, or aliases."}
```

Timestamps elided for width. Note the `LIMIT 10` on the join — Claude added that
itself, and the row cap would have applied regardless.

## Running it

**Requirements:** Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run setup_database.py
```

### From the command line

[`demo.py`](demo.py) is an MCP client: it launches the server as a subprocess,
speaks the protocol over stdio, hands Claude the three tools, and runs the
tool-use loop until Claude has an answer. Set `ANTHROPIC_API_KEY`, then:

```bash
uv run demo.py "Which customer spent the most?"
```

The transcript prints each tool call and a one-line summary of what came back,
then Claude's answer:

```
MCP server ready -- tools: list_tables, describe_table, run_query

> Which customer spent the most?

  -> list_tables()
     customers, orders, payment_methods [restricted], products
  -> describe_table(table_name=customers)
     customers: 5 columns
  -> run_query(sql=SELECT c.name, SUM(p.price * o.quantity) AS total FROM ...)
     4 row(s)
```

Claude explores the schema and writes the SQL itself, so the calls vary between
runs — see [A real session](#a-real-session) for a full exchange.

A refused query is reported as a refusal rather than being flattened into an
empty result, so hitting the guardrail is visible in the same transcript:

```
  -> run_query(sql=SELECT * FROM payment_methods)
     REFUSED -- Access to payment_methods is restricted.
  -> run_query(sql=SELECT name FROM customers WHERE email LIKE 'a%')
     REFUSED -- Column 'email' is masked and cannot be used in filters, functions, or aliases.
```

Because this goes through the real protocol rather than importing the tool
functions, it exercises the MCP layer, the policy layer, and the read-only
connection together. Add `--verbose` to see the server's own logs.

### In Claude Desktop

No API key needed — a free Claude account is enough, since the model runs on
Anthropic's side and this server is just a local subprocess it talks to.

1. Register the server — add this to `claude_desktop_config.json`
   (Settings → Developer → Edit Config), using the absolute path to this folder:
   ```json
   {
     "mcpServers": {
       "mcp-sql-server": {
         "command": "/absolute/path/to/uv",
         "args": ["--directory", "/absolute/path/to/mcp-sql-server", "run", "server.py"]
       }
     }
   }
   ```

   **Use the absolute path to `uv`, not just `uv`.** Claude Desktop launches
   servers with a minimal environment rather than your shell's, so a bare `uv`
   that works in a terminal can fail to resolve there — the usual cause of a
   server that shows up but never connects. Find it with `which uv` (macOS and
   Linux) or `(Get-Command uv).Source` (Windows); it is often under
   `~/.local/bin`.

2. Fully quit and reopen Claude Desktop — reloading the window is not enough.

3. Ask it something like *"Which customer spent the most?"* and watch it call
   `list_tables`, inspect the schema, and write the join itself. Then ask for the
   payment methods to see the policy layer refuse.

## Tests

```bash
uv run pytest
```

62 tests covering the policy layer, the tools, and the demo client's output
formatting. The ones that matter most are the bypass attempts — a guardrail is
only as good as the attacks it survives:

| Attempt | Result |
|---------|--------|
| `SELECT * FROM payment_methods` | refused |
| `SELECT * FROM PAYMENT_METHODS` | refused — case |
| `... WHERE id IN (SELECT ... FROM payment_methods)` | refused — subquery |
| `... JOIN payment_methods ON ...` | refused — join |
| `WITH leak AS (SELECT * FROM payment_methods) ...` | refused — CTE |
| `SELECT * FROM customers UNION SELECT * FROM payment_methods` | refused — union |
| `SELECT sql FROM sqlite_master` | refused — schema disclosure |
| `SELECT * FROM customers; DROP TABLE orders` | refused — multiple statements |
| `SELECT name FROM customers WHERE email LIKE 'a%'` | refused — masked column in filter |
| `SELECT upper(email) FROM customers` | refused — masked column in function |
| `SELECT email AS contact FROM customers` | refused — masked column aliased |
| `SELECT name FROM customers WHERE name != 'payment_methods'` | **allowed** — string literal, not a table |
| `DELETE FROM orders` bypassing the policy entirely | refused by the read-only connection |

That last-but-one row is the case a text-matching guard gets wrong: there is no
table reference in it, and parsing knows the difference.

## Limitations

- **The policy covers tables and columns, not rows.** There's no concept of "this
  user may see their own orders only." Row-level policy is the natural next step
  and would need the query rewritten with an injected predicate, not just validated.
- **Masking is enforced by refusing indirect use, which is blunt.** A legitimate
  `COUNT(email)` is refused along with `WHERE email LIKE 'a%'`. Failing closed is
  the right default, but a real system would classify expressions by whether they
  actually leak values rather than rejecting all of them.
- **Masked columns are matched by name across tables.** A result set doesn't carry
  table provenance, so a same-named column on another table would be masked too.
  Safe direction to be wrong in, but imprecise.
- **The strongest boundary is still the database, not this code.** The read-only
  connection is one step; in production the agent would connect as a role with
  table and column grants, so restricted data is unreachable regardless of what
  query is sent or what this server does.
- **`sqlglot` is doing security-relevant work.** Any parser disagreement between it
  and SQLite is a potential gap. Pinning the version and tracking its releases
  matters more than it would for a formatting tool.

## Files

| File | Purpose |
|------|---------|
| [`server.py`](server.py) | The MCP server and its three tools |
| [`policy.py`](policy.py) | Policy loading, SQL validation, output masking |
| [`policy.yaml`](policy.yaml) | The access policy — the part you'd change per deployment |
| [`audit.py`](audit.py) | Append-only JSON Lines audit log |
| [`demo.py`](demo.py) | MCP client — asks Claude a question from the command line |
| [`setup_database.py`](setup_database.py) | Creates and seeds the mock SQLite database |
| [`tests/`](tests/) | Policy and tool tests, including bypass attempts |

## Stack

Python · SQLite · [sqlglot](https://github.com/tobymao/sqlglot) · Model Context Protocol (MCP) · [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) · Claude Opus 5

## License

MIT — see [LICENSE](LICENSE).
