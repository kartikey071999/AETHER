"""Declarative workflows: a bounded, validated sequence of skill steps passing data by reference.

References are ``${input.<name>...}`` or ``${steps.<id>.output.<field>...}``; a string that is
exactly one reference is replaced by the referenced value, otherwise references are interpolated
as text. Steps may only reference inputs and *earlier* steps, so every workflow is acyclic.
"""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aether.communication.messages import AgentId
from aether.skills.registry import SkillRegistry

MAX_STEPS = 30
REFERENCE = re.compile(r"\$\{([^}]+)\}")
_ID = r"^[a-z][a-z0-9_]{0,63}$"
_NAME = r"^[a-z][a-z0-9_.-]{1,119}$"
INPUT_TYPES = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


class WorkflowDefinitionError(ValueError):
    pass


class WorkflowStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID)
    skill: str = Field(min_length=1, max_length=120)
    inputs: dict[str, Any] = Field(default_factory=dict)
    # Run the step only if this reference resolves to a truthy value.
    when: str | None = Field(default=None, max_length=300)
    retries: int = Field(default=0, ge=0, le=3)
    on_error: Literal["fail", "continue"] = "fail"


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_NAME)
    version: int = Field(default=1, ge=1)
    owner: AgentId
    description: str = Field(default="", max_length=2000)
    inputs: dict[str, Literal["str", "int", "float", "bool", "list", "dict"]] = Field(
        default_factory=dict
    )
    steps: list[WorkflowStep] = Field(min_length=1, max_length=MAX_STEPS)
    outputs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _structure(self) -> "WorkflowDefinition":
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id}")
            for reference in _references(step.inputs) + _references(step.when):
                _check_reference(reference, self.inputs, seen, where=f"step {step.id}")
            seen.add(step.id)
        for name, value in self.outputs.items():
            for reference in _references(value):
                _check_reference(reference, self.inputs, seen, where=f"output {name}")
        return self

    def validate_skills(self, registry: SkillRegistry) -> None:
        """Every step's skill exists and the owning agent may use it."""
        for step in self.steps:
            skill = registry.skills.get(step.skill)
            if skill is None:
                raise WorkflowDefinitionError(f"step {step.id}: unknown skill {step.skill}")
            if self.owner not in skill.owners:
                raise WorkflowDefinitionError(
                    f"step {step.id}: {self.owner.value} may not use skill {step.skill}"
                )

    def check_inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        missing = sorted(set(self.inputs) - set(values))
        extra = sorted(set(values) - set(self.inputs))
        if missing or extra:
            raise WorkflowDefinitionError(f"inputs: missing {missing}, unexpected {extra}")
        for name, kind in self.inputs.items():
            if not isinstance(values[name], INPUT_TYPES[kind]) or (
                kind in {"int", "float"} and isinstance(values[name], bool)
            ):
                raise WorkflowDefinitionError(f"input {name} must be {kind}")
        return values

    @property
    def definition_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _references(value: Any) -> list[str]:
    if isinstance(value, str):
        return [match.strip() for match in REFERENCE.findall(value)]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _references(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _references(item)]
    return []


def _check_reference(
    reference: str, inputs: dict[str, str], steps: set[str], *, where: str
) -> None:
    parts = reference.split(".")
    if parts[0] == "input" and len(parts) >= 2 and parts[1] in inputs:
        return
    if parts[0] == "steps" and len(parts) >= 3 and parts[1] in steps and parts[2] == "output":
        return
    raise ValueError(f"{where}: invalid reference ${{{reference}}} (only inputs and earlier steps)")


_MISSING = object()


def resolve(value: Any, scope: dict[str, Any]) -> Any:
    """Replace references in ``value`` with data from ``scope`` (``input`` and ``steps``)."""
    if isinstance(value, dict):
        return {key: resolve(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, scope) for item in value]
    if not isinstance(value, str):
        return value
    whole = REFERENCE.fullmatch(value.strip())
    if whole:
        return lookup(whole.group(1).strip(), scope)
    return REFERENCE.sub(lambda match: str(lookup(match.group(1).strip(), scope)), value)


def lookup(reference: str, scope: dict[str, Any]) -> Any:
    current: Any = scope
    for part in reference.split("."):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            current = _MISSING
        if current is _MISSING:
            return None
    return current
