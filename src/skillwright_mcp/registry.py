from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Annotated, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    create_model,
)

from .db import Database, SkillRow
from .runtime import Runtime
from .workflow import WorkflowDefinition, WorkflowInput

_PYTHON_TYPES: dict[str, Any] = {
    "string": StrictStr,
    "number": StrictFloat,
    "integer": StrictInt,
    "boolean": StrictBool,
}


class SkillToolRegistry:
    """Expose every persisted skill as a first-class dynamically registered MCP tool."""

    def __init__(self, server: MCPServer[Runtime]) -> None:
        self.server = server
        self._registered: set[str] = set()
        self._lock = asyncio.Lock()

    async def refresh_all(self, database: Database) -> None:
        async with self._lock:
            self._clear_unlocked()
            for skill in await database.list_skills():
                await self._refresh_skill_unlocked(database, skill.name)

    async def refresh_skill(self, database: Database, skill_name: str) -> None:
        async with self._lock:
            await self._refresh_skill_unlocked(database, skill_name)

    async def _refresh_skill_unlocked(self, database: Database, skill_name: str) -> None:
        stored = await database.get_workflow_version(skill_name)
        if stored is None:
            return
        skill, version = stored
        workflow = WorkflowDefinition.model_validate(version.definition)
        if skill.tool_name in self._registered:
            self.server.remove_tool(skill.tool_name)
        fn = _generated_callable(skill, workflow, version.version)
        self.server.add_tool(
            fn,
            name=skill.tool_name,
            title=skill.name,
            description=_tool_description(skill, workflow, version.version),
            meta={
                "skillwright": {
                    "skill": skill.name,
                    "version": version.version,
                    "generated": True,
                }
            },
            structured_output=True,
        )
        self._registered.add(skill.tool_name)

    async def clear(self) -> None:
        async with self._lock:
            self._clear_unlocked()

    def _clear_unlocked(self) -> None:
        for tool_name in self._registered:
            self.server.remove_tool(tool_name)
        self._registered.clear()

def _generated_callable(
    skill: SkillRow,
    workflow: WorkflowDefinition,
    version: int,
) -> Callable[..., Any]:
    return_type = _output_annotation(skill, workflow)

    async def invoke(**kwargs: Any) -> Any:
        ctx = kwargs.pop("ctx")
        runtime = ctx.request_context.lifespan_context
        return await runtime.engine.run_skill(skill.name, inputs=kwargs, version=version)

    parameters: list[inspect.Parameter] = [
        inspect.Parameter(
            "ctx",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Context[Runtime],
        )
    ]
    annotations: dict[str, Any] = {"ctx": Context[Runtime], "return": return_type}
    for input_name, spec in workflow.public_inputs().items():
        annotation = _input_annotation(spec)
        default: Any = inspect.Parameter.empty
        if spec.default is not None:
            default = spec.default
        elif not spec.required:
            default = None
        parameters.append(
            inspect.Parameter(
                input_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=default,
            )
        )
        annotations[input_name] = annotation

    invoke.__name__ = skill.tool_name
    invoke.__qualname__ = skill.tool_name
    invoke.__doc__ = workflow.description
    invoke.__annotations__ = annotations
    invoke.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=parameters,
        return_annotation=return_type,
    )
    return invoke


def _input_annotation(spec: WorkflowInput) -> Any:
    value_type: Any = _PYTHON_TYPES[spec.type]
    if not spec.required and spec.default is None:
        value_type = value_type | None
    metadata = Field(description=spec.description) if spec.description else Field()
    return Annotated[value_type, metadata]


def _output_annotation(skill: SkillRow, workflow: WorkflowDefinition) -> type[BaseModel]:
    if workflow.outputs:
        output_fields: dict[str, tuple[Any, Any]] = {
            f"value_{index}": (
                Annotated[
                    _PYTHON_TYPES[spec.type],
                    Field(
                        alias=name,
                        serialization_alias=name,
                        description=spec.description,
                    ),
                ],
                ...,
            )
            for index, (name, spec) in enumerate(workflow.outputs.items())
        }
        model_factory = cast(Any, create_model)
        outputs_type: Any = model_factory(
            f"{skill.tool_name}_outputs",
            __config__=ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True),
            **output_fields,
        )
    else:
        outputs_type = dict[str, Any]
    return create_model(
        f"{skill.tool_name}_result",
        __config__=ConfigDict(extra="allow"),
        status=(str, ...),
        outputs=(outputs_type | None, None),
    )


def _tool_description(skill: SkillRow, workflow: WorkflowDefinition, version: int) -> str:
    purpose = (
        workflow.description.strip() or f"Run the saved Skillwright automation {skill.name!r}."
    )
    return (
        f"{purpose}\n\n"
        f"Generated from persisted Skillwright skill {skill.name!r}; executes registered "
        f"version v{version} "
        "deterministically through Microsoft Playwright MCP. Secret inputs are resolved from local "
        "environment bindings and are intentionally absent from this tool schema."
    )
