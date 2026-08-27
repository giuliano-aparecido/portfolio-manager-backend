"""Carries the authenticated user_id into an agent tool call regardless of
which transport invoked it.

Tool functions in agent_tools.py open their own short-lived DB session and
must know *who* is calling — but they run under two different auth systems:

- A real MCP client (Claude Desktop, etc.) goes through mcp_server's own
  bearer-token auth middleware, which exposes the verified identity via
  mcp's get_access_token() (a contextvar the MCP SDK manages itself).
- The internal /agent/ask loop calls mcp_server.call_tool(...) directly
  in-process (see PROJECT.md's agent-chat section for why: no HTTP
  loopback, no second auth mechanism) — it never goes through that
  middleware, so get_access_token() would return None there. It sets this
  module's contextvar instead, around each tool call, using the user_id it
  already has from get_authenticated_user_id.

resolve_user_id() checks the MCP path first, falling back to this one, so
every tool function has a single call to make regardless of caller.
"""

from contextvars import ContextVar

from app.exceptions import UnauthorizedError

_current_user_id: ContextVar[str | None] = ContextVar("agent_current_user_id", default=None)


def set_current_user_id(user_id: str) -> None:
    _current_user_id.set(user_id)


def resolve_user_id() -> str:
    from mcp.server.auth.middleware.auth_context import get_access_token

    access_token = get_access_token()
    if access_token is not None and access_token.subject:
        return access_token.subject

    user_id = _current_user_id.get()
    if user_id is None:
        raise UnauthorizedError()
    return user_id
