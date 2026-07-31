import pytest

from app.config import Settings


class TestSettingsSecretGuard:
    def test_raises_when_production_has_no_secret(self) -> None:
        with pytest.raises(ValueError, match="NEXTAUTH_SECRET"):
            Settings(environment="production", nextauth_secret="", _env_file=None)

    def test_raises_when_production_has_insecure_default_secret(self) -> None:
        with pytest.raises(ValueError, match="NEXTAUTH_SECRET"):
            Settings(
                environment="production",
                nextauth_secret="dev-only-insecure-secret-change-me",
                _env_file=None,
            )

    def test_allows_production_with_a_real_secret(self) -> None:
        settings = Settings(environment="production", nextauth_secret="a-real-secret", _env_file=None)
        assert settings.nextauth_secret == "a-real-secret"

    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_allows_dev_and_test_without_a_real_secret(self, environment: str) -> None:
        settings = Settings(environment=environment, nextauth_secret="", _env_file=None)
        assert settings.nextauth_secret == ""
