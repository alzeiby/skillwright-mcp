from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from .workflow import WorkflowDefinition

_LEGACY_PLATFORM_TABLES = {
    "approvals",
    "audit_events",
    "principals",
    "repairs",
    "runs",
    "skill_permissions",
    "step_executions",
    "alembic_version",
}
_CORE_TABLES = {
    "recordings",
    "browser_actions",
    "skills",
    "workflow_versions",
    "skill_secret_bindings",
}
_BUSY_TIMEOUT_MS = 5_000


def generated_tool_name(skill_name: str) -> str:
    """Return a stable, collision-resistant MCP identifier for one persisted skill."""

    slug = re.sub(r"[^A-Za-z0-9_]+", "_", skill_name).strip("_").lower() or "automation"
    slug = re.sub(r"_+", "_", slug)[:72].rstrip("_") or "automation"
    digest = hashlib.sha256(skill_name.encode("utf-8")).hexdigest()[:8]
    return f"skillwright_{slug}_{digest}"


@dataclass(slots=True)
class BrowserActionRow:
    id: int
    tool_name: str
    arguments: dict[str, Any]
    durable_locator: str | None
    success: bool | None
    snapshot_before: str | None


@dataclass(slots=True)
class SkillRow:
    id: str
    name: str
    tool_name: str
    description: str
    current_version: int


