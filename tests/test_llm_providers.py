from app.config import Settings
from app.routers.agent import get_llm_provider
from app.services.llm.base import AssistantTurn, ToolCallRequest, ToolResult, ToolResultsTurn, UserTurn
from app.services.llm.claude_provider import ClaudeProvider, _to_claude_messages
from app.services.llm.gemini_provider import GeminiProvider, _to_gemini_contents


class TestToClaudeMessages:
    def test_user_turn_becomes_a_plain_user_message(self) -> None:
        messages = _to_claude_messages([UserTurn(text="What do I hold?")])
        assert messages == [{"role": "user", "content": "What do I hold?"}]

    def test_assistant_turn_with_tool_calls_becomes_text_and_tool_use_blocks(self) -> None:
        messages = _to_claude_messages(
            [AssistantTurn(text="Checking...", tool_calls=[ToolCallRequest(id="tc1", name="get_holdings", input={})])]
        )
        assert messages == [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking..."},
                    {"type": "tool_use", "id": "tc1", "name": "get_holdings", "input": {}},
                ],
            }
        ]

    def test_assistant_turn_with_no_text_omits_the_text_block(self) -> None:
        messages = _to_claude_messages(
            [AssistantTurn(text="", tool_calls=[ToolCallRequest(id="tc1", name="get_holdings", input={})])]
        )
        assert messages[0]["content"] == [{"type": "tool_use", "id": "tc1", "name": "get_holdings", "input": {}}]

    def test_tool_results_turn_becomes_a_user_message_with_tool_result_blocks(self) -> None:
        messages = _to_claude_messages(
            [ToolResultsTurn(results=[ToolResult(tool_call_id="tc1", name="get_holdings", content="{}", is_error=False)])]
        )
        assert messages == [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc1", "content": "{}", "is_error": False}],
            }
        ]


class TestToGeminiContents:
    def test_user_turn_becomes_a_user_content_with_a_text_part(self) -> None:
        contents = _to_gemini_contents([UserTurn(text="What do I hold?")])
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "What do I hold?"

    def test_assistant_turn_becomes_model_content_with_text_and_function_call_parts(self) -> None:
        contents = _to_gemini_contents(
            [AssistantTurn(text="Checking...", tool_calls=[ToolCallRequest(id="tc1", name="get_holdings", input={"refresh": True})])]
        )
        assert contents[0].role == "model"
        assert contents[0].parts[0].text == "Checking..."
        function_call = contents[0].parts[1].function_call
        assert function_call.id == "tc1"
        assert function_call.name == "get_holdings"
        assert function_call.args == {"refresh": True}

    def test_tool_results_turn_becomes_user_content_with_function_response_parts(self) -> None:
        contents = _to_gemini_contents(
            [ToolResultsTurn(results=[ToolResult(tool_call_id="tc1", name="get_holdings", content='{"shares": 10}', is_error=False)])]
        )
        assert contents[0].role == "user"
        function_response = contents[0].parts[0].function_response
        assert function_response.id == "tc1"
        assert function_response.name == "get_holdings"
        # JSON tool output is unwrapped back into structured data, not left as a string.
        assert function_response.response == {"result": {"shares": 10}}

    def test_tool_error_result_is_wrapped_under_an_error_key(self) -> None:
        contents = _to_gemini_contents(
            [ToolResultsTurn(results=[ToolResult(tool_call_id="tc1", name="compute_whatif", content="not tracked", is_error=True)])]
        )
        function_response = contents[0].parts[0].function_response
        assert function_response.response == {"error": "not tracked"}


class TestGetLlmProvider:
    def test_defaults_to_gemini(self, monkeypatch) -> None:
        monkeypatch.setattr("app.routers.agent.get_settings", lambda: Settings(agent_provider="gemini"))
        assert isinstance(get_llm_provider(), GeminiProvider)

    def test_selects_claude_when_configured(self, monkeypatch) -> None:
        monkeypatch.setattr("app.routers.agent.get_settings", lambda: Settings(agent_provider="claude"))
        assert isinstance(get_llm_provider(), ClaudeProvider)
