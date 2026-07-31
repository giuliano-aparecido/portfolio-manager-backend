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