@dataclass(slots=True)
class WorkflowVersionRow:
    skill_id: str
    version: int
    definition: dict[str, Any]
    parent_version: int | None
    change_reason: str
    created_at: datetime


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS browser_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        history_scope TEXT NOT NULL DEFAULT 'legacy',
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        durable_locator TEXT,
        success INTEGER,
        snapshot_before TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_browser_actions_history
    ON browser_actions(history_scope, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        tool_name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        current_version INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_versions (
        skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        definition TEXT NOT NULL,
        parent_version INTEGER,
        change_reason TEXT NOT NULL DEFAULT 'created',
        created_at TEXT NOT NULL,
        PRIMARY KEY(skill_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_secret_bindings (
        skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
        input_name TEXT NOT NULL,
        secret_ref TEXT NOT NULL,
        PRIMARY KEY(skill_id, input_name)
    )
    """,
)

class Database:
    """Direct local SQLite store for authoring history, immutable skills, and secret bindings."""

    def __init__(self, path: str | Path) -> None:
        if isinstance(path, str) and "://" in path:
            raise ValueError("Skillwright supports local SQLite paths only")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def _connect(self, *, foreign_keys: bool = True) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(str(self.path), timeout=_BUSY_TIMEOUT_MS / 1_000)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            await connection.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        async with self._connect(foreign_keys=False) as connection:
            try:
                # BEGIN IMMEDIATE serializes first-start creation/migration across separate local
                # Skillwright processes sharing one SQLite file.
                await connection.execute("BEGIN IMMEDIATE")
                schema_state = await _schema_state(connection)
                if schema_state == "unrelated":
                    raise ValueError(
                        "SQLite database is not empty and is not a recognized Skillwright database"
                    )
                if schema_state == "migrate":
                    await _migrate_local_schema(connection)
                else:
                    await _create_schema(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def close(self) -> None:
        # Connections are deliberately short-lived so concurrent MCP operations never share a
        # transaction. There is no pool or engine to tear down.
        return None

    async def bind_skill_secret(
        self,
        *,
        skill_id: str,
        input_name: str,
        secret_ref: str,
    ) -> None:
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO skill_secret_bindings
                    (skill_id, input_name, secret_ref)
                VALUES (?, ?, ?)
                ON CONFLICT(skill_id, input_name) DO UPDATE SET
                    secret_ref=excluded.secret_ref
                """,
                (skill_id, input_name, secret_ref),
            )
            await connection.commit()

    async def unbind_skill_secret(self, *, skill_id: str, input_name: str) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM skill_secret_bindings WHERE skill_id = ? AND input_name = ?",
                (skill_id, input_name),
            )
            await connection.commit()
            return cursor.rowcount > 0

    async def skill_secret_bindings(self, skill_id: str) -> dict[str, str]:
        async with self._connect() as connection:
            rows = await _fetch_all(
                connection,
                """
                SELECT input_name, secret_ref
                FROM skill_secret_bindings
                WHERE skill_id = ?
                ORDER BY input_name
                """,
                (skill_id,),
            )
        return {str(row["input_name"]): str(row["secret_ref"]) for row in rows}

    async def start_browser_action(
        self,
        *,
        history_scope: str,
        tool_name: str,
        arguments: dict[str, Any],
        snapshot_before: str | None,
        durable_locator: str | None,
    ) -> int:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO browser_actions (
                    history_scope, tool_name, arguments,
                    durable_locator, success, snapshot_before
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (
                    history_scope,
                    tool_name,
                    _dump_json(arguments),
                    durable_locator,
                    snapshot_before,
                ),
            )
            action_id = cursor.lastrowid
            if action_id is None:  # pragma: no cover - INTEGER PRIMARY KEY always returns an id
                raise RuntimeError("browser action insert did not produce an id")
            await connection.commit()
        return int(action_id)

    async def latest_action_id(self, history_scope: str) -> int:
        async with self._connect() as connection:
            row = await _fetch_one(
                connection,
                "SELECT max(id) AS latest_id FROM browser_actions WHERE history_scope = ?",
                (history_scope,),
            )
        assert row is not None
        latest = row["latest_id"]
        return int(latest) if latest is not None else 0

    async def finish_browser_action(
        self,
        action_id: int,
        *,
        success: bool,
    ) -> None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE browser_actions
                SET success = ?
                WHERE id = ?
                """,
                (
                    int(success),
                    action_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"browser action not found: {action_id}")
            await connection.commit()

    async def action_range(
        self,
        start_event: int,
        end_event: int,
        *,
        history_scope: str,
    ) -> Sequence[BrowserActionRow]:
        if end_event < start_event:
            raise ValueError("end_event must be >= start_event")
        async with self._connect() as connection:
            rows = await _fetch_all(
                connection,
                """
                SELECT * FROM browser_actions
                WHERE id >= ? AND id <= ? AND history_scope = ?
                ORDER BY id
                """,
                (start_event, end_event, history_scope),
            )
        return [_browser_action_from_row(row) for row in rows]

    async def create_skill_version(
        self,
        definition: WorkflowDefinition,
        *,
        parent_version: int | None = None,
        change_reason: str = "created",
        expected_current_version: int | None = None,
        secret_bindings: Mapping[str, str] | None = None,
    ) -> tuple[SkillRow, WorkflowVersionRow]:
        async with self._connect() as connection:
            try:
                # Serialize writers at the SQLite-file boundary. Separate local MCP processes may
                # legitimately share ~/.skillwright/skillwright.db.
                await connection.execute("BEGIN IMMEDIATE")
                skill_row = await _fetch_one(
                    connection,
                    "SELECT * FROM skills WHERE name = ?",
                    (definition.name,),
                )
                if skill_row is None:
                    if expected_current_version not in (None, 0):
                        raise ValueError("workflow no longer exists at the expected version")
                    skill = SkillRow(
                        id=str(uuid4()),
                        name=definition.name,
                        tool_name=generated_tool_name(definition.name),
                        description=definition.description,
                        current_version=0,
                    )
                    await connection.execute(
                        """
                        INSERT INTO skills (id, name, tool_name, description, current_version)
                        VALUES (?, ?, ?, ?, 0)
                        """,
                        (
                            skill.id,
                            skill.name,
                            skill.tool_name,
                            skill.description,
                        ),
                    )
                else:
                    skill = _skill_from_row(skill_row)

                if (
                    expected_current_version is not None
                    and skill.current_version != expected_current_version
                ):
                    raise ValueError(
                        f"stale workflow base: expected v{expected_current_version}, "
                        f"current is v{skill.current_version}"
                    )

                new_version_number = skill.current_version + 1
                now = datetime.now(UTC)
                version = WorkflowVersionRow(
                    skill_id=skill.id,
                    version=new_version_number,
                    definition=definition.model_dump(mode="json"),
                    parent_version=parent_version,
                    change_reason=change_reason,
                    created_at=now,
                )
                await connection.execute(
                    """
                    INSERT INTO workflow_versions (
                        skill_id, version, definition,
                        parent_version, change_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.skill_id,
                        version.version,
                        _dump_json(version.definition),
                        version.parent_version,
                        version.change_reason,
                        version.created_at.isoformat(),
                    ),
                )
                await connection.execute(
                    """
                    UPDATE skills
                    SET current_version = ?, description = ?
                    WHERE id = ?
                    """,
                    (
                        new_version_number,
                        definition.description,
                        skill.id,
                    ),
                )
                skill.current_version = new_version_number
                skill.description = definition.description

                for input_name, secret_ref in (secret_bindings or {}).items():
                    await connection.execute(
                        """
                        INSERT INTO skill_secret_bindings
                            (skill_id, input_name, secret_ref)
                        VALUES (?, ?, ?)
                        ON CONFLICT(skill_id, input_name) DO UPDATE SET
                            secret_ref=excluded.secret_ref
                        """,
                        (
                            skill.id,
                            input_name,
                            secret_ref,
                        ),
                    )

                await connection.commit()
                return skill, version
            except BaseException:
                await connection.rollback()
                raise

    async def list_skills(self) -> Sequence[SkillRow]:
        async with self._connect() as connection:
            rows = await _fetch_all(connection, "SELECT * FROM skills ORDER BY name")
        return [_skill_from_row(row) for row in rows]

    async def search_skills(self, query: str, limit: int = 10) -> Sequence[SkillRow]:
        pattern = f"%{query.strip().lower()}%"
        async with self._connect() as connection:
            rows = await _fetch_all(
                connection,
                """
                SELECT * FROM skills
                WHERE lower(name) LIKE ? OR lower(description) LIKE ?
                ORDER BY name
                LIMIT ?
                """,
                (pattern, pattern, limit),
            )
        return [_skill_from_row(row) for row in rows]

    async def get_skill(self, name: str) -> SkillRow | None:
        async with self._connect() as connection:
            row = await _fetch_one(connection, "SELECT * FROM skills WHERE name = ?", (name,))
        return _skill_from_row(row) if row is not None else None

    async def get_workflow_version(
        self, name: str, version: int | None = None
    ) -> tuple[SkillRow, WorkflowVersionRow] | None:
        async with self._connect() as connection:
            skill_row = await _fetch_one(connection, "SELECT * FROM skills WHERE name = ?", (name,))
            if skill_row is None:
                return None
            skill = _skill_from_row(skill_row)
            selected_version = skill.current_version if version is None else version
            version_row = await _fetch_one(
                connection,
                "SELECT * FROM workflow_versions WHERE skill_id = ? AND version = ?",
                (skill.id, selected_version),
            )
        if version_row is None:
            return None
        return skill, _workflow_version_from_row(version_row)

    async def workflow_versions(self, name: str) -> Sequence[WorkflowVersionRow]:
        async with self._connect() as connection:
            skill = await _fetch_one(connection, "SELECT id FROM skills WHERE name = ?", (name,))
            if skill is None:
                return []
            rows = await _fetch_all(
                connection,
                "SELECT * FROM workflow_versions WHERE skill_id = ? ORDER BY version DESC",
                (skill["id"],),
            )
        return [_workflow_version_from_row(row) for row in rows]


