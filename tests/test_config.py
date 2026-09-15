from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from skillwright_mcp.config import Settings


def test_database_components_build_encoded_asyncpg_url_without_revealing_password() -> None:
    password = "p@ss word:/?#[]"
    settings = Settings(
        database_host="db.internal.example",
        database_port=5433,
        database_name="skill wright",
        database_user="app user",
        database_password=password,
    )

    assert settings.resolved_database_url() == (
        "postgresql+asyncpg://app user:p%40ss word%3A%2F%3F%23%5B%5D@"
        "db.internal.example:5433/skill wright"
    )
    parsed = make_url(settings.resolved_database_url())
    assert parsed.username == "app user"
    assert parsed.password == password
    assert parsed.database == "skill wright"
    assert password not in repr(settings)


def test_database_components_require_injected_password() -> None:
    settings = Settings(database_host="db.internal.example")

    with pytest.raises(ValueError, match="database_password is required"):
        settings.resolved_database_url()


def test_database_components_can_require_tls() -> None:
    settings = Settings(
        database_host="db.internal.example",
        database_password="secret",
        database_ssl="require",
    )

    assert settings.resolved_database_connect_args() == {"ssl": "require"}


def test_database_ssl_does_not_rewrite_explicit_database_url() -> None:
    explicit_url = "postgresql+asyncpg://user:password@localhost/example"
    settings = Settings(database_url=explicit_url, database_ssl="require")

    assert settings.resolved_database_url() == explicit_url
    assert settings.resolved_database_connect_args() == {}


def test_verify_full_database_ssl_requires_root_certificate() -> None:
    settings = Settings(
        database_host="db.internal.example",
        database_password="secret",
        database_ssl="verify-full",
    )

    with pytest.raises(ValueError, match="database_ssl_root_cert is required"):
        settings.resolved_database_connect_args()
