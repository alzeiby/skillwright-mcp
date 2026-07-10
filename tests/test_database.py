from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from skillwright_mcp.db import Database, generated_tool_name
from skillwright_mcp.workflow import NavigateStep, WorkflowDefinition, WorkflowInput


def _table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")')
        return {str(row[1]) for row in rows}
    finally:
        connection.close()


def _indexes(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_versions_are_append_only_and_stale_base_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    await database.initialize()
    try:
        workflow = WorkflowDefinition(
            name="invoice",
            steps=[NavigateStep(url="https://example.test")],
        )
        skill, first = await database.create_skill_version(workflow)
        assert skill.current_version == 1
        _, second = await database.create_skill_version(
            workflow,
            parent_version=1,
            expected_current_version=1,
        )
        assert second.version == 2
        with pytest.raises(ValueError, match="stale workflow base"):
            await database.create_skill_version(
                workflow,
                parent_version=1,
                expected_current_version=1,
            )
        versions = await database.workflow_versions("invoice")
        assert [row.version for row in versions] == [2, 1]
        assert first.skill_id == second.skill_id == skill.id
    finally:
        await database.close()


def test_database_rejects_postgres_and_other_remote_backends() -> None:
    with pytest.raises(ValueError, match="local SQLite paths only"):
        Database("postgresql+asyncpg://localhost/skillwright")


@pytest.mark.asyncio
async def test_fresh_schema_contains_only_local_product_entities(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    database = Database(path)
    await database.initialize()
    try:
        tables = _table_names(path)
        assert tables == {
            "browser_actions",
            "skill_secret_bindings",
            "skills",
            "workflow_versions",
        }
        assert not {
            "runs",
            "repairs",
            "approvals",
            "principals",
            "step_executions",
            "audit_events",
            "skill_permissions",
        } & tables
        assert _columns(path, "browser_actions") == {
            "id",
            "history_scope",
            "tool_name",
            "arguments",
            "durable_locator",
            "success",
            "snapshot_before",
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_first_start_serializes_schema_creation(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-start.sqlite"
    first = Database(path)
    second = Database(path)
    try:
        await asyncio.gather(first.initialize(), second.initialize())
        tables = _table_names(path)
        assert tables == {
            "browser_actions",
            "skill_secret_bindings",
            "skills",
            "workflow_versions",
        }
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
async def test_secret_binding_upsert_is_safe_across_database_instances(tmp_path: Path) -> None:
    path = tmp_path / "binding-upsert.sqlite"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        skill, _ = await first.create_skill_version(
            WorkflowDefinition(
                name="login",
                inputs={"password": WorkflowInput(secret=True)},
                steps=[NavigateStep(url="https://example.test")],
            )
        )
        await asyncio.gather(
            first.bind_skill_secret(
                skill_id=skill.id,
                input_name="password",
                secret_ref="FIRST_PASSWORD",
            ),
            second.bind_skill_secret(
                skill_id=skill.id,
                input_name="password",
                secret_ref="SECOND_PASSWORD",
            ),
        )
        bindings = await first.skill_secret_bindings(skill.id)
        assert bindings["password"] in {"FIRST_PASSWORD", "SECOND_PASSWORD"}
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
async def test_legacy_sqlite_keeps_skills_but_drops_platform_control_tables(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE principals (id TEXT PRIMARY KEY, external_key TEXT);
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                current_version INTEGER NOT NULL,
                owner_principal_id TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE workflow_versions (
                id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                created_from_recording_id TEXT,
                parent_version INTEGER,
                change_reason TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE approvals (id TEXT PRIMARY KEY, run_id TEXT);
            CREATE TABLE unrelated_notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
            INSERT INTO unrelated_notes(body) VALUES ('keep me');
            """
        )
        definition = {
            "schema_version": 3,
            "name": "legacy login",
            "description": "preserved automation",
            "inputs": {},
            "steps": [
                {
                    "op": "click",
                    "target": {"role": "button", "name": "Continue"},
                    "approval": {"reason": "old platform gate"},
                }
            ],
        }
        connection.execute(
            "INSERT INTO skills VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("skill-1", "legacy login", "preserved automation", 1, "principal-1"),
        )
        connection.execute(
            "INSERT INTO workflow_versions VALUES "
            "(?, ?, ?, ?, ?, NULL, NULL, ?, CURRENT_TIMESTAMP)",
            ("version-1", "skill-1", 1, 3, json.dumps(definition), "legacy"),
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    await database.initialize()
    try:
        skill = await database.get_skill("legacy login")
        assert skill is not None
        assert skill.tool_name == generated_tool_name("legacy login")
        stored = await database.get_workflow_version("legacy login")
        assert stored is not None
        migrated = WorkflowDefinition.model_validate(stored[1].definition)
        assert migrated.name == "legacy login"
        assert "approval" not in migrated.model_dump_json()

        tables = _table_names(path)
        assert "principals" not in tables
        assert "runs" not in tables
        assert "approvals" not in tables
        assert "unrelated_notes" in tables
        connection = sqlite3.connect(path)
        try:
            note = connection.execute(
                "SELECT body FROM unrelated_notes WHERE id = 1"
            ).fetchone()
            assert note == ("keep me",)
        finally:
            connection.close()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_previous_local_schema_migrates_to_lean_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / "previous-local.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                tool_name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                current_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE workflow_versions (
                id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                created_from_recording_id TEXT,
                parent_version INTEGER,
                change_reason TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                UNIQUE(skill_id, version)
            );
            CREATE TABLE recordings (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'recording',
                started_at TEXT NOT NULL,
                stopped_at TEXT
            );
            CREATE TABLE browser_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id TEXT,
                history_scope TEXT NOT NULL DEFAULT 'legacy',
                source TEXT NOT NULL DEFAULT 'agent',
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                upstream_tool_name TEXT NOT NULL,
                upstream_arguments TEXT NOT NULL,
                durable_locator TEXT,
                state TEXT NOT NULL DEFAULT 'started',
                result TEXT NOT NULL DEFAULT '{}',
                success INTEGER,
                error TEXT,
                snapshot_before TEXT,
                snapshot_after TEXT,
                duration_ms REAL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE skill_secret_bindings (
                id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL,
                input_name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'env',
                secret_ref TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(skill_id, input_name)
            );
            CREATE INDEX ix_recordings_status ON recordings(status);
            CREATE INDEX ix_browser_actions_state ON browser_actions(state);
            """
        )
        definition = WorkflowDefinition(
            name="local secret",
            inputs={"password": WorkflowInput(type="string", secret=True)},
            steps=[NavigateStep(url="https://example.test")],
        )
        connection.execute(
            "INSERT INTO skills VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                "skill-local",
                "local secret",
                generated_tool_name("local secret"),
                "",
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_versions VALUES
                (?, ?, ?, ?, ?, NULL, NULL, ?, CURRENT_TIMESTAMP)
            """,
            (
                "version-local",
                "skill-local",
                1,
                definition.schema_version,
                definition.model_dump_json(),
                "created",
            ),
        )
        connection.execute(
            """
            INSERT INTO skill_secret_bindings VALUES
                (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            ("binding-local", "skill-local", "password", "env", "LOGIN_PASSWORD"),
        )
        connection.execute(
            """
            INSERT INTO recordings VALUES
                (
                    'recording-local', 'local secret', '', 'stopped',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """
        )
        connection.execute(
            """
            INSERT INTO browser_actions (
                recording_id, history_scope, source, tool_name, arguments,
                upstream_tool_name, upstream_arguments, durable_locator, state, result,
                success, error, snapshot_before, snapshot_after, duration_ms, created_at
            ) VALUES (?, ?, 'agent', 'browser_click', ?, 'browser_click', ?, ?, 'succeeded',
                      '{}', 1, NULL, ?, NULL, 12.5, CURRENT_TIMESTAMP)
            """,
            (
                "recording-local",
                "scope-local",
                json.dumps({"target": "e1", "element": "Continue"}),
                json.dumps({"target": "e1", "element": "Continue"}),
                "getByRole('button', { name: 'Continue' })",
                "- button \"Continue\" [ref=e1]",
            ),
        )
        connection.execute(
            """
            INSERT INTO browser_actions (
                recording_id, history_scope, source, tool_name, arguments,
                upstream_tool_name, upstream_arguments, durable_locator, state, result,
                success, error, snapshot_before, snapshot_after, duration_ms, created_at
            ) VALUES (NULL, 'replay-old', 'replay', 'browser_click', ?, 'browser_click', ?, ?,
                      'succeeded', '{}', 1, NULL, ?, NULL, 8.0, CURRENT_TIMESTAMP)
            """,
            (
                json.dumps({"target": "e2", "element": "Replay only"}),
                json.dumps({"target": "e2", "element": "Replay only"}),
                "getByRole('button', { name: 'Replay only' })",
                "- button \"Replay only\" [ref=e2]",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    await database.initialize()
    try:
        skill = await database.get_skill("local secret")
        assert skill is not None
        bindings = await database.skill_secret_bindings(skill.id)
        assert bindings == {"password": "LOGIN_PASSWORD"}
        assert _columns(path, "skill_secret_bindings") == {
            "skill_id",
            "input_name",
            "secret_ref",
        }
        assert "recordings" not in _table_names(path)
        assert _columns(path, "browser_actions") == {
            "id",
            "history_scope",
            "tool_name",
            "arguments",
            "durable_locator",
            "success",
            "snapshot_before",
        }
        actions = await database.action_range(1, 1, history_scope="scope-local")
        assert len(actions) == 1
        assert actions[0].tool_name == "browser_click"
        assert actions[0].arguments == {"target": "e1", "element": "Continue"}
        assert actions[0].success is True
        assert actions[0].durable_locator == "getByRole('button', { name: 'Continue' })"
        connection = sqlite3.connect(path)
        try:
            action_count = connection.execute("SELECT count(*) FROM browser_actions").fetchone()[0]
        finally:
            connection.close()
        assert action_count == 1
        indexes = _indexes(path)
        assert "ix_browser_actions_history" in indexes
        assert "ix_recordings_status" not in indexes
        assert "ix_browser_actions_recording_id" not in indexes
        assert "ix_browser_actions_state" not in indexes
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recording_table_schema_migrates_to_history_only(tmp_path: Path) -> None:
    path = tmp_path / "recording-table.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                tool_name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                current_version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE workflow_versions (
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                parent_version INTEGER,
                change_reason TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                PRIMARY KEY(skill_id, version)
            );
            CREATE TABLE recordings (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE browser_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id TEXT,
                history_scope TEXT NOT NULL DEFAULT 'legacy',
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                durable_locator TEXT,
                success INTEGER,
                snapshot_before TEXT
            );
            INSERT INTO recordings VALUES ('recording-1', 'login', '');
            INSERT INTO browser_actions (
                recording_id, history_scope, tool_name, arguments,
                durable_locator, success, snapshot_before
            ) VALUES (
                'recording-1', 'scope-1', 'browser_click',
                '{"target":"e1","element":"Continue"}',
                'getByRole(''button'', { name: ''Continue'' })', 1,
                '- button "Continue" [ref=e1]'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    await database.initialize()
    try:
        assert _table_names(path) == {
            "browser_actions",
            "skill_secret_bindings",
            "skills",
            "workflow_versions",
        }
        assert _columns(path, "browser_actions") == {
            "id",
            "history_scope",
            "tool_name",
            "arguments",
            "durable_locator",
            "success",
            "snapshot_before",
        }
        actions = await database.action_range(1, 1, history_scope="scope-1")
        assert len(actions) == 1
        assert actions[0].tool_name == "browser_click"
        assert actions[0].arguments == {"target": "e1", "element": "Continue"}
        assert actions[0].success is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unrelated_sqlite_table_names_are_never_treated_as_legacy_skillwright(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unrelated.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE runs (id INTEGER PRIMARY KEY, note TEXT NOT NULL);
            CREATE TABLE principals (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO runs(note) VALUES ('foreign run');
            INSERT INTO principals(label) VALUES ('foreign principal');
            INSERT INTO alembic_version(version_num) VALUES ('foreign-head');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    with pytest.raises(ValueError, match="not a recognized Skillwright database"):
        await database.initialize()
    try:
        tables = _table_names(path)
        connection = sqlite3.connect(path)
        try:
            run_note = connection.execute("SELECT note FROM runs WHERE id = 1").fetchone()[0]
            principal_label = connection.execute(
                "SELECT label FROM principals WHERE id = 1"
            ).fetchone()[0]
            foreign_head = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        finally:
            connection.close()
        assert tables == {"runs", "principals", "alembic_version"}
        assert run_note == "foreign run"
        assert principal_label == "foreign principal"
        assert foreign_head == "foreign-head"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unrelated_skills_table_is_not_rewritten_as_skillwright(tmp_path: Path) -> None:
    path = tmp_path / "foreign-skills.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE skills (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            INSERT INTO skills(name, payload) VALUES ('foreign', 'KEEP_PAYLOAD');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    with pytest.raises(ValueError, match="not a recognized Skillwright database"):
        await database.initialize()
    try:
        columns = _columns(path, "skills")
        connection = sqlite3.connect(path)
        try:
            payload = connection.execute(
                "SELECT payload FROM skills WHERE name = 'foreign'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert _table_names(path) == {"skills"}
        assert columns == {"id", "name", "payload"}
        assert payload == "KEEP_PAYLOAD"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unrelated_skills_and_versions_pair_is_not_rewritten_as_skillwright(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foreign-skill-versions.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                current_version INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE workflow_versions (
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            INSERT INTO skills VALUES ('foreign-1', 'foreign', 'foreign', 1, 'KEEP_SKILL');
            INSERT INTO workflow_versions VALUES ('foreign-1', 1, '{}', 'KEEP_VERSION');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    with pytest.raises(ValueError, match="not a recognized Skillwright database"):
        await database.initialize()
    try:
        connection = sqlite3.connect(path)
        try:
            skill_payload = connection.execute(
                "SELECT payload FROM skills WHERE id = 'foreign-1'"
            ).fetchone()[0]
            version_payload = connection.execute(
                "SELECT payload FROM workflow_versions WHERE skill_id = 'foreign-1'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert _table_names(path) == {"skills", "workflow_versions"}
        assert skill_payload == "KEEP_SKILL"
        assert version_payload == "KEEP_VERSION"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unrelated_sqlite_user_version_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "foreign-version.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA user_version=77;
            CREATE TABLE foreign_data (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            INSERT INTO foreign_data(payload) VALUES ('keep');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    with pytest.raises(ValueError, match="not a recognized Skillwright database"):
        await database.initialize()
    try:
        connection = sqlite3.connect(path)
        try:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            payload = connection.execute(
                "SELECT payload FROM foreign_data WHERE id = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        assert _table_names(path) == {"foreign_data"}
        assert user_version == 77
        assert payload == "keep"
    finally:
        await database.close()

