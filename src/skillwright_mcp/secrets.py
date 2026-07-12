from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
_SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


class SecretResolutionError(RuntimeError):
    pass


def validate_secret_ref(secret_ref: str) -> str:
    if not _SECRET_REF_RE.fullmatch(secret_ref):
        raise ValueError("secret reference must match [A-Z][A-Z0-9_]*")
    return secret_ref


def resolve_secret(secret_ref: str) -> str:
    """Resolve one environment-backed secret without caching its plaintext value."""

    try:
        normalized_ref = validate_secret_ref(secret_ref)
    except ValueError as exc:
        raise SecretResolutionError("secret binding is invalid") from exc
    value = os.environ.get(f"SKILLWRIGHT_SECRET_{normalized_ref}")
    if value is None or not value:
        raise SecretResolutionError("secret is not configured")
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
