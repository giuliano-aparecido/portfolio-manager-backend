"""Reference LLMProvider implementation, using the Claude Messages API's
native tool-use + streaming support.

`messages` is passed straight through in Anthropic's own wire shape (a
`tool_result` content block for tool responses, etc.) rather than translated
through a provider-neutral message format — a second provider would need to
translate its own shape at its call site in the agent loop. Building a full
provider-neutral message IR isn't justified when only one provider ships;
see base.py's docstring.
"""

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.config import get_settings
from app.services.llm.base import AgentEvent, LLMProvider, ToolCallRequest

MAX_TOKENS = 4096


class ClaudeProvider(LLMProvider):
    def __init__(self, client: anthropic.AsyncAnthropic | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = model or settings.agent_model

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
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
            messages=messages,
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
