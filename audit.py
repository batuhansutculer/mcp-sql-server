"""Audit log for every query the agent attempts.

Writes one JSON object per line to `audit.log`. Refusals are recorded as well
as successes -- an access-control layer you cannot review after the fact is
hard to trust, and "show me what the agent actually ran" is the first question
anyone asks when an AI system touches customer or financial data.

JSON Lines rather than free text so the log can be queried directly:

    cat audit.log | jq 'select(.decision == "denied")'
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).parent / "audit.log"


def record(
    tool: str,
    decision: str,
    sql: str | None = None,
    reason: str | None = None,
    row_count: int | None = None,
    log_path: Path | None = None,
) -> dict:
    """Append one audit entry. Returns the entry, for tests to assert on.

    `log_path` resolves at call time rather than as a default argument, so
    tests can redirect the log without writing to the real one.
    """
    path = log_path if log_path is not None else LOG_PATH
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "decision": decision,
    }
    if sql is not None:
        entry["sql"] = " ".join(sql.split())
    if reason is not None:
        entry["reason"] = reason
    if row_count is not None:
        entry["row_count"] = row_count

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry
