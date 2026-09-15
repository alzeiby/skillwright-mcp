from __future__ import annotations

from skillwright_mcp.ecs import _task_protection_endpoint


def test_task_protection_endpoint_accepts_only_link_local_agent_uri() -> None:
    assert _task_protection_endpoint("http://169.254.170.2/api") == (
        "http://169.254.170.2/api/task-protection/v1/state"
    )
    assert _task_protection_endpoint("https://169.254.170.2/api") is None
    assert _task_protection_endpoint("http://example.com/api") is None
    assert _task_protection_endpoint("http://127.0.0.1/api") is None
    assert _task_protection_endpoint("http://user@169.254.170.2/api") is None
    assert _task_protection_endpoint("http://169.254.170.2/api?x=1") is None
