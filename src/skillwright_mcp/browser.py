from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from .db import BrowserActionRow, Database
from .playwright import BrowserResult, PlaywrightMCPClient
from .secrets import Redactor
from .telemetry import browser_action_finished, tracer


@dataclass(slots=True)
class BrowserActionResult:
    event_id: int
    result: BrowserResult | None
    ok: bool
    error: str | None
    snapshot_before: str | None
    snapshot_after: str | None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "event_id": self.event_id,
            "error": self.error,
        }
        if self.result is not None:
            payload.update(self.result.as_dict())
        return payload


class BrowserController:
    """Proxy Playwright MCP calls while capturing durable browser action history."""

    def __init__(self, playwright: PlaywrightMCPClient, database: Database) -> None:
        self.playwright = playwright
        self.database = database
        self._active_recordings: dict[str, str] = {}
        self.latest_snapshot: str | None = None
        self._session_redactor = Redactor()

    @staticmethod
    def _actor_key(actor_principal_id: str | None) -> str:
        return actor_principal_id or "__legacy__"

    @property
    def active_recording_id(self) -> str | None:
        return self._active_recordings.get(self._actor_key(None))

    @active_recording_id.setter
    def active_recording_id(self, value: str | None) -> None:
        key = self._actor_key(None)
        if value is None:
            self._active_recordings.pop(key, None)
        else:
            self._active_recordings[key] = value

    def active_recording_for(self, actor_principal_id: str | None) -> str | None:
        return self._active_recordings.get(self._actor_key(actor_principal_id))

    def set_active_recording(
        self,
        actor_principal_id: str | None,
        recording_id: str | None,
    ) -> None:
        key = self._actor_key(actor_principal_id)
        if recording_id is None:
            self._active_recordings.pop(key, None)
        else:
            self._active_recordings[key] = recording_id

    async def close(self) -> None:
        """Close the browser session and release secret material retained for session redaction."""

        try:
            await self.playwright.close()
        finally:
            self._session_redactor = Redactor()
            self.latest_snapshot = None

    async def navigate(
        self,
        url: str,
        *,
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> BrowserActionResult:
        return await self._call(
            public_name="browser_navigate",
            public_args={"url": url},
            upstream_name="browser_navigate",
            upstream_args={"url": url},
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )

    async def snapshot(
        self,
        *,
        target: str | None = None,
        depth: int | None = None,
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> BrowserActionResult:
        args: dict[str, Any] = {}
        if target is not None:
            args["target"] = target
        if depth is not None:
            args["depth"] = depth
        return await self._call(
            public_name="browser_snapshot",
            public_args=args,
            upstream_name="browser_snapshot",
            upstream_args=args,
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )

    async def click(
        self,
        target: str,
        *,
        element: str | None = None,
        double_click: bool = False,
        button: Literal["left", "right", "middle"] = "left",
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> BrowserActionResult:
        public_args: dict[str, Any] = {
            "target": target,
            "element": element,
            "double_click": double_click,
            "button": button,
        }
        upstream_args: dict[str, Any] = {"target": target}
        if element:
            upstream_args["element"] = element
        if double_click:
            upstream_args["doubleClick"] = True
        if button != "left":
            upstream_args["button"] = button
        return await self._call(
            public_name="browser_click",
            public_args=public_args,
            upstream_name="browser_click",
            upstream_args=upstream_args,
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )

    async def fill(
        self,
        target: str,
        text: str,
        *,
        element: str | None = None,
        submit: bool = False,
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> BrowserActionResult:
        public_args: dict[str, Any] = {
            "target": target,
            "text": text,
            "element": element,
            "submit": submit,
        }
        upstream_args: dict[str, Any] = {"target": target, "text": text}
        if element:
            upstream_args["element"] = element
        if submit:
            upstream_args["submit"] = True
        return await self._call(
            public_name="browser_fill",
            public_args=public_args,
            upstream_name="browser_type",
            upstream_args=upstream_args,
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )

    async def fill_secret(
        self,
        target: str,
        secret_value: str,
        *,
        secret_ref: str,
        input_name: str,
        element: str | None = None,
        submit: bool = False,
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
    ) -> BrowserActionResult:
        public_args: dict[str, Any] = {
            "target": target,
            "secret_ref": secret_ref,
            "input_name": input_name,
            "element": element,
            "submit": submit,
        }
        upstream_args: dict[str, Any] = {"target": target, "text": secret_value}
        if element:
            upstream_args["element"] = element
        if submit:
            upstream_args["submit"] = True
        return await self._call(
            public_name="browser_fill_secret",
            public_args=public_args,
            upstream_name="browser_type",
            upstream_args=upstream_args,
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=Redactor.from_values([secret_value]),
        )

    async def select(
        self,
        target: str,
        values: list[str],
        *,
        element: str | None = None,
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> BrowserActionResult:
        public_args = {"target": target, "values": values, "element": element}
        upstream_args: dict[str, Any] = {"target": target, "values": values}
        if element:
            upstream_args["element"] = element
        return await self._call(
            public_name="browser_select",
            public_args=public_args,
            upstream_name="browser_select_option",
            upstream_args=upstream_args,
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )

    async def wait(
        self,
        *,
        seconds: float | None = None,
        text: str | None = None,
        text_gone: str | None = None,
        source: str = "agent",
        run_id: str | None = None,
        actor_principal_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> BrowserActionResult:
        args: dict[str, Any] = {}
        if seconds is not None:
            args["time"] = seconds
        if text is not None:
            args["text"] = text
        if text_gone is not None:
            args["textGone"] = text_gone
        public_args = {"seconds": seconds, "text": text, "text_gone": text_gone}
        return await self._call(
            public_name="browser_wait",
            public_args=public_args,
            upstream_name="browser_wait_for",
            upstream_args=args,
            source=source,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )

    async def generate_locator(
        self,
        target: str,
        *,
        element: str | None = None,
        redactor: Redactor | None = None,
    ) -> str | None:
        args: dict[str, Any] = {"target": target}
        if element:
            args["element"] = element
        return await self._generate_locator(args, redactor=redactor)

    async def _call(
        self,
        *,
        public_name: str,
        public_args: dict[str, Any],
        upstream_name: str,
        upstream_args: dict[str, Any],
        source: str,
        run_id: str | None,
        actor_principal_id: str | None,
        redactor: Redactor | None,
    ) -> BrowserActionResult:
        self._session_redactor = self._session_redactor.merged(redactor)
        effective_redactor = self._session_redactor
        snapshot_before = self.latest_snapshot
        durable_locator = (
            await self._generate_locator(upstream_args, redactor=effective_redactor)
            if public_name
            in {"browser_click", "browser_fill", "browser_fill_secret", "browser_select"}
            else None
        )
        recording_id = (
            self.active_recording_for(actor_principal_id) if source == "agent" else None
        )
        pending = await self.database.start_browser_action(
            actor_principal_id=actor_principal_id,
            recording_id=recording_id,
            run_id=run_id,
            source=source,
            tool_name=public_name,
            arguments=effective_redactor.value(_without_none(public_args)),
            upstream_tool_name=upstream_name,
            upstream_arguments=effective_redactor.value(upstream_args),
            snapshot_before=(
                effective_redactor.text(snapshot_before) if snapshot_before is not None else None
            ),
            durable_locator=durable_locator,
        )
        span_attributes: dict[str, str] = {
            "skillwright.browser.tool": public_name,
            "skillwright.browser.source": source,
        }
        if run_id is not None:
            span_attributes["skillwright.run.id"] = run_id
        with tracer().start_as_current_span(
            "browser.action",
            attributes=span_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            started = perf_counter()
            result: BrowserResult | None = None
            error: str | None = None
            try:
                result = await self.playwright.call(upstream_name, upstream_args)
                if not result.ok:
                    error = result.text or "Playwright MCP returned an error"
            except Exception as exc:
                # Transport/process failures must become explicit action evidence.
                error = f"{type(exc).__name__}: {exc}"

            duration_ms = (perf_counter() - started) * 1000
            success = result is not None and result.ok
            status = "succeeded" if success else "failed"
            span.set_attribute("skillwright.browser.status", status)
            browser_action_finished(
                tool=public_name,
                source=source,
                status=status,
                duration_ms=duration_ms,
            )
        redacted_result = _redact_browser_result(result, effective_redactor)
        if redacted_result is not None and redacted_result.ok:
            if public_name == "browser_snapshot" or _contains_snapshot_refs(redacted_result.text):
                self.latest_snapshot = redacted_result.text
            else:
                # Current Playwright MCP action responses usually link snapshots as files.
                # Do not carry semantic evidence across a page-changing action.
                self.latest_snapshot = None
        snapshot_after = self.latest_snapshot
        redacted_error = effective_redactor.text(error) if error is not None else None
        redacted_snapshot_before = (
            effective_redactor.text(snapshot_before) if snapshot_before is not None else None
        )
        redacted_snapshot_after = (
            effective_redactor.text(snapshot_after) if snapshot_after is not None else None
        )

        row: BrowserActionRow = await self.database.finish_browser_action(
            pending.id,
            result=(
                redacted_result.raw
                if redacted_result is not None
                else {"error": redacted_error}
            ),
            success=success,
            error=redacted_error,
            snapshot_after=redacted_snapshot_after,
            duration_ms=duration_ms,
        )
        return BrowserActionResult(
            event_id=row.id,
            result=redacted_result,
            ok=success,
            error=redacted_error,
            snapshot_before=redacted_snapshot_before,
            snapshot_after=redacted_snapshot_after,
        )

    async def _generate_locator(
        self,
        upstream_args: dict[str, Any],
        *,
        redactor: Redactor | None = None,
    ) -> str | None:
        target = upstream_args.get("target")
        if not isinstance(target, str) or not target:
            return None
        if not await self.playwright.has_tool("browser_generate_locator"):
            return None
        try:
            result = await self.playwright.call(
                "browser_generate_locator",
                {
                    "target": target,
                    **(
                        {"element": upstream_args["element"]}
                        if isinstance(upstream_args.get("element"), str)
                        else {}
                    ),
                },
            )
        except Exception:
            return None
        if not result.ok:
            return None
        locator = _extract_locator(result.text)
        return redactor.text(locator) if locator is not None and redactor is not None else locator


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _redact_browser_result(
    result: BrowserResult | None,
    redactor: Redactor,
) -> BrowserResult | None:
    if result is None:
        return None
    structured = redactor.value(result.structured_content)
    return BrowserResult(
        tool_name=result.tool_name,
        ok=result.ok,
        text=redactor.text(result.text),
        structured_content=structured if isinstance(structured, dict) else None,
        raw=redactor.value(result.raw),
    )


def _contains_snapshot_refs(text: str) -> bool:
    return "[ref=" in text


_LOCATOR_CODE_RE = re.compile(r"```(?:\w+)?\s*\n(?P<code>.*?)\n```", re.DOTALL)


def _extract_locator(text: str) -> str | None:
    if match := _LOCATOR_CODE_RE.search(text):
        code = match.group("code").strip().rstrip(";")
        for prefix in ("await page.", "page."):
            if code.startswith(prefix):
                code = code.removeprefix(prefix)
        if code:
            return code
    for line in reversed(text.splitlines()):
        candidate = line.strip().strip("`").rstrip(";")
        if (
            "getBy" in candidate
            or candidate.startswith("locator(")
            or candidate.startswith("frameLocator(")
        ):
            candidate = candidate.removeprefix("await page.").removeprefix("page.")
            return candidate
    return None
