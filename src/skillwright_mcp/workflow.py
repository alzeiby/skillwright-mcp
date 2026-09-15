from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WORKFLOW_SCHEMA_VERSION = 2
_TEMPLATE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowInput(StrictModel):
    type: Literal["string", "number", "integer", "boolean"] = "string"
    description: str | None = None
    required: bool = True
    default: str | float | int | bool | None = None


class ParameterBinding(StrictModel):
    step: int = Field(ge=0)
    field: Literal["url", "value", "select_value", "text", "text_gone"]
    input_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    input_type: Literal["string", "number", "integer", "boolean"] = "string"
    description: str | None = None
    item_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_item_index(self) -> ParameterBinding:
        if self.field == "select_value" and self.item_index is None:
            raise ValueError("select_value binding requires item_index")
        if self.field != "select_value" and self.item_index is not None:
            raise ValueError("item_index is only valid for select_value bindings")
        return self


class ElementTarget(StrictModel):
    """Durable semantic evidence used to resolve an element during replay."""

    role: str | None = None
    name: str | None = None
    locator: str | None = None
    label: str | None = None
    text: str | None = None
    stable_attributes: dict[str, str] = Field(default_factory=dict)
    nearby_text: list[str] = Field(default_factory=list)
    page_url_prefix: str | None = None
    recorded_description: str | None = None

    @model_validator(mode="after")
    def require_identity_signal(self) -> ElementTarget:
        if not any(
            (
                self.role,
                self.name,
                self.locator,
                self.label,
                self.text,
                self.stable_attributes,
                self.recorded_description,
            )
        ):
            raise ValueError("element target requires at least one identity signal")
        return self


class ApprovalGate(StrictModel):
    reason: str = Field(min_length=1, max_length=500)


class NavigateStep(StrictModel):
    op: Literal["navigate"] = "navigate"
    url: str


class ClickStep(StrictModel):
    op: Literal["click"] = "click"
    target: ElementTarget
    double_click: bool = False
    button: Literal["left", "right", "middle"] = "left"
    approval: ApprovalGate | None = None


class FillStep(StrictModel):
    op: Literal["fill"] = "fill"
    target: ElementTarget
    value: str
    submit: bool = False
    approval: ApprovalGate | None = None


class SelectStep(StrictModel):
    op: Literal["select"] = "select"
    target: ElementTarget
    values: list[str]
    approval: ApprovalGate | None = None


class WaitStep(StrictModel):
    op: Literal["wait"] = "wait"
    seconds: float | None = Field(default=None, ge=0)
    text: str | None = None
    text_gone: str | None = None

    @model_validator(mode="after")
    def require_wait_condition(self) -> WaitStep:
        if self.seconds is None and self.text is None and self.text_gone is None:
            raise ValueError("wait step requires seconds, text, or text_gone")
        return self


class ExtractStep(StrictModel):
    op: Literal["extract"] = "extract"
    target: ElementTarget
    save_as: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    attribute: str | None = None


class AssertStep(StrictModel):
    op: Literal["assert"] = "assert"
    target: ElementTarget | None = None
    url_contains: str | None = None
    title_contains: str | None = None
    text_contains: str | None = None

    @model_validator(mode="after")
    def require_assertion(self) -> AssertStep:
        if not any((self.target, self.url_contains, self.title_contains, self.text_contains)):
            raise ValueError("assert step requires at least one assertion")
        return self


WorkflowStep = Annotated[
    NavigateStep | ClickStep | FillStep | SelectStep | WaitStep | ExtractStep | AssertStep,
    Field(discriminator="op"),
]


class WorkflowDefinition(StrictModel):
    schema_version: Literal[1, 2] = 2
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    inputs: dict[str, WorkflowInput] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(min_length=1)

    def prepare_inputs(self, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
        supplied = dict(supplied or {})
        unknown = sorted(set(supplied) - set(self.inputs))
        if unknown:
            raise ValueError(f"unknown workflow inputs: {', '.join(unknown)}")

        result: dict[str, Any] = {}
        for name, spec in self.inputs.items():
            if name in supplied:
                value = supplied[name]
            elif spec.default is not None:
                value = spec.default
            elif spec.required:
                raise ValueError(f"missing required workflow input: {name}")
            else:
                value = None
            result[name] = _coerce_input(name, spec, value)
        return result


def _coerce_input(name: str, spec: WorkflowInput, value: Any) -> Any:
    if value is None and not spec.required:
        return None
    if spec.type == "string":
        if not isinstance(value, str):
            raise ValueError(f"input {name!r} must be a string")
        return value
    if spec.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"input {name!r} must be a boolean")
        return value
    if spec.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"input {name!r} must be an integer")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"input {name!r} must be a number")
    return value


def render_template(value: str, variables: dict[str, Any]) -> str:
    """Render only {{ variable }} placeholders; no arbitrary template execution."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise ValueError(f"unknown template variable: {name}")
        rendered = variables[name]
        return "" if rendered is None else str(rendered)

    return _TEMPLATE_RE.sub(replace, value)
