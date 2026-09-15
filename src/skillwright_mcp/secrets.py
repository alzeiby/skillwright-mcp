from __future__ import annotations

import asyncio
import os
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, quote_plus

import boto3  # type: ignore[import-untyped]
from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]

REDACTED = "[REDACTED]"
_SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_AWS_SECRET_REF_RE = re.compile(r"^[A-Za-z0-9_./+=,@:-]{1,2048}$")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
SecretProvider = Literal["env", "aws-secrets-manager", "aws-ssm"]
SECRET_PROVIDERS: frozenset[str] = frozenset({"env", "aws-secrets-manager", "aws-ssm"})


class SecretResolutionError(RuntimeError):
    pass


def validate_secret_ref(secret_ref: str, *, provider: str = "env") -> str:
    if provider not in SECRET_PROVIDERS:
        raise ValueError(f"unsupported secret provider: {provider}")
    if provider == "env" and not _SECRET_REF_RE.fullmatch(secret_ref):
        raise ValueError(
            "secret reference must match [A-Z][A-Z0-9_]*"
        )
    if provider != "env" and not _AWS_SECRET_REF_RE.fullmatch(secret_ref):
        raise ValueError(
            "AWS secret references must contain only AWS name/ARN characters and no whitespace"
        )
    return secret_ref


def secret_env_name(secret_ref: str) -> str:
    validate_secret_ref(secret_ref, provider="env")
    return f"SKILLWRIGHT_SECRET_{secret_ref}"


def secret_marker(secret_ref: str, *, provider: str = "env") -> dict[str, dict[str, str]]:
    return {
        "$secret": {
            "provider": provider,
            "reference": validate_secret_ref(secret_ref, provider=provider),
        }
    }


def secret_binding_from_marker(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"$secret"}:
        return None
    payload = value.get("$secret")
    if not isinstance(payload, dict) or set(payload) != {"provider", "reference"}:
        return None
    provider = payload.get("provider")
    reference = payload.get("reference")
    if not isinstance(provider, str) or not isinstance(reference, str):
        return None
    return provider, reference


class SecretResolver:
    """Resolve opaque Skillwright refs without caching plaintext secret values."""

    def __init__(
        self,
        *,
        aws_region: str | None = None,
        aws_timeout_seconds: float = 15.0,
        aws_client_factory: Callable[[str, str | None], Any] | None = None,
    ) -> None:
        self._aws_region = aws_region
        self._aws_timeout_seconds = aws_timeout_seconds
        self._aws_client_factory = aws_client_factory or self._default_aws_client_factory
        self._aws_clients: dict[str, Any] = {}
        self._aws_clients_lock = threading.Lock()

    async def resolve(self, secret_ref: str, *, provider: str = "env") -> str:
        try:
            normalized_ref = validate_secret_ref(secret_ref, provider=provider)
        except ValueError as exc:
            raise SecretResolutionError("secret binding is invalid") from exc

        if provider == "env":
            env_name = secret_env_name(normalized_ref)
            value = os.environ.get(env_name)
            if value is None or not value:
                raise SecretResolutionError("secret is not configured")
            return value

        try:
            async with asyncio.timeout(self._aws_timeout_seconds):
                return await asyncio.to_thread(self._resolve_aws, normalized_ref, provider)
        except TimeoutError as exc:
            raise SecretResolutionError("secret resolution timed out") from exc

    def _resolve_aws(self, secret_ref: str, provider: str) -> str:
        try:
            if provider == "aws-secrets-manager":
                response = self._aws_client("secretsmanager").get_secret_value(SecretId=secret_ref)
                value = response.get("SecretString")
                if not isinstance(value, str):
                    raise SecretResolutionError("binary Secrets Manager values are not supported")
            elif provider == "aws-ssm":
                response = self._aws_client("ssm").get_parameter(
                    Name=secret_ref,
                    WithDecryption=True,
                )
                parameter = response.get("Parameter")
                value = parameter.get("Value") if isinstance(parameter, dict) else None
                if not isinstance(value, str):
                    raise SecretResolutionError("SSM parameter did not contain a string value")
            else:
                raise SecretResolutionError("unsupported secret provider")
        except SecretResolutionError:
            raise
        except Exception as exc:
            # AWS SDK errors can contain request metadata or the secret reference. Keep the
            # durable/user-facing failure intentionally generic and retain details only as the
            # exception cause in process memory.
            raise SecretResolutionError("secret could not be resolved") from exc
        if not value:
            raise SecretResolutionError("secret is configured but empty")
        return value

    def _aws_client(self, service_name: str) -> Any:
        with self._aws_clients_lock:
            client = self._aws_clients.get(service_name)
            if client is None:
                client = self._aws_client_factory(service_name, self._aws_region)
                self._aws_clients[service_name] = client
            return client

    @staticmethod
    def _default_aws_client_factory(service_name: str, region_name: str | None) -> Any:
        return boto3.client(
            service_name,
            region_name=region_name,
            config=BotoConfig(
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 2, "mode": "standard"},
            ),
        )


@dataclass(frozen=True, slots=True)
class Redactor:
    _values: tuple[str, ...] = ()

    @classmethod
    def from_values(cls, values: Iterable[str]) -> Redactor:
        unique = sorted({value for value in values if value}, key=len, reverse=True)
        return cls(tuple(unique))

    def merged(self, other: Redactor | None) -> Redactor:
        if other is None:
            return self
        return Redactor.from_values((*self._values, *other._values))

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._values:
            encoded = quote(secret, safe="")
            encoded_plus = quote_plus(secret)
            variants = {
                secret,
                encoded,
                encoded_plus,
                _lower_percent_escapes(encoded),
                _lower_percent_escapes(encoded_plus),
            }
            for variant in sorted(variants, key=len, reverse=True):
                if variant:
                    redacted = redacted.replace(variant, REDACTED)
        return redacted

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                self.text(key) if isinstance(key, str) else key: self.value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.value(item) for item in value)
        return value


def _lower_percent_escapes(value: str) -> str:
    return _PERCENT_ESCAPE_RE.sub(lambda match: match.group(0).lower(), value)
