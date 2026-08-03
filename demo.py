"""Ask Claude a question about the business database, from the command line.

This launches the MCP server in this repo as a subprocess and talks to it over
the Model Context Protocol -- the same path Claude Desktop takes. Claude gets
the server's three tools, decides which to call, writes its own SQL, and works
until it has an answer. The policy layer applies exactly as it does anywhere
else, so a refused query is refused here too.

Talking to the real server over stdio, rather than importing the tool functions
directly, is the point: it exercises the protocol, the policy layer, and the
read-only connection together.

    uv run demo.py "Which customer spent the most?"

Requires an Anthropic API key in ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from anthropic import APIError, AsyncAnthropic, AuthenticationError, RateLimitError
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent
MODEL = "claude-opus-5"
MAX_TOKENS = 16_000

SYSTEM = """You are a business analyst with read-only access to a company database.

Explore the schema before writing SQL -- do not guess at table or column names.
Some tables and columns are restricted by server-side policy. If a query is
refused, report the refusal rather than trying to work around it.

Answer with the figures, briefly."""


def _text(blocks) -> str:
    """The prose Claude wrote in one turn, if any."""
    return "".join(b.text for b in blocks if b.type == "text").strip()


def _format_args(tool_input: dict) -> str:
    """Render tool arguments compactly enough to sit on one line."""
    parts = []
    for key, value in tool_input.items():
        text = " ".join(str(value).split())
        if len(text) > 90:
            text = text[:87] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _summarize_result(content) -> str:
    """One line describing what a tool returned.

    The tools return JSON, so a refusal and a result set can be told apart and
    reported differently -- which is what makes the guardrail visible in the
    transcript rather than buried in a blob of rows.
    """
    if isinstance(content, str):
        raw = content
    else:
        raw = "".join(
            part.get("text", "")
            for part in content or []
            if isinstance(part, dict) and part.get("type") == "text"
        )

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return " ".join(raw.split())[:100]

    if not isinstance(data, dict):
        return " ".join(raw.split())[:100]
    if "error" in data:
        return f"REFUSED -- {data['error']}"
    if "row_count" in data:
        suffix = " (truncated)" if data.get("truncated") else ""
        return f"{data['row_count']} row(s){suffix}"
    if "tables" in data:
        names = ", ".join(
            t["name"] + ("" if t["accessible"] else " [restricted]")
            for t in data["tables"]
        )
        return names
    if "columns" in data:
        return f"{data.get('table', '?')}: {len(data['columns'])} columns"
    return " ".join(raw.split())[:100]


def _first_api_error(err: BaseException) -> APIError | None:
    """Find an Anthropic error inside a nested exception group.

    The MCP client runs the session in an anyio task group, so an API error
    raised inside the tool-use loop reaches us wrapped in one or more
    ExceptionGroups. Unwrapping it lets a bad key print as one line instead of
    a forty-line traceback.
    """
    if isinstance(err, APIError):
        return err
    for nested in getattr(err, "exceptions", ()):
        if found := _first_api_error(nested):
            return found
    return None


def _explain(err: APIError) -> str:
    if isinstance(err, AuthenticationError):
        return "Authentication failed -- check ANTHROPIC_API_KEY."
    if isinstance(err, RateLimitError):
        return "Rate limited by the Anthropic API. Wait a moment and retry."
    return f"Anthropic API error: {err}"


@contextlib.contextmanager
def _server_log(verbose: bool):
    """Where the server subprocess's stderr goes.

    The MCP server logs every request at INFO, which drowns the transcript.
    Hidden by default, available with --verbose when something breaks.
    """
    if verbose:
        yield sys.stderr
    else:
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            yield devnull


async def ask(question: str, verbose: bool = False) -> int:
    client = AsyncAnthropic()

    # The client constructs fine without credentials and only fails once a
    # request is built -- by which point the error surfaces as a nested
    # TaskGroup traceback. Check up front so a missing key reads as a missing
    # key.
    if not (client.api_key or client.auth_token or client.credentials):
        print(
            "No Anthropic credentials found.\n"
            "Set ANTHROPIC_API_KEY in your environment, then run this again.",
            file=sys.stderr,
        )
        return 2

    # Launch the server with this environment's interpreter rather than `uv`,
    # so the subprocess works the same way regardless of what's on PATH.
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
    )

    try:
        with _server_log(verbose) as errlog:
            async with stdio_client(server, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _converse(client, session, question)
    except Exception as err:
        if (api_error := _first_api_error(err)) is None:
            raise
        print(_explain(api_error), file=sys.stderr)
        return 1


async def _converse(client: AsyncAnthropic, session: ClientSession, question: str) -> int:
    """Run the tool-use loop against a connected MCP session."""
    listed = await session.list_tools()

    print(f"MCP server ready -- tools: {', '.join(t.name for t in listed.tools)}")
    print(f"\n> {question}\n")

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
        tools=[async_mcp_tool(tool, session) for tool in listed.tools],
        # If a safety classifier declines the request, serve it from the
        # recommended fallback model rather than returning the refusal.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    final = None
    async for message in runner:
        final = message

        calls = [b for b in message.content if b.type == "tool_use"]
        if calls:
            # Prose written alongside a tool call is Claude narrating its
            # plan; indent it with the calls it belongs to.
            if narration := _text(message.content):
                print(f"  {narration}")
            for call in calls:
                print(f"  -> {call.name}({_format_args(call.input)})")

        # Cached by the runner -- reading it here does not re-run the tools.
        results = await runner.generate_tool_call_response()
        for block in (results or {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                print(f"     {_summarize_result(block.get('content'))}")

    if final is None:
        print("No response from the model.", file=sys.stderr)
        return 1

    if final.stop_reason == "refusal":
        detail = getattr(final, "stop_details", None)
        reason = getattr(detail, "explanation", None) or "no explanation given"
        print(f"\nClaude declined this request ({reason}).", file=sys.stderr)
        return 1

    print(f"\n{_text(final.content)}")
    print(
        f"\n[{MODEL} | in {final.usage.input_tokens} / "
        f"out {final.usage.output_tokens} tokens]",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask Claude a question about the business database."
    )
    parser.add_argument("question", help="the question to ask, in plain language")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show the server's own log output (hidden by default)",
    )
    args = parser.parse_args()
    return asyncio.run(ask(args.question, verbose=args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
