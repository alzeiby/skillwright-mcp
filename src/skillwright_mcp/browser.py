from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from .db import BrowserActionRow, Database
from .playwright import BrowserResult, PlaywrightMCPClient


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
        self.active_recording_id: str | None = None
        self.latest_snapshot: str | None = None

    async def navigate(
        self, url: str, *, source: str = "agent", run_id: str | None = None
    ) -> BrowserActionResult:
        return await self._call(
            public_name="browser_navigate",
            public_args={"url": url},
            upstream_name="browser_navigate",
            upstream_args={"url": url},
            source=source,
            run_id=run_id,
        )

    async def snapshot(
        self,
        *,
        target: str | None = None,
        depth: int | None = None,
        source: str = "agent",
        run_id: str | None = None,
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
        )

    async def select(
        self,
        target: str,
        values: list[str],
        *,
        element: str | None = None,
        source: str = "agent",
        run_id: str | None = None,
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
        )

    async def wait(
        self,
        *,
        seconds: float | None = None,
        text: str | None = None,
        text_gone: str | None = None,
        source: str = "agent",
        run_id: str | None = None,
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
        )

    async def generate_locator(self, target: str, *, element: str | None = None) -> str | None:
        args: dict[str, Any] = {"target": target}
        if element:
            args["element"] = element
        return await self._generate_locator(args)

    async def _call(
        self,
        *,
        public_name: str,
        public_args: dict[str, Any],
        upstream_name: str,
        upstream_args: dict[str, Any],
        source: str,
        run_id: str | None,
    ) -> BrowserActionResult:
        snapshot_before = self.latest_snapshot
        durable_locator = (
            await self._generate_locator(upstream_args)
            if public_name in {"browser_click", "browser_fill", "browser_select"}
            else None
        )
        recording_id = self.active_recording_id if source == "agent" else None
        pending = await self.database.start_browser_action(
            recording_id=recording_id,
            run_id=run_id,
            source=source,
            tool_name=public_name,
            arguments=_without_none(public_args),
            upstream_tool_name=upstream_name,
            upstream_arguments=upstream_args,
            snapshot_before=snapshot_before,
            durable_locator=durable_locator,
        )
        started = perf_counter()
        result: BrowserResult | None = None
        error: str | None = None
        try:
            result = await self.playwright.call(upstream_name, upstream_args)
            if not result.ok:
                error = result.text or "Playwright MCP returned an error"
        except Exception as exc:  # transport/process failures must become explicit action evidence
            error = f"{type(exc).__name__}: {exc}"

        duration_ms = (perf_counter() - started) * 1000
        if result is not None and result.ok:
            if public_name == "browser_snapshot" or _contains_snapshot_refs(result.text):
                self.latest_snapshot = result.text
            else:
                # Current Playwright MCP action responses usually link snapshots as files.
                # Do not carry semantic evidence across a page-changing action.
                self.latest_snapshot = None
        snapshot_after = self.latest_snapshot
        success = result is not None and result.ok

        row: BrowserActionRow = await self.database.finish_browser_action(
            pending.id,
            result=result.raw if result is not None else {"error": error},
            success=success,
            error=error,
            snapshot_after=snapshot_after,
            duration_ms=duration_ms,
        )
        return BrowserActionResult(
            event_id=row.id,
            result=result,
            ok=success,
            error=error,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
        )

    async def _generate_locator(self, upstream_args: dict[str, Any]) -> str | None:
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
        return _extract_locator(result.text)


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


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
