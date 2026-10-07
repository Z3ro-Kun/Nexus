import pytest

from app.core.config import Settings


def test_defaults_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NEXUS_ENVIRONMENT", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_name == "NEXUS"
    assert settings.environment == "development"
    assert settings.postgres_host == "localhost"
    assert settings.postgres_port == 5432
    assert settings.postgres_password.get_secret_value() == ""


def test_reads_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ENVIRONMENT", "production")
    monkeypatch.setenv("NEXUS_CORS_ORIGINS", '["https://nexus.example"]')
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_USER", "svc")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")
    monkeypatch.setenv("POSTGRES_DB", "nexus_prod")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    url = settings.database_url

    assert settings.environment == "production"
    assert settings.cors_origins == ["https://nexus.example"]
    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.port, url.username, url.database) == (
        "db.internal",
        6543,
        "svc",
        "nexus_prod",
    )
    assert url.password == "s3cret"


def test_password_is_not_exposed_when_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert "s3cret" not in repr(settings)
    assert "s3cret" not in str(settings.database_url)


def test_rejects_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ENVIRONMENT", "staging-ish")

    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]
