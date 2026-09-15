from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from skillwright_mcp.config import Settings
from skillwright_mcp.migrate import (
    _migration_marker_config,
    _runtime_database_config,
    _runtime_database_password,
    _write_migration_marker,
)


class FakeSecretsClient:
    def __init__(self, secret_string: str | None = None) -> None:
        self.secret_string = secret_string
        self.put_calls: list[dict[str, Any]] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        if self.secret_string is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
                "GetSecretValue",
            )
        return {"SecretString": self.secret_string}

    def put_secret_value(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)


class FakeSSMClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_parameter(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _migration_settings(**kwargs: Any) -> Settings:
    return Settings(
        migration_runtime_database_user="skillwright_app",
        migration_runtime_database_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:db",
        **kwargs,
    )


def test_runtime_database_password_initializes_empty_managed_secret() -> None:
    client = FakeSecretsClient()

    credentials = _runtime_database_password(_migration_settings(), secrets_client=client)

    assert credentials is not None
    user, password = credentials
    assert user == "skillwright_app"
    assert len(password) == 64
    assert all(character in "0123456789abcdef" for character in password)
    assert len(client.put_calls) == 1
    payload = json.loads(client.put_calls[0]["SecretString"])
    assert payload == {"username": user, "password": password}


def test_runtime_database_password_reuses_existing_secret() -> None:
    client = FakeSecretsClient(
        json.dumps({"username": "skillwright_app", "password": "existing-password"})
    )

    credentials = _runtime_database_password(_migration_settings(), secrets_client=client)

    assert credentials == ("skillwright_app", "existing-password")
    assert client.put_calls == []


def test_runtime_database_password_rejects_mismatched_secret_username() -> None:
    client = FakeSecretsClient(json.dumps({"username": "other", "password": "secret"}))

    with pytest.raises(RuntimeError, match="runtime database secret is invalid"):
        _runtime_database_password(_migration_settings(), secrets_client=client)


@pytest.mark.parametrize("user", ["Bad-Role", "9role", "role with space", "a" * 64])
def test_runtime_database_config_rejects_unsafe_role_names(user: str) -> None:
    settings = Settings(
        migration_runtime_database_user=user,
        migration_runtime_database_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:db",
    )

    with pytest.raises(RuntimeError, match="runtime database user is invalid"):
        _runtime_database_config(settings)


def test_runtime_database_config_requires_both_fields() -> None:
    settings = Settings(migration_runtime_database_user="skillwright_app")

    with pytest.raises(RuntimeError, match="must be configured together"):
        _runtime_database_config(settings)


def test_migration_marker_writes_only_after_complete_configuration() -> None:
    client = FakeSSMClient()
    settings = Settings(
        migration_marker_parameter="/skillwright/prod/migrated-image",
        release_image_tag="git-deadbeef",
    )

    _write_migration_marker(settings, ssm_client=client)

    assert client.calls == [
        {
            "Name": "/skillwright/prod/migrated-image",
            "Value": "git-deadbeef",
            "Type": "String",
            "Overwrite": True,
        }
    ]


def test_migration_marker_rejects_latest_tag() -> None:
    settings = Settings(migration_marker_parameter="/marker", release_image_tag="latest")

    with pytest.raises(RuntimeError, match="immutable non-latest"):
        _migration_marker_config(settings)
