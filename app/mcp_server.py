"""The MCP (Model Context Protocol) server: exposes the same tools from
app/services/agent_tools.py to any MCP client — the frontend's /agent/ask
loop (in-process, via list_tools()/call_tool() below) and, for anyone who
wants to connect directly, external clients like Claude Desktop.

Mounted into the FastAPI app in app/main.py via app.mount("/mcp", ...); its
session_manager must be entered in that app's lifespan or mounted requests
fail (see main.py).
"""

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

from app.config import get_settings
from app.dependencies.mcp_auth import JwtTokenVerifier
from app.services.agent_tools import register_tools


def _build_mcp_server() -> MCPServer:
    settings = get_settings()
    server = MCPServer(
        "portfolio-manager",
        instructions=(
            "Tools for answering questions about one person's real investment "
            "portfolio (multi-currency, FIFO cost basis). All figures are in "
            "CHF unless a tool result says otherwise. Never compute financial "
            "figures yourself — always call a tool for any concrete number."
        ),
        token_verifier=JwtTokenVerifier(),
        auth=AuthSettings(
            issuer_url=settings.mcp_issuer_url,
            resource_server_url=settings.mcp_resource_server_url,
            required_scopes=["read"],
        ),
    )
    register_tools(server)
    return server


mcp_server = _build_mcp_server()
