from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .workflow import WorkflowDefinition

JsonType = JSON().with_variant(JSONB, "postgresql")


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class RecordingRow(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="recording", nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserActionRow(Base):
    __tablename__ = "browser_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recording_id: Mapped[str | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(32), default="agent", nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    upstream_tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    upstream_arguments: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    durable_locator: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), default="started", nullable=False, index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    snapshot_before: Mapped[str | None] = mapped_column(Text)
    snapshot_after: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class SkillRow(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class WorkflowVersionRow(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_workflow_version_skill_version"),
        Index("ix_workflow_versions_skill_created", "skill_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    created_from_recording_id: Mapped[str | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL")
    )
    parent_version: Mapped[int | None] = mapped_column(Integer)
    change_reason: Mapped[str] = mapped_column(Text, default="created", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("skill_id", "idempotency_key", name="uq_runs_skill_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    workflow_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"), index=True
    )
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    outputs: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    repair_overrides: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_context: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    worker_id: Mapped[str | None] = mapped_column(String(160), index=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StepExecutionRow(Base):
    __tablename__ = "step_executions"
    __table_args__ = (Index("ix_step_executions_run_step", "run_id", "step_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    step: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    resolved_target: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class RepairRow(Base):
    __tablename__ = "repairs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    workflow_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"), index=True
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_target: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    replacement_target: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(80), nullable=False)
    persist_version: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    new_workflow_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(160), default="local", nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite+aiosqlite:///./"):
            relative = url.removeprefix("sqlite+aiosqlite:///./")
            Path(relative).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self, *, create_schema: bool = True) -> None:
        async with self.engine.begin() as connection:
            if create_schema:
                await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def start_recording(self, name: str, description: str = "") -> RecordingRow:
        async with self.sessions.begin() as session:
            row = RecordingRow(name=name, description=description)
            session.add(row)
            await session.flush()
            return row

    async def stop_recording(self, recording_id: str, status: str = "stopped") -> RecordingRow:
        async with self.sessions.begin() as session:
            row = await session.get(RecordingRow, recording_id)
            if row is None:
                raise KeyError(f"recording not found: {recording_id}")
            row.status = status
            row.stopped_at = _now()
            return row

    async def get_recording(self, recording_id: str) -> RecordingRow | None:
        async with self.sessions() as session:
            return await session.get(RecordingRow, recording_id)

    async def start_browser_action(
        self,
        *,
        recording_id: str | None,
        run_id: str | None,
        source: str,
        tool_name: str,
        arguments: dict[str, Any],
        upstream_tool_name: str,
        upstream_arguments: dict[str, Any],
        snapshot_before: str | None,
        durable_locator: str | None,
    ) -> BrowserActionRow:
        async with self.sessions.begin() as session:
            row = BrowserActionRow(
                recording_id=recording_id,
                run_id=run_id,
                source=source,
                tool_name=tool_name,
                arguments=arguments,
                upstream_tool_name=upstream_tool_name,
                upstream_arguments=upstream_arguments,
                durable_locator=durable_locator,
                state="started",
                result={},
                success=None,
                snapshot_before=snapshot_before,
            )
            session.add(row)
            await session.flush()
            return row

    async def finish_browser_action(
        self,
        action_id: int,
        *,
        result: dict[str, Any],
        success: bool,
        error: str | None,
        snapshot_after: str | None,
        duration_ms: float,
    ) -> BrowserActionRow:
        async with self.sessions.begin() as session:
            row = await session.get(BrowserActionRow, action_id)
            if row is None:
                raise KeyError(f"browser action not found: {action_id}")
            row.state = "succeeded" if success else "failed"
            row.result = result
            row.success = success
            row.error = error
            row.snapshot_after = snapshot_after
            row.duration_ms = duration_ms
            await session.flush()
            return row

    async def recording_actions(self, recording_id: str) -> Sequence[BrowserActionRow]:
        async with self.sessions() as session:
            result = await session.scalars(
                select(BrowserActionRow)
                .where(BrowserActionRow.recording_id == recording_id)
                .order_by(BrowserActionRow.id)
            )
            return result.all()

    async def action_range(self, start_event: int, end_event: int) -> Sequence[BrowserActionRow]:
        if end_event < start_event:
            raise ValueError("end_event must be >= start_event")
        async with self.sessions() as session:
            result = await session.scalars(
                select(BrowserActionRow)
                .where(BrowserActionRow.id >= start_event, BrowserActionRow.id <= end_event)
                .order_by(BrowserActionRow.id)
            )
            return result.all()

    async def create_skill_version(
        self,
        definition: WorkflowDefinition,
        *,
        recording_id: str | None = None,
        parent_version: int | None = None,
        change_reason: str = "created",
        expected_current_version: int | None = None,
    ) -> tuple[SkillRow, WorkflowVersionRow]:
        async with self.sessions.begin() as session:
            skill = await session.scalar(
                select(SkillRow).where(SkillRow.name == definition.name).with_for_update()
            )
            if skill is None:
                if expected_current_version not in (None, 0):
                    raise ValueError("workflow no longer exists at the expected version")
                skill = SkillRow(name=definition.name, description=definition.description)
                session.add(skill)
                await session.flush()
            if (
                expected_current_version is not None
                and skill.current_version != expected_current_version
            ):
                raise ValueError(
                    f"stale workflow base: expected v{expected_current_version}, "
                    f"current is v{skill.current_version}"
                )
            new_version = skill.current_version + 1
            row = WorkflowVersionRow(
                skill_id=skill.id,
                version=new_version,
                schema_version=definition.schema_version,
                definition=definition.model_dump(mode="json"),
                created_from_recording_id=recording_id,
                parent_version=parent_version,
                change_reason=change_reason,
            )
            session.add(row)
            skill.current_version = new_version
            skill.description = definition.description
            skill.updated_at = _now()
            await session.flush()
            return skill, row

    async def list_skills(self, limit: int = 100) -> Sequence[SkillRow]:
        async with self.sessions() as session:
            result = await session.scalars(select(SkillRow).order_by(SkillRow.name).limit(limit))
            return result.all()

    async def search_skills(self, query: str, limit: int = 10) -> Sequence[SkillRow]:
        pattern = f"%{query.strip().lower()}%"
        async with self.sessions() as session:
            result = await session.scalars(
                select(SkillRow)
                .where(
                    func.lower(SkillRow.name).like(pattern)
                    | func.lower(SkillRow.description).like(pattern)
                )
                .order_by(SkillRow.name)
                .limit(limit)
            )
            return result.all()

    async def get_skill(self, name: str) -> SkillRow | None:
        async with self.sessions() as session:
            return cast(
                SkillRow | None,
                await session.scalar(select(SkillRow).where(SkillRow.name == name)),
            )

    async def get_workflow_version(
        self, name: str, version: int | None = None
    ) -> tuple[SkillRow, WorkflowVersionRow] | None:
        async with self.sessions() as session:
            skill = await session.scalar(select(SkillRow).where(SkillRow.name == name))
            if skill is None:
                return None
            selected_version = skill.current_version if version is None else version
            row = await session.scalar(
                select(WorkflowVersionRow).where(
                    WorkflowVersionRow.skill_id == skill.id,
                    WorkflowVersionRow.version == selected_version,
                )
            )
            if row is None:
                return None
            return skill, row

    async def workflow_versions(self, name: str) -> Sequence[WorkflowVersionRow]:
        async with self.sessions() as session:
            skill = await session.scalar(select(SkillRow).where(SkillRow.name == name))
            if skill is None:
                return []
            result = await session.scalars(
                select(WorkflowVersionRow)
                .where(WorkflowVersionRow.skill_id == skill.id)
                .order_by(WorkflowVersionRow.version.desc())
            )
            return result.all()

    async def create_run(
        self,
        *,
        skill: SkillRow,
        version: WorkflowVersionRow,
        inputs: dict[str, Any],
        status: str = "running",
        idempotency_key: str | None = None,
    ) -> RunRow:
        async with self.sessions() as session:
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(RunRow).where(
                        RunRow.skill_id == skill.id,
                        RunRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return existing
            row = RunRow(
                skill_id=skill.id,
                workflow_version_id=version.id,
                workflow_version=version.version,
                idempotency_key=idempotency_key,
                status=status,
                inputs=inputs,
                outputs={},
                repair_overrides={},
                attempt_count=1 if status == "running" else 0,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if idempotency_key is None:
                    raise
                existing = await session.scalar(
                    select(RunRow).where(
                        RunRow.skill_id == skill.id,
                        RunRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                return cast(RunRow, existing)
            return row

    async def get_run(self, run_id: str) -> RunRow | None:
        async with self.sessions() as session:
            return await session.get(RunRow, run_id)

    async def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_step: int | None = None,
        outputs: dict[str, Any] | None = None,
        repair_overrides: dict[str, Any] | None = None,
        failure_context: dict[str, Any] | None = None,
        finish: bool = False,
        heartbeat: bool = False,
    ) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if status is not None:
                row.status = status
            if current_step is not None:
                row.current_step = current_step
            if outputs is not None:
                row.outputs = outputs
            if repair_overrides is not None:
                row.repair_overrides = repair_overrides
            row.failure_context = failure_context
            if heartbeat:
                row.heartbeat_at = _now()
            if finish:
                row.finished_at = _now()

    async def claim_run(self, run_id: str, worker_id: str) -> RunRow | None:
        now = _now()
        async with self.sessions.begin() as session:
            result = await session.scalar(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status.in_(["queued", "retrying"]),
                    RunRow.cancel_requested.is_(False),
                )
                .values(
                    status="running",
                    worker_id=worker_id,
                    heartbeat_at=now,
                    attempt_count=RunRow.attempt_count + 1,
                    failure_context=None,
                )
                .returning(RunRow)
            )
            return result

    async def heartbeat_run(self, run_id: str, worker_id: str) -> bool:
        async with self.sessions.begin() as session:
            updated_id = await session.scalar(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status == "running",
                    RunRow.worker_id == worker_id,
                )
                .values(heartbeat_at=_now())
                .returning(RunRow.id)
            )
            return updated_id is not None

    async def is_cancel_requested(self, run_id: str) -> bool:
        async with self.sessions() as session:
            value = await session.scalar(select(RunRow.cancel_requested).where(RunRow.id == run_id))
            return bool(value)

    async def request_cancel(self, run_id: str) -> RunRow | None:
        async with self.sessions.begin() as session:
            row = await session.get(RunRow, run_id, with_for_update=True)
            if row is None:
                return None
            if row.status in {"succeeded", "failed", "cancelled", "failed_unknown"}:
                return row
            row.cancel_requested = True
            if row.status in {"queued", "retrying"}:
                row.status = "cancelled"
                row.finished_at = _now()
            return row

    async def recover_stale_runs(self, stale_after_seconds: int) -> dict[str, list[str]]:
        cutoff = _now() - timedelta(seconds=stale_after_seconds)
        requeued: list[str] = []
        unknown: list[str] = []
        expired_repairs: list[str] = []
        async with self.sessions.begin() as session:
            result = await session.scalars(
                select(RunRow)
                .where(
                    RunRow.status.in_(["running", "repair_required"]),
                    (RunRow.heartbeat_at.is_(None) | (RunRow.heartbeat_at < cutoff)),
                )
                .with_for_update(skip_locked=True)
            )
            rows = list(result.all())
            for row in rows:
                if row.status == "repair_required":
                    context = dict(row.failure_context or {})
                    context.update(
                        {
                            "status": "repair_session_expired",
                            "reason": "worker_heartbeat_expired_while_waiting_for_repair",
                            "session_available": False,
                        }
                    )
                    row.status = "repair_session_expired"
                    row.finished_at = _now()
                    row.worker_id = None
                    row.failure_context = context
                    pending = await session.scalars(
                        select(RepairRow).where(
                            RepairRow.run_id == row.id,
                            RepairRow.status.in_(["pending", "applying"]),
                        )
                    )
                    for repair in pending:
                        repair.status = "session_expired"
                        repair.validation_result = context
                        repair.completed_at = _now()
                    expired_repairs.append(row.id)
                    continue
                started_mutation = await session.scalar(
                    select(BrowserActionRow.id).where(
                        BrowserActionRow.run_id == row.id,
                        BrowserActionRow.state == "started",
                        BrowserActionRow.tool_name.in_(
                            ["browser_click", "browser_fill", "browser_select"]
                        ),
                    )
                )
                completed_mutation = await session.scalar(
                    select(StepExecutionRow.id).where(
                        StepExecutionRow.run_id == row.id,
                        StepExecutionRow.status == "succeeded",
                        StepExecutionRow.step["op"].as_string().in_(["click", "fill", "select"]),
                    )
                )
                await session.execute(
                    update(BrowserActionRow)
                    .where(BrowserActionRow.run_id == row.id, BrowserActionRow.state == "started")
                    .values(
                        state="unknown",
                        success=None,
                        error="worker heartbeat expired before browser action outcome was recorded",
                    )
                )
                if started_mutation is not None or completed_mutation is not None:
                    row.status = "failed_unknown"
                    row.finished_at = _now()
                    row.failure_context = {
                        "status": "failed_unknown",
                        "run_id": row.id,
                        "reason": "worker_lost_after_mutating_browser_work",
                        "side_effect_state": "unknown",
                    }
                    unknown.append(row.id)
                else:
                    row.status = "queued"
                    row.worker_id = None
                    row.heartbeat_at = None
                    row.current_step = 0
                    row.failure_context = {
                        "status": "retrying",
                        "reason": "worker_heartbeat_expired_before_mutating_work",
                    }
                    requeued.append(row.id)
        return {
            "requeued": requeued,
            "failed_unknown": unknown,
            "repair_session_expired": expired_repairs,
        }

    async def get_workflow_version_by_id(self, version_id: str) -> WorkflowVersionRow | None:
        async with self.sessions() as session:
            return await session.get(WorkflowVersionRow, version_id)

    async def get_skill_by_id(self, skill_id: str) -> SkillRow | None:
        async with self.sessions() as session:
            return cast(SkillRow | None, await session.get(SkillRow, skill_id))

    async def add_step_execution(
        self,
        *,
        run_id: str,
        step_index: int,
        attempt: int,
        status: str,
        step: dict[str, Any],
        resolved_target: dict[str, Any] | None,
        result: dict[str, Any] | None,
        error: str | None,
        duration_ms: float,
    ) -> StepExecutionRow:
        async with self.sessions.begin() as session:
            row = StepExecutionRow(
                run_id=run_id,
                step_index=step_index,
                attempt=attempt,
                status=status,
                step=step,
                resolved_target=resolved_target,
                result=result,
                error=error,
                duration_ms=duration_ms,
            )
            session.add(row)
            await session.flush()
            return row

    async def create_repair(
        self,
        *,
        run_id: str,
        workflow_version_id: str,
        step_index: int,
        expected_target: dict[str, Any],
        replacement_target: dict[str, Any],
        candidate_id: str,
        persist_version: bool = True,
        status: str = "pending",
    ) -> RepairRow:
        async with self.sessions.begin() as session:
            row = RepairRow(
                run_id=run_id,
                workflow_version_id=workflow_version_id,
                step_index=step_index,
                expected_target=expected_target,
                replacement_target=replacement_target,
                candidate_id=candidate_id,
                persist_version=persist_version,
                status=status,
            )
            session.add(row)
            await session.flush()
            return row

    async def get_repair(self, repair_id: str) -> RepairRow | None:
        async with self.sessions() as session:
            return cast(RepairRow | None, await session.get(RepairRow, repair_id))

    async def pending_repair(self, run_id: str) -> RepairRow | None:
        async with self.sessions() as session:
            return cast(
                RepairRow | None,
                await session.scalar(
                    select(RepairRow)
                    .where(RepairRow.run_id == run_id, RepairRow.status == "pending")
                    .order_by(RepairRow.created_at)
                    .limit(1)
                ),
            )

    async def claim_pending_repair(self, run_id: str) -> RepairRow | None:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(RepairRow)
                .where(RepairRow.run_id == run_id, RepairRow.status == "pending")
                .order_by(RepairRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "applying"
            await session.flush()
            return row

    async def heartbeat_repair_session(self, run_id: str, worker_id: str) -> bool:
        async with self.sessions.begin() as session:
            updated_id = await session.scalar(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status == "repair_required",
                    RunRow.worker_id == worker_id,
                )
                .values(heartbeat_at=_now())
                .returning(RunRow.id)
            )
            return updated_id is not None

    async def expire_repair_session(self, run_id: str, *, worker_id: str | None = None) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(RunRow, run_id, with_for_update=True)
            if row is None or row.status != "repair_required":
                return
            if worker_id is not None and row.worker_id != worker_id:
                return
            context = dict(row.failure_context or {})
            context.update(
                {
                    "status": "repair_session_expired",
                    "reason": "live_browser_session_is_no_longer_available",
                    "session_available": False,
                }
            )
            row.status = "repair_session_expired"
            row.failure_context = context
            row.finished_at = _now()
            row.worker_id = None
            pending = await session.scalars(
                select(RepairRow).where(
                    RepairRow.run_id == run_id,
                    RepairRow.status.in_(["pending", "applying"]),
                )
            )
            for repair in pending:
                repair.status = "session_expired"
                repair.validation_result = context
                repair.completed_at = _now()

    async def complete_repair(
        self,
        repair_id: str,
        *,
        status: str,
        validation_result: dict[str, Any],
        new_workflow_version_id: str | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(RepairRow, repair_id)
            if row is None:
                raise KeyError(f"repair not found: {repair_id}")
            row.status = status
            row.validation_result = validation_result
            row.new_workflow_version_id = new_workflow_version_id
            row.completed_at = _now()

    async def audit(
        self,
        event_type: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(
                AuditEventRow(
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    data=data or {},
                )
            )
