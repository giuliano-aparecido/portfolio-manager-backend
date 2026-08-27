"""Provider-agnostic seam for the agent loop (app/routers/agent.py).

Conversation history is a list of neutral Turn objects, not either
provider's own wire shape — Claude (content blocks with tool_use/
tool_result) and Gemini (Content/Part objects with function_call/
function_response) structure a tool-calling turn differently enough that a
shared dict shape would just be one provider's shape with the other
provider translating out of it. Each provider's stream_turn() translates
this neutral history into its own request shape internally; the router
only ever builds/appends Turn objects.

Tool defs stay plain JSON Schema (tools: list[dict] with name/description/
input_schema) since that much genuinely is shared — MCP tool metadata is
already in that shape (see mcp_server.py's list_tools()).
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class UserTurn:
    text: str


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)


@dataclass
class ToolResultsTurn:
    results: list[ToolResult]


Turn = UserTurn | AssistantTurn | ToolResultsTurn


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
        history: list[Turn],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[AgentEvent]:
        """Run one model turn over the given history. The caller executes
        any tool calls the turn ends with and appends an AssistantTurn (with
        those tool_calls) followed by a ToolResultsTurn before calling this
        again for the next turn.
        """
        raise NotImplementedError
