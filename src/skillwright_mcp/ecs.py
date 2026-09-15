from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from ipaddress import ip_address
from time import monotonic
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class EcsTaskProtection:
    """Keep stateful MCP tasks alive while they own interactive browser sessions."""

    def __init__(
        self,
        endpoint: str | None,
        *,
        required: bool = False,
        expires_in_minutes: int = 45,
        refresh_after_seconds: float = 10 * 60.0,
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._endpoint = endpoint
        self._required = required
        self._expires_in_minutes = expires_in_minutes
        self._refresh_after_seconds = refresh_after_seconds
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._protected = False
        self._last_refresh = 0.0

    @classmethod
    def from_environment(cls) -> EcsTaskProtection:
        execution_env = os.getenv("AWS_EXECUTION_ENV", "")
        if not execution_env.startswith("AWS_ECS_"):
            return cls(None)

        agent_uri = os.getenv("ECS_AGENT_URI")
        endpoint = _task_protection_endpoint(agent_uri) if agent_uri else None
        return cls(endpoint, required=True)

    async def protect(self) -> None:
        if self._endpoint is None:
            if self._required:
                raise RuntimeError("ECS task protection endpoint is unavailable")
            return

        async with self._lock:
            now = self._clock()
            if self._protected and now - self._last_refresh < self._refresh_after_seconds:
                return
            await asyncio.to_thread(
                self._set_protection,
                True,
                self._expires_in_minutes,
            )
            self._protected = True
            self._last_refresh = now

    async def unprotect(self) -> None:
        if self._endpoint is None:
            return

        async with self._lock:
            if not self._protected:
                return
            await asyncio.to_thread(self._set_protection, False, None)
            self._protected = False
            self._last_refresh = 0.0

    def _set_protection(self, enabled: bool, expires_in_minutes: int | None) -> None:
        payload: dict[str, bool | int] = {"ProtectionEnabled": enabled}
        if expires_in_minutes is not None:
            payload["ExpiresInMinutes"] = expires_in_minutes
        request = Request(
            self._endpoint or "",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise RuntimeError("ECS task protection request failed")
        except (OSError, TimeoutError, URLError) as exc:
            raise RuntimeError("ECS task protection request failed") from exc


def _task_protection_endpoint(agent_uri: str) -> str | None:
    parsed = urlparse(agent_uri)
    if parsed.scheme != "http" or parsed.hostname is None:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        if not ip_address(parsed.hostname).is_link_local:
            return None
    except ValueError:
        return None
    return f"{agent_uri.rstrip('/')}/task-protection/v1/state"
