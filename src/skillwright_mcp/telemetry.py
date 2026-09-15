from __future__ import annotations

from opentelemetry import metrics, trace

from . import __version__

_TRACER = trace.get_tracer("skillwright_mcp", __version__)
_METER = metrics.get_meter("skillwright_mcp", __version__)

_ACTIVE_RUNS = _METER.create_up_down_counter(
    "skillwright.runs.active",
    unit="1",
    description="Workflow execution segments currently active in this process.",
)
_RUN_OUTCOMES = _METER.create_counter(
    "skillwright.runs.outcomes",
    unit="1",
    description="Workflow execution outcomes.",
)
_WORKFLOW_DURATION = _METER.create_histogram(
    "skillwright.workflow.duration",
    unit="ms",
    description="Duration of deterministic workflow execution segments.",
)
_STEP_DURATION = _METER.create_histogram(
    "skillwright.workflow.step.duration",
    unit="ms",
    description="Duration of workflow step execution.",
)
_BROWSER_ACTION_DURATION = _METER.create_histogram(
    "skillwright.browser.action.duration",
    unit="ms",
    description="Latency of calls across the Playwright MCP boundary.",
)
_REPAIR_REQUIRED = _METER.create_counter(
    "skillwright.repair.required",
    unit="1",
    description="Workflow steps that require deterministic target repair.",
)
_QUEUE_WAIT = _METER.create_histogram(
    "skillwright.queue.wait.duration",
    unit="ms",
    description="Time from durable queue creation until the first worker claim.",
)
_STALE_RECOVERY = _METER.create_counter(
    "skillwright.runs.stale_recovery",
    unit="1",
    description="Stale-run recovery outcomes.",
)


def tracer() -> trace.Tracer:
    return _TRACER


def run_started() -> None:
    _ACTIVE_RUNS.add(1)


def run_finished(*, status: str, duration_ms: float) -> None:
    _ACTIVE_RUNS.add(-1)
    _RUN_OUTCOMES.add(1, {"status": status})
    _WORKFLOW_DURATION.record(duration_ms, {"status": status})


def workflow_step_finished(*, operation: str, status: str, duration_ms: float) -> None:
    _STEP_DURATION.record(
        duration_ms,
        {"operation": operation, "status": status},
    )
    if status == "repair_required":
        _REPAIR_REQUIRED.add(1, {"operation": operation})


def queue_wait_finished(*, duration_ms: float) -> None:
    _QUEUE_WAIT.record(duration_ms)


def stale_recovery_recorded(*, outcome: str, count: int) -> None:
    if count > 0:
        _STALE_RECOVERY.add(count, {"outcome": outcome})


def browser_action_finished(*, tool: str, source: str, status: str, duration_ms: float) -> None:
    _BROWSER_ACTION_DURATION.record(
        duration_ms,
        {"tool": tool, "source": source, "status": status},
    )
