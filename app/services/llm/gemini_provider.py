"""Second LLMProvider implementation, using the Gemini API's native
function-calling + streaming support. Translates the neutral Turn history
(see base.py) into Gemini's own wire shape (Content/Part objects with
function_call/function_response parts) at call time — structurally
different from Claude's (see claude_provider.py) despite both representing
the same tool-calling turn.

Streaming semantics: each yielded chunk carries only its own incremental
parts (confirmed against the installed google-genai SDK's own docstring
example — concatenating chunk.text across the loop produces the full
non-repeating response), so text/function_call parts are only ever
emitted once each, in the chunk they first appear.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from app.config import get_settings
from app.services.llm.base import AgentEvent, AssistantTurn, LLMProvider, ToolCallRequest, ToolResultsTurn, Turn, UserTurn


def _tool_result_response(content: str, *, is_error: bool) -> dict[str, Any]:
    # FunctionResponse.response is a dict, not a string — our tool results
    # are JSON text (see agent_tools.py), so unwrap it back into structured
    # data where possible rather than handing Gemini a JSON-shaped string.
    key = "error" if is_error else "result"
    try:
        return {key: json.loads(content)}
    except (json.JSONDecodeError, TypeError):
        return {key: content}


def _to_gemini_contents(history: list[Turn]) -> list[types.Content]:
    contents: list[types.Content] = []
    for turn in history:
        if isinstance(turn, UserTurn):
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=turn.text)]))
        elif isinstance(turn, AssistantTurn):
            parts: list[types.Part] = []
            if turn.text:
                parts.append(types.Part.from_text(text=turn.text))
            parts.extend(
                types.Part(function_call=types.FunctionCall(id=tc.id, name=tc.name, args=tc.input))
                for tc in turn.tool_calls
            )
            contents.append(types.Content(role="model", parts=parts))
        elif isinstance(turn, ToolResultsTurn):
            parts = [
                types.Part(
                    function_response=types.FunctionResponse(
                        id=r.tool_call_id, name=r.name, response=_tool_result_response(r.content, is_error=r.is_error)
                    )
                )
                for r in turn.results
            ]
            contents.append(types.Content(role="user", parts=parts))
    return contents


class GeminiProvider(LLMProvider):
    def __init__(self, client: genai.Client | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._client = client or genai.Client(api_key=settings.gemini_api_key)
        self._model = model or settings.gemini_model

    async def stream_turn(
        self,
        history: list[Turn],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[AgentEvent]:
        gemini_tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"]
                    )
                    for t in tools
                ]
            )
        ]
        config = types.GenerateContentConfig(system_instruction=system, tools=gemini_tools)

        call_count = 0
        hit_max_tokens = False
        stream = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=_to_gemini_contents(history),
            config=config,
        )
        async for chunk in stream:
            candidates = chunk.candidates or []
            if not candidates:
                continue
            if candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
                hit_max_tokens = True
            if candidates[0].content is None or candidates[0].content.parts is None:
                continue
            for part in candidates[0].content.parts:
                if part.text:
                    yield AgentEvent(type="text_delta", text=part.text)
                elif part.function_call is not None:
                    fc = part.function_call
                    # Gemini function calls aren't always given an id — one
                    # is only needed to label our own tool_call SSE frame,
                    # not to correlate with Gemini itself (unlike Claude).
                    call_id = fc.id or f"call_{call_count}"
                    call_count += 1
                    yield AgentEvent(
                        type="tool_call_start",
                        tool_call=ToolCallRequest(id=call_id, name=fc.name or "", input=dict(fc.args or {})),
                    )

        if hit_max_tokens:
            # Silent truncation is worse than an explicit error — the
            # partial answer already streamed stays on screen, but the
            # caller needs to know it's incomplete rather than treating
            # this like a clean turn end.
            yield AgentEvent(type="error", error_message="The response was cut off for being too long.")
            return

        yield AgentEvent(type="turn_end")
