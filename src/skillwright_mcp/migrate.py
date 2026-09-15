from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import Any

import boto3  # type: ignore[import-untyped]
from alembic.config import Config
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

from .config import Settings

_DATABASE_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _runtime_database_config(settings: Settings) -> tuple[str, str] | None:
    user = settings.migration_runtime_database_user
    secret_arn = settings.migration_runtime_database_secret_arn
    if user is None and secret_arn is None:
        return None
    if user is None or secret_arn is None:
        raise RuntimeError(
            "migration runtime database user and secret ARN must be configured together"
        )
    if _DATABASE_ROLE_RE.fullmatch(user) is None:
        raise RuntimeError("migration runtime database user is invalid")
    return user, secret_arn


def _migration_marker_config(settings: Settings) -> tuple[str, str] | None:
    parameter = settings.migration_marker_parameter
    image_tag = settings.release_image_tag
    if parameter is None and image_tag is None:
        return None
    if parameter is None or image_tag is None:
        raise RuntimeError(
            "migration marker parameter and release image tag must be configured together"
        )
    if not image_tag.strip() or image_tag.lower() == "latest":
        raise RuntimeError("release image tag must be an immutable non-latest tag")
    return parameter, image_tag


def _secret_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _runtime_database_password(
    settings: Settings,
    *,
    secrets_client: Any | None = None,
) -> tuple[str, str] | None:
    runtime_config = _runtime_database_config(settings)
    if runtime_config is None:
        return None
    user, secret_arn = runtime_config
    client = secrets_client or boto3.client("secretsmanager", region_name=settings.aws_region)

    secret_string: str | None = None
    try:
        response = client.get_secret_value(SecretId=secret_arn)
        value = response.get("SecretString")
        if isinstance(value, str):
            secret_string = value
    except ClientError as exc:
        if _secret_error_code(exc) != "ResourceNotFoundException":
            raise RuntimeError("runtime database secret could not be read") from None

    if secret_string is not None:
        try:
            payload = json.loads(secret_string)
        except (TypeError, ValueError):
            raise RuntimeError("runtime database secret is invalid") from None
        if not isinstance(payload, dict):
            raise RuntimeError("runtime database secret is invalid")
        stored_user = payload.get("username")
        password = payload.get("password")
        if stored_user != user or not isinstance(password, str) or not password:
            raise RuntimeError("runtime database secret is invalid")
        return user, password

    password = secrets.token_hex(32)
    payload = json.dumps({"username": user, "password": password}, separators=(",", ":"))
    try:
        client.put_secret_value(SecretId=secret_arn, SecretString=payload)
    except ClientError:
        raise RuntimeError("runtime database secret could not be initialized") from None
    return user, password


async def _ensure_runtime_database_role(
    settings: Settings,
    *,
    user: str,
    password: str,
) -> None:
    engine = create_async_engine(
        settings.resolved_database_url(),
        poolclass=NullPool,
        connect_args=settings.resolved_database_connect_args(),
    )
    try:
        async with engine.begin() as connection:
            exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                    {"role_name": user},
                )
            ).scalar_one_or_none()
            preparer = connection.dialect.identifier_preparer
            quoted_user = preparer.quote(user)
            quoted_database = preparer.quote(settings.database_name)
            quoted_password = (
                await connection.execute(
                    text("SELECT quote_literal(:password)"), {"password": password}
                )
            ).scalar_one()
            role_options = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            if exists is None:
                await connection.exec_driver_sql(
                    f"CREATE ROLE {quoted_user} LOGIN PASSWORD {quoted_password} {role_options}"
                )
            else:
                await connection.exec_driver_sql(
                    f"ALTER ROLE {quoted_user} LOGIN PASSWORD {quoted_password} {role_options}"
                )
            await connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_user}"
            )
    finally:
        await engine.dispose()


async def _grant_runtime_database_access(settings: Settings, *, user: str) -> None:
    engine = create_async_engine(
        settings.resolved_database_url(),
        poolclass=NullPool,
        connect_args=settings.resolved_database_connect_args(),
    )
    try:
        async with engine.begin() as connection:
            quoted_user = connection.dialect.identifier_preparer.quote(user)
            await connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_user}")
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f"TO {quoted_user}"
            )
            await connection.exec_driver_sql(
                f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {quoted_user}"
            )
            await connection.exec_driver_sql(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted_user}"
            )
            await connection.exec_driver_sql(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {quoted_user}"
            )
            await connection.exec_driver_sql(
                f"REVOKE INSERT, UPDATE, DELETE ON TABLE alembic_version FROM {quoted_user}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT ON TABLE alembic_version TO {quoted_user}"
            )
    finally:
        await engine.dispose()


def _write_migration_marker(
    settings: Settings,
    *,
    ssm_client: Any | None = None,
) -> None:
    marker_config = _migration_marker_config(settings)
    if marker_config is None:
        return
    parameter, image_tag = marker_config
    client = ssm_client or boto3.client("ssm", region_name=settings.aws_region)
    try:
        client.put_parameter(Name=parameter, Value=image_tag, Type="String", Overwrite=True)
    except ClientError:
        raise RuntimeError("migration marker could not be written") from None


def main() -> None:
    settings = Settings()
    runtime_credentials = _runtime_database_password(settings)
    if runtime_credentials is not None:
        user, password = runtime_credentials
        asyncio.run(_ensure_runtime_database_role(settings, user=user, password=password))

    command.upgrade(Config("alembic.ini"), "head")

    if runtime_credentials is not None:
        user, _ = runtime_credentials
        asyncio.run(_grant_runtime_database_access(settings, user=user))

    _write_migration_marker(settings)


if __name__ == "__main__":
    main()
