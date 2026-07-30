from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "development" | "test" | "production" — development/test auto-provision
    # a fixed dev@local.test user with no real auth check, mirroring the
    # original Next.js app's lib/auth-helper.ts behavior exactly.
    environment: str = "development"

    database_url: str = "postgresql://portfolio:portfolio-dev-password@localhost:5433/portfolio"

    # Shared HMAC key with the frontend's NextAuth jwt.encode/decode override —
    # verifying a session token here must use the exact same secret.
    nextauth_secret: str = "dev-only-insecure-secret-change-me"

    # Vercel frontend origin, for CORS. Not enforced in development.
    frontend_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