async def _fetch_one(
    connection: aiosqlite.Connection,
    statement: str,
    parameters: Sequence[Any] = (),
) -> aiosqlite.Row | None:
    cursor = await connection.execute(statement, parameters)
    return await cursor.fetchone()


async def _fetch_all(
    connection: aiosqlite.Connection,
    statement: str,
    parameters: Sequence[Any] = (),
) -> list[aiosqlite.Row]:
    cursor = await connection.execute(statement, parameters)
    return list(await cursor.fetchall())


async def _create_schema(connection: aiosqlite.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        await connection.execute(statement)


async def _table_names(connection: aiosqlite.Connection) -> set[str]:
    rows = await _fetch_all(
        connection,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    )
    return {str(row["name"]) for row in rows}


async def _table_columns(connection: aiosqlite.Connection, table: str) -> set[str]:
    safe_table = table.replace('"', '""')
    rows = await _fetch_all(connection, f'PRAGMA table_info("{safe_table}")')
    return {str(row["name"]) for row in rows}


async def _schema_state(
    connection: aiosqlite.Connection,
) -> Literal["new", "current", "migrate", "unrelated"]:
    tables = await _table_names(connection)
    if not tables:
        return "new"
    # Do not reinterpret arbitrary user tables merely because they use generic names like
    # ``skills`` or ``workflow_versions``. Local Skillwright schemas have a distinctive browser
    # history shape; the oldest supported service-era schema is identified by Skillwright-specific
    # owner metadata.
    if not {"skills", "workflow_versions"} <= tables:
        return "unrelated"
    skills_columns = await _table_columns(connection, "skills")
    version_columns = await _table_columns(connection, "workflow_versions")
    if not {"id", "name", "description", "current_version"} <= skills_columns:
        return "unrelated"
    if not {"skill_id", "version", "definition"} <= version_columns:
        return "unrelated"

    action_columns: set[str] = set()
    if "browser_actions" in tables:
        action_columns = await _table_columns(connection, "browser_actions")
    has_current_local_core = (
        "tool_name" in skills_columns
        and {
            "id",
            "history_scope",
            "tool_name",
            "arguments",
            "durable_locator",
            "success",
            "snapshot_before",
        }
        <= action_columns
    )
    has_previous_local_core = {"recordings", "browser_actions"} <= tables
    if has_previous_local_core:
        recording_columns = await _table_columns(connection, "recordings")
        has_previous_local_core = (
            {"id", "name", "description"} <= recording_columns
            and {"id", "recording_id", "tool_name", "arguments"} <= action_columns
        )
    has_legacy_service_signature = (
        "owner_principal_id" in skills_columns
        and bool(tables & {"principals", "runs", "approvals", "audit_events"})
    )
    if (
        not has_current_local_core
        and not has_previous_local_core
        and not has_legacy_service_signature
    ):
        return "unrelated"
    if version_columns & {"id", "schema_version", "created_from_recording_id"}:
        return "migrate"

    obsolete_columns = {
        "browser_actions": {
            "actor_principal_id",
            "recording_id",
            "run_id",
            "source",
            "upstream_tool_name",
            "upstream_arguments",
            "state",
            "result",
            "snapshot_after",
            "duration_ms",
            "error",
            "created_at",
        },
        "skills": {"owner_principal_id", "created_at", "updated_at"},
    }
    for table, unwanted in obsolete_columns.items():
        if table in tables and (await _table_columns(connection, table)) & unwanted:
            return "migrate"
    if "tool_name" not in skills_columns:
        return "migrate"
    if "skill_secret_bindings" in tables:
        binding_columns = await _table_columns(connection, "skill_secret_bindings")
        if binding_columns & {"id", "provider", "updated_at", "updated_by_principal_id"}:
            return "migrate"
    if (
        "browser_actions" in tables
        and "history_scope" not in await _table_columns(connection, "browser_actions")
    ):
        return "migrate"
    return "current"


async def _migrate_local_schema(connection: aiosqlite.Connection) -> None:
    """Preserve useful local data while collapsing older Skillwright schemas."""

    existing = await _table_names(connection)
    copied: dict[str, list[dict[str, Any]]] = {}
    columns_by_table: dict[str, tuple[str, ...]] = {
        "browser_actions": (
            "id",
            "history_scope",
            "tool_name",
            "arguments",
            "durable_locator",
            "success",
            "snapshot_before",
        ),
        "skills": ("id", "name", "description", "current_version"),
        "workflow_versions": (
            "skill_id",
            "version",
            "definition",
            "parent_version",
            "change_reason",
            "created_at",
        ),
        "skill_secret_bindings": (
            "skill_id",
            "input_name",
            "provider",
            "secret_ref",
        ),
    }
    for table, wanted in columns_by_table.items():
        if table not in existing:
            continue
        actual = await _table_columns(connection, table)
        selected = [column for column in wanted if column in actual]
        if not selected:
            continue
        quoted = ", ".join(f'"{column}"' for column in selected)
        statement = f'SELECT {quoted} FROM "{table}"'
        if table == "browser_actions" and "source" in actual:
            statement += " WHERE source = 'agent'"
        rows = await _fetch_all(connection, statement)
        copied[table] = [dict(row) for row in rows]

    for table in sorted(existing & (_CORE_TABLES | _LEGACY_PLATFORM_TABLES)):
        await connection.execute(f'DROP TABLE IF EXISTS "{table}"')
    await _create_schema(connection)

    for row in copied.get("skills", []):
        row["tool_name"] = generated_tool_name(str(row["name"]))
        await _insert_mapping(connection, "skills", row)
    for row in copied.get("workflow_versions", []):
        definition = _decode_json(row.get("definition"))
        if isinstance(definition, dict):
            definition["schema_version"] = min(int(definition.get("schema_version", 1)), 4)
            for step in definition.get("steps", []):
                if isinstance(step, dict):
                    step.pop("approval", None)
            definition.setdefault("outputs", {})
            row["definition"] = _dump_json(definition)
        await _insert_mapping(connection, "workflow_versions", row)
    for row in copied.get("skill_secret_bindings", []):
        provider = row.get("provider", "env")
        if provider != "env":
            continue
        await _insert_mapping(
            connection,
            "skill_secret_bindings",
            {
                "skill_id": row["skill_id"],
                "input_name": row["input_name"],
                "secret_ref": row["secret_ref"],
            },
        )
    for row in copied.get("browser_actions", []):
        row.setdefault("history_scope", "legacy")
        await _insert_mapping(connection, "browser_actions", row)


async def _insert_mapping(
    connection: aiosqlite.Connection,
    table: str,
    row: Mapping[str, Any],
) -> None:
    columns = list(row)
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    values = [
        _dump_json(_decode_json(row[column]))
        if column in {"arguments", "definition"}
        else row[column]
        for column in columns
    ]
    await connection.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        values,
    )


