from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import PlaywrightMCPClient
from skillwright_mcp.skills import SkillService
from skillwright_mcp.snapshot import PageSnapshot, parse_snapshot

ROOT = Path(__file__).resolve().parents[1]


class BillingHandler(BaseHTTPRequestHandler):
    variant: ClassVar[str] = "basic_form"

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/":
            self.send_error(404)
            return
        body = (ROOT / "test_sites" / self.variant / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


@pytest.fixture
def billing_server() -> Iterator[tuple[str, type[BillingHandler]]]:
    BillingHandler.variant = "basic_form"
    server = ThreadingHTTPServer(("127.0.0.1", 0), BillingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/", BillingHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _ref(snapshot: PageSnapshot, *, role: str, name: str) -> str:
    element = snapshot.resolve_target(role=role, name=name)
    assert element is not None, f"missing {role} {name!r} in snapshot:\n{snapshot.raw}"
    return element.ref


@pytest.mark.asyncio
async def test_real_record_replay_failure_repair_and_versioning(
    tmp_path: Path,
    billing_server: tuple[str, type[BillingHandler]],
) -> None:
    url, handler = billing_server
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'skillwright.db').as_posix()}",
        playwright_output_dir=tmp_path / "playwright-output",
    )
    database = Database(settings.database_url)
    await database.initialize()
    playwright = PlaywrightMCPClient(settings)
    browser = BrowserController(playwright, database)
    engine = WorkflowEngine(database, browser)
    skills = SkillService(database, browser, engine)

    try:
        started = await skills.record_start(
            "download_latest_invoice",
            "Download the current billing statement.",
        )
        assert started["status"] == "recording"

        assert (await browser.navigate(url)).ok

        snapshot_result = await browser.snapshot()
        assert snapshot_result.ok and snapshot_result.result is not None
        snapshot = parse_snapshot(snapshot_result.result.text)
        account_ref = _ref(snapshot, role="textbox", name="Account number")
        assert (await browser.fill(account_ref, "ACC-42", element="Account number")).ok

        snapshot_result = await browser.snapshot()
        assert snapshot_result.ok and snapshot_result.result is not None
        snapshot = parse_snapshot(snapshot_result.result.text)
        month_ref = _ref(snapshot, role="combobox", name="Statement month")
        assert (
            await browser.select(month_ref, ["september"], element="Statement month")
        ).ok

        snapshot_result = await browser.snapshot()
        assert snapshot_result.ok and snapshot_result.result is not None
        snapshot = parse_snapshot(snapshot_result.result.text)
        bill_ref = _ref(snapshot, role="button", name="Current Bill")
        assert (await browser.click(bill_ref, element="Current Bill")).ok

        saved = await skills.record_stop()
        assert saved["status"] == "saved"
        assert saved["version"] == 1
        assert saved["steps"] == 4
        serialized = str(saved["workflow"])
        assert "Current Bill" in serialized
        assert "ACC-42" in serialized

        first_replay = await engine.run_skill("download_latest_invoice")
        assert first_replay["status"] == "succeeded", first_replay
        assert first_replay["workflow_version"] == 1

        handler.variant = "renamed_button"
        broken = await engine.run_skill("download_latest_invoice")
        assert broken["status"] == "repair_required", broken
        assert broken["session_available"] is True
        assert broken["workflow_version"] == 1
        assert broken["operation"] == "click"
        replacement = next(
            candidate
            for candidate in broken["candidates"]
            if candidate["name"] == "View Latest Statement"
        )

        repaired = await engine.repair(
            broken["run_id"],
            step=broken["step"],
            replacement_element_id=replacement["id"],
            persist=True,
        )
        assert repaired["status"] == "succeeded", repaired
        assert repaired["saved_workflow_version"] == 2

        versions = await skills.versions("download_latest_invoice")
        assert [version["version"] for version in versions["versions"]] == [2, 1]

        second_replay = await engine.run_skill("download_latest_invoice")
        assert second_replay["status"] == "succeeded", second_replay
        assert second_replay["workflow_version"] == 2
    finally:
        await playwright.close()
        await database.close()
