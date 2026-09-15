from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
_SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SecretResolutionError(RuntimeError):
    pass


def validate_secret_ref(secret_ref: str) -> str:
    if not _SECRET_REF_RE.fullmatch(secret_ref):
        raise ValueError(
            "secret reference must match [A-Z][A-Z0-9_]*"
        )
    return secret_ref


def secret_env_name(secret_ref: str) -> str:
    validate_secret_ref(secret_ref)
    return f"SKILLWRIGHT_SECRET_{secret_ref}"


def secret_marker(secret_ref: str, *, provider: str = "env") -> dict[str, dict[str, str]]:
    if provider != "env":
        raise ValueError(f"unsupported secret provider: {provider}")
    return {
        "$secret": {
            "provider": provider,
            "reference": validate_secret_ref(secret_ref),
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
    """Resolve opaque Skillwright refs from a tightly scoped environment namespace."""

    def resolve(self, secret_ref: str, *, provider: str = "env") -> str:
        if provider != "env":
            raise SecretResolutionError(f"unsupported secret provider: {provider}")
        env_name = secret_env_name(secret_ref)
        value = os.environ.get(env_name)
        if value is None:
            raise SecretResolutionError(f"secret {secret_ref!r} is not configured")
        if not value:
            raise SecretResolutionError(f"secret {secret_ref!r} is configured but empty")
        return value


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
            variants = {secret, quote(secret, safe=""), quote_plus(secret)}
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
