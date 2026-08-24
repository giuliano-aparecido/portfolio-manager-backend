"""Provider-agnostic seam for the agent loop (app/routers/agent.py). Plain
JSON-Schema tool defs and role-based message dicts — the same shape every
major provider's tool-calling API uses — so a second provider could be
added later without touching the loop. Only ClaudeProvider ships in v1 (see
claude_provider.py); no unused stub implementations for other providers.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AgentEvent:
    type: Literal["text_delta", "tool_call_start", "turn_end", "error"]
    text: str | None = None
    tool_call: ToolCallRequest | None = None
    error_message: str | None = None


class LLMProvider(ABC):
    @abstractmethod
    def stream_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[AgentEvent]:
        """Run one model turn. The caller is responsible for executing any
        tool calls the turn ends with and appending their results to
        `messages` before calling this again for the next turn.
        """
        raise NotImplementedError