def _dump_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _decode_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid legacy SQLite timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _browser_action_from_row(row: Any) -> BrowserActionRow:
    success = row["success"]
    return BrowserActionRow(
        id=int(row["id"]),
        tool_name=str(row["tool_name"]),
        arguments=dict(_decode_json(row["arguments"])),
        durable_locator=str(row["durable_locator"]) if row["durable_locator"] is not None else None,
        success=bool(success) if success is not None else None,
        snapshot_before=str(row["snapshot_before"]) if row["snapshot_before"] is not None else None,
    )


def _skill_from_row(row: Any) -> SkillRow:
    return SkillRow(
        id=str(row["id"]),
        name=str(row["name"]),
        tool_name=str(row["tool_name"]),
        description=str(row["description"]),
        current_version=int(row["current_version"]),
    )


def _workflow_version_from_row(row: Any) -> WorkflowVersionRow:
    created_at = _decode_datetime(row["created_at"])
    assert created_at is not None
    definition = _decode_json(row["definition"])
    if not isinstance(definition, dict):
        raise ValueError("workflow version definition is not a JSON object")
    return WorkflowVersionRow(
        skill_id=str(row["skill_id"]),
        version=int(row["version"]),
        definition=definition,
        parent_version=int(row["parent_version"]) if row["parent_version"] is not None else None,
        change_reason=str(row["change_reason"]),
        created_at=created_at,
    )
