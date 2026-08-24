"""Reference LLMProvider implementation, using the Claude Messages API's
native tool-use + streaming support. Translates the neutral Turn history
(see base.py) into Anthropic's own wire shape (tool_use/tool_result content
blocks) at call time.
"""

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.config import get_settings
from app.services.llm.base import AgentEvent, AssistantTurn, LLMProvider, ToolCallRequest, ToolResultsTurn, Turn, UserTurn

MAX_TOKENS = 4096


def _to_claude_messages(history: list[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history:
        if isinstance(turn, UserTurn):
            messages.append({"role": "user", "content": turn.text})
        elif isinstance(turn, AssistantTurn):
            content: list[dict[str, Any]] = []
            if turn.text:
                content.append({"type": "text", "text": turn.text})
            content.extend(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input} for tc in turn.tool_calls
            )
            messages.append({"role": "assistant", "content": content})
        elif isinstance(turn, ToolResultsTurn):
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.tool_call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in turn.results
                    ],
                }
            )
    return messages


class ClaudeProvider(LLMProvider):
    def __init__(self, client: anthropic.AsyncAnthropic | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = model or settings.agent_model

    async def stream_turn(
        self,
        history: list[Turn],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[AgentEvent]:
        claude_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools
        ]
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=_to_claude_messages(history),
            tools=claude_tools,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    yield AgentEvent(type="text_delta", text=event.delta.text)
            final_message = await stream.get_final_message()

        for block in final_message.content:
            if block.type == "tool_use":
                yield AgentEvent(
                    type="tool_call_start",
                    tool_call=ToolCallRequest(id=block.id, name=block.name, input=block.input),
                )

        yield AgentEvent(type="turn_end")
