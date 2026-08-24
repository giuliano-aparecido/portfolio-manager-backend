"""POST /agent/ask — the portfolio assistant chat endpoint.

Runs a tool-calling loop against an LLMProvider, dispatching tool calls
in-process through the same mcp_server object that serves external MCP
clients (mcp_server.list_tools() / .call_tool(...)) rather than opening an
HTTP loopback to its own /mcp mount — see mcp_server.py and
agent_context.py for why. The frontend resends the whole conversation each
turn; nothing is persisted server-side.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.dependencies.auth import get_authenticated_user_id
from app.mcp_server import mcp_server
from app.rate_limiter import limiter
from app.schemas.agent import AskRequest
from app.services.agent_context import set_current_user_id
from app.services.llm.base import LLMProvider, ToolCallRequest
from app.services.llm.claude_provider import ClaudeProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPT = (
    "You are a portfolio assistant answering questions about one person's "
    "real, multi-currency investment portfolio. You have tools for holdings, "
    "allocation, per-ticker detail, passive investments, and a hypothetical "
    "buy/sell simulator. Never compute or estimate a financial figure "
    "yourself — always call a tool for any concrete number, and name which "
    "tool(s) informed your answer. If a tool result includes price errors "
    "or an unauthorized ticker, say so rather than guessing the missing "
    "data. All monetary figures are in CHF unless stated otherwise."
)


def get_llm_provider() -> LLMProvider:
    return ClaudeProvider()


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/ask")
@limiter.limit("6/minute")
async def ask(
    request: Request,
    body: AskRequest,
    user_id: str = Depends(get_authenticated_user_id),
    provider: LLMProvider = Depends(get_llm_provider),
) -> StreamingResponse:
    async def event_stream() -> AsyncIterator[str]:
        set_current_user_id(user_id)

        messages: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in body.messages]
        mcp_tools = await mcp_server.list_tools()
        tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in mcp_tools]

        for _ in range(MAX_TOOL_ITERATIONS):
            text_parts: list[str] = []
            tool_calls: list[ToolCallRequest] = []

            try:
                async for event in provider.stream_turn(messages, tools, system=SYSTEM_PROMPT):
                    if event.type == "text_delta" and event.text:
                        text_parts.append(event.text)
                        yield _sse_frame("token", {"text": event.text})
                    elif event.type == "tool_call_start" and event.tool_call:
                        tool_calls.append(event.tool_call)
                        yield _sse_frame("tool_call", {"name": event.tool_call.name, "input": event.tool_call.input})
                    elif event.type == "error":
                        yield _sse_frame("error", {"message": event.error_message or "LLM provider error"})
                        return
            except Exception:  # noqa: BLE001 — never leak internal error details to the stream
                logger.exception("Unexpected error during agent turn")
                yield _sse_frame("error", {"message": "The assistant hit an unexpected error."})
                return

            if not tool_calls:
                yield _sse_frame("done", {})
                return

            assistant_content: list[dict[str, Any]] = []
            full_text = "".join(text_parts)
            if full_text:
                assistant_content.append({"type": "text", "text": full_text})
            assistant_content.extend(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input} for tc in tool_calls
            )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_result_blocks: list[dict[str, Any]] = []
            for tc in tool_calls:
                result = await mcp_server.call_tool(tc.name, tc.input)
                result_text = "".join(getattr(c, "text", "") for c in result.content)
                yield _sse_frame("tool_result", {"name": tc.name})
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result_text,
                        "is_error": result.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_result_blocks})

        yield _sse_frame("error", {"message": "The assistant took too many steps to answer — try rephrasing."})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
