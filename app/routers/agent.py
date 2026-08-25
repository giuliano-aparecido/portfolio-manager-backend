"""POST /agent/ask — the portfolio assistant chat endpoint, streamed as SSE."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.dependencies.auth import get_authenticated_user_id
from app.exceptions import AppError
from app.mcp_server import mcp_server
from app.rate_limiter import limiter
from app.schemas.agent import AskRequest
from app.services.agent_context import set_current_user_id
from app.services.llm.base import AssistantTurn, LLMProvider, ToolCallRequest, ToolResult, ToolResultsTurn, Turn, UserTurn
from app.services.llm.claude_provider import ClaudeProvider
from app.services.llm.gemini_provider import GeminiProvider

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


class _StreamStopped(Exception):
    """Raised by a turn/dispatch helper once it has already yielded its own
    error frame, so event_stream knows to stop without yielding a second one.
    """


def get_llm_provider() -> LLMProvider:
    settings = get_settings()
    if settings.agent_provider == "claude":
        return ClaudeProvider()
    if settings.agent_provider == "gemini":
        return GeminiProvider()
    raise AppError(500, f"Unknown AGENT_PROVIDER: {settings.agent_provider!r}")


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_llm_turn(
    provider: LLMProvider,
    history: list[Turn],
    tools: list[dict[str, Any]],
    tool_calls: list[ToolCallRequest],
) -> AsyncIterator[str]:
    """Streams one provider turn as SSE frames, appending any requested tool
    calls into `tool_calls` (mutated in place) as they arrive, and appending
    the resulting AssistantTurn to `history` once the turn ends cleanly.
    """
    text_parts: list[str] = []
    try:
        async for event in provider.stream_turn(history, tools, system=SYSTEM_PROMPT):
            if event.type == "text_delta" and event.text:
                text_parts.append(event.text)
                yield _sse_frame("token", {"text": event.text})
            elif event.type == "tool_call_start" and event.tool_call:
                tool_calls.append(event.tool_call)
                yield _sse_frame("tool_call", {"name": event.tool_call.name, "input": event.tool_call.input})
            elif event.type == "error":
                yield _sse_frame("error", {"message": event.error_message or "LLM provider error"})
                raise _StreamStopped
            elif event.type == "turn_end":
                # Explicit stop rather than relying only on the generator
                # running out, so a provider bug that kept yielding after
                # turn_end can't silently slip through.
                break
    except _StreamStopped:
        raise
    except Exception:  # noqa: BLE001 — never leak internal error details to the stream
        logger.exception("Unexpected error during agent turn")
        yield _sse_frame("error", {"message": "The assistant hit an unexpected error."})
        raise _StreamStopped from None

    history.append(AssistantTurn(text="".join(text_parts), tool_calls=tool_calls))


async def _dispatch_tool_calls(tool_calls: list[ToolCallRequest], results: list[ToolResult]) -> AsyncIterator[str]:
    """Runs each requested tool call in-process against mcp_server, streaming
    a tool_result frame per call and appending to `results` (mutated in
    place) as they complete.
    """
    try:
        for tc in tool_calls:
            result = await mcp_server.call_tool(tc.name, tc.input)
            result_text = "".join(getattr(c, "text", "") for c in result.content)
            yield _sse_frame("tool_result", {"name": tc.name})
            results.append(ToolResult(tool_call_id=tc.id, name=tc.name, content=result_text, is_error=result.is_error))
    except Exception:  # noqa: BLE001 — a tool can raise directly (e.g. compute_whatif's live
        # price/FX fetch); unlike MCP's transport layer, in-process dispatch never converts
        # that into an is_error result, so it needs its own catch here.
        logger.exception("Unexpected error during tool dispatch")
        yield _sse_frame("error", {"message": "The assistant hit an unexpected error running a tool."})
        raise _StreamStopped from None


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

        history: list[Turn] = [UserTurn(text=m.content) if m.role == "user" else AssistantTurn(text=m.content) for m in body.messages]
        mcp_tools = await mcp_server.list_tools()
        tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in mcp_tools]

        for _ in range(MAX_TOOL_ITERATIONS):
            tool_calls: list[ToolCallRequest] = []
            try:
                async for frame in _stream_llm_turn(provider, history, tools, tool_calls):
                    yield frame
            except _StreamStopped:
                return

            if not tool_calls:
                yield _sse_frame("done", {})
                return

            results: list[ToolResult] = []
            try:
                async for frame in _dispatch_tool_calls(tool_calls, results):
                    yield frame
            except _StreamStopped:
                return
            history.append(ToolResultsTurn(results=results))

        yield _sse_frame("error", {"message": "The assistant took too many steps to answer — try rephrasing."})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
