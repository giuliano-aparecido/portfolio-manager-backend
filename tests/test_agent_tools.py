"""compute_whatif's input validation, tested by calling the registered MCP
tool directly (mcp_server.call_tool) rather than through the HTTP route —
this doesn't need Postgres or an authenticated context, since the
validation in question runs before _tool_context() ever resolves a user
or opens a session (see app/services/agent_tools.py).
"""

import json
import math

import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from app.mcp_server import mcp_server


def _result_text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


class TestComputeWhatifInputValidation:
    @pytest.mark.parametrize(
        ("kwargs", "expected_substring"),
        [
            ({"quantity": -5}, "quantity must be a positive number"),
            ({"quantity": 0}, "quantity must be a positive number"),
            # NaN/inf both fail every ordinary `<= 0` comparison too, so
            # they'd silently slip past a naive non-positive check straight
            # into the simulation — the validation guards against both
            # explicitly (see agent_tools.py).
            ({"quantity": math.nan}, "quantity must be a positive number"),
            ({"quantity": math.inf}, "quantity must be a positive number"),
            ({"quantity": 5, "price_per_share": -10}, "price_per_share must be a positive number"),
            ({"quantity": 5, "price_per_share": math.inf}, "price_per_share must be a positive number"),
        ],
    )
    async def test_rejects_invalid_quantity_or_price(self, kwargs: dict, expected_substring: str) -> None:
        result = await mcp_server.call_tool("compute_whatif", {"ticker": "AAPL", "action": "BUY", **kwargs})
        assert result.is_error is False
        assert expected_substring in json.loads(_result_text(result))["error"]

    async def test_omitted_price_per_share_is_not_rejected_by_the_price_check(self) -> None:
        # price_per_share is optional (falls back to the live quote) - a
        # missing value must not trip the "must be positive" validation.
        # With valid input, execution proceeds past validation into
        # _tool_context() and fails there instead (no MCP/agent_context
        # auth set up in this test, no Postgres in this environment) -
        # confirmed empirically that this specific, documented failure mode
        # surfaces as UnexpectedToolError wrapping an UnauthorizedError, not
        # a graceful {"error": ...} result - narrowed to that exception type
        # rather than a bare Exception so this test doesn't also pass for an
        # unrelated crash.
        try:
            result = await mcp_server.call_tool("compute_whatif", {"ticker": "AAPL", "action": "BUY", "quantity": 5})
            text = _result_text(result)
        except UnexpectedToolError as exc:
            text = str(exc)
        assert "quantity must be a positive number" not in text
        assert "price_per_share must be a positive number" not in text
