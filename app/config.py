from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator

INSECURE_DEFAULT_SECRET = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "development" | "test" | "production" — development/test auto-provision
    # a fixed dev@local.test user with no real auth check, so local/CI work
    # never needs real Google OAuth credentials.
    environment: str = "development"

    database_url: str = "postgresql://portfolio:portfolio-dev-password@localhost:5433/portfolio"

    # Shared HMAC key with the frontend's NextAuth jwt.encode/decode override —
    # verifying a session token here must use the exact same secret.
    nextauth_secret: str = INSECURE_DEFAULT_SECRET

    # Vercel frontend origin, for CORS. Not enforced in development.
    frontend_origin: str = "http://localhost:3000"

    # Portfolio-assistant agent (see app/routers/agent.py, app/mcp_server.py).
    # "gemini" first, per an explicit ask — Claude stays available (set to
    # "claude") since app/services/llm/base.py's whole point is to make
    # that a one-line switch, not a code change.
    agent_provider: str = "gemini"
    anthropic_api_key: str = ""
    agent_model: str = "claude-sonnet-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Used only to populate the MCP server's protected-resource metadata —
    # this app doesn't run a real OAuth authorization server (see
    # app/dependencies/mcp_auth.py's docstring for the deliberately minimal
    # auth story). Point these at the deployed backend's own base URL.
    mcp_issuer_url: str = "http://localhost:8000"
    mcp_resource_server_url: str = "http://localhost:8000/mcp"

    # Company-fundamentals data source for the value-investing agent tools
    # (see app/services/fundamentals/). "yahoo" is the only provider today;
    # the FundamentalsProvider seam (app/services/fundamentals/base.py) makes
    # adding another a one-file change, not a rewrite. Results are cached in
    # Postgres once per UTC day per symbol (ticker_fundamentals_cache).
    fundamentals_provider: str = "yahoo"

    @model_validator(mode="after")
    def _require_real_secret_outside_dev(self) -> "Settings":
        if self.environment not in ("development", "test") and (
            not self.nextauth_secret or self.nextauth_secret == INSECURE_DEFAULT_SECRET
        ):
            raise ValueError(
                "NEXTAUTH_SECRET must be set to a real secret outside development/test — "
                "refusing to start with the insecure default."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
