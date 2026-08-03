"""Tests for the demo client's transcript formatting.

The Claude call itself needs an API key and is not exercised here. These cover
the pure functions that turn tool output into the transcript -- in particular
that a refusal is reported as a refusal rather than being flattened into an
ordinary-looking result.
"""

import json
import sys

import httpx
from anthropic import APIError, AuthenticationError

from demo import _explain, _first_api_error, _format_args, _summarize_result

if sys.version_info < (3, 11):  # BaseExceptionGroup is a builtin from 3.11 on
    from exceptiongroup import BaseExceptionGroup


def as_content(payload: dict) -> list[dict]:
    """Wrap a tool's JSON response the way MCP delivers it."""
    return [{"type": "text", "text": json.dumps(payload)}]


class TestSummarizeResult:
    def test_refusal_is_labelled_as_one(self):
        summary = _summarize_result(
            as_content({"error": "Access to payment_methods is restricted."})
        )
        assert summary == "REFUSED -- Access to payment_methods is restricted."

    def test_row_count_is_reported(self):
        assert _summarize_result(as_content({"row_count": 4})) == "4 row(s)"

    def test_truncation_is_flagged(self):
        summary = _summarize_result(as_content({"row_count": 500, "truncated": True}))
        assert summary == "500 row(s) (truncated)"

    def test_restricted_tables_are_marked_in_the_listing(self):
        summary = _summarize_result(
            as_content({"tables": [
                {"name": "customers", "accessible": True},
                {"name": "payment_methods", "accessible": False},
            ]})
        )
        assert summary == "customers, payment_methods [restricted]"

    def test_column_listing_names_the_table(self):
        summary = _summarize_result(
            as_content({"table": "customers", "columns": [{}, {}, {}]})
        )
        assert summary == "customers: 3 columns"

    def test_non_json_output_does_not_crash(self):
        assert _summarize_result([{"type": "text", "text": "boom"}]) == "boom"

    def test_plain_string_content_is_accepted(self):
        assert _summarize_result("boom") == "boom"

    def test_empty_content_does_not_crash(self):
        assert _summarize_result(None) == ""


def an_auth_error() -> AuthenticationError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request)
    return AuthenticationError("invalid x-api-key", response=response, body=None)


class TestApiErrorUnwrapping:
    """A bad key should print one line, not a nested TaskGroup traceback."""

    def test_finds_a_bare_error(self):
        err = an_auth_error()
        assert _first_api_error(err) is err

    def test_finds_an_error_nested_in_exception_groups(self):
        err = an_auth_error()
        wrapped = BaseExceptionGroup(
            "outer", [BaseExceptionGroup("inner", [err])]
        )
        assert _first_api_error(wrapped) is err

    def test_returns_none_for_unrelated_errors(self):
        assert _first_api_error(ValueError("nope")) is None
        assert _first_api_error(BaseExceptionGroup("g", [ValueError("nope")])) is None

    def test_auth_errors_get_an_actionable_message(self):
        assert "ANTHROPIC_API_KEY" in _explain(an_auth_error())

    def test_other_errors_fall_back_to_the_api_message(self):
        assert "Anthropic API error" in _explain(APIError("boom", request=None, body=None))


class TestFormatArgs:
    def test_whitespace_is_collapsed_onto_one_line(self):
        assert _format_args({"sql": "SELECT *\n  FROM customers"}) == (
            "sql=SELECT * FROM customers"
        )

    def test_long_values_are_truncated(self):
        formatted = _format_args({"sql": "SELECT " + "x" * 200})
        assert formatted.endswith("...")
        assert len(formatted) <= 98  # "sql=" plus the 90-char cap

    def test_no_arguments_renders_empty(self):
        assert _format_args({}) == ""
