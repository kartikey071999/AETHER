"""Pure normalisation of Pydantic JSON schemas for strict structured-output modes.

Strict modes (Azure OpenAI ``json_schema`` with ``strict``, Anthropic ``output_config.format``)
reject schemas that Pydantic emits by default. Unsupported constraints are dropped here; the
caller still validates the model output against the original Pydantic model.
"""

from copy import deepcopy
from typing import Any, Literal

Flavour = Literal["openai", "anthropic", "generic"]

_NULL: dict[str, Any] = {"type": "null"}
_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
_LENGTH_AND_RANGE = frozenset(
    {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "maxItems",
        "uniqueItems",
        "minItems",
        "minProperties",
        "maxProperties",
        "contentEncoding",
        "contentMediaType",
        "examples",
        "default",
    }
)
_SCHEMA_VALUES = ("items", "not", "additionalProperties", "contains")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")
_REF_PREFIXES = ("#/$defs/", "#/definitions/")


def strict_json_schema(schema: dict[str, Any], *, flavour: Flavour) -> dict[str, Any]:
    """Return a copy of ``schema`` accepted by the provider's strict structured-output mode.

    * ``generic``: refs are inlined and every object gets ``additionalProperties: false``.
    * ``anthropic``: additionally drops range/length/default keywords and ``oneOf``.
    * ``openai``: as anthropic, plus every property is required and originally optional
      properties become nullable (``anyOf`` with ``null``).

    Recursive refs cannot be inlined; their definitions are kept (and normalised) in ``$defs``.
    """
    return _Normaliser(schema, flavour).run()


class _Normaliser:
    def __init__(self, schema: dict[str, Any], flavour: Flavour) -> None:
        if flavour not in ("openai", "anthropic", "generic"):
            raise ValueError(f"unknown schema flavour: {flavour}")
        self.flavour = flavour
        self.root = deepcopy(schema)
        self.defs: dict[str, Any] = {
            **self.root.pop("definitions", {}),
            **self.root.pop("$defs", {}),
        }
        self.kept: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        result = self._schema(self._inline(self.root, ()))
        pending = list(self.kept)
        done: set[str] = set()
        output: dict[str, Any] = {}
        while pending:
            name = pending.pop()
            if name in done:
                continue
            done.add(name)
            output[name] = self._schema(self._inline(deepcopy(self.defs[name]), (name,)))
            pending.extend(key for key in self.kept if key not in done)
        if output:
            result["$defs"] = output
        return result

    def _inline(self, node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [self._inline(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = next((ref.removeprefix(p) for p in _REF_PREFIXES if ref.startswith(p)), None)
            if name is None or name not in self.defs:
                raise ValueError(f"unresolvable schema reference: {ref}")
            siblings = {k: self._inline(v, stack) for k, v in node.items() if k != "$ref"}
            if name in stack:
                self.kept[name] = True
                return {"$ref": f"#/$defs/{name}", **siblings}
            resolved = self._inline(deepcopy(self.defs[name]), (*stack, name))
            return {**resolved, **siblings}
        return {
            key: (
                {name: self._inline(value, stack) for name, value in item.items()}
                if key == "properties" and isinstance(item, dict)
                else item
                if key in ("enum", "const", "default", "examples")
                else self._inline(item, stack)
            )
            for key, item in node.items()
        }

    def _schema(self, node: dict[str, Any]) -> dict[str, Any]:
        strict = self.flavour != "generic"
        result = dict(node)
        if strict:
            for key in _LENGTH_AND_RANGE & result.keys():
                if key == "minItems" and result[key] in (0, 1) and self.flavour == "anthropic":
                    continue
                del result[key]
            if "format" in result and result["format"] not in _FORMATS:
                del result["format"]
            if "oneOf" in result:
                result["anyOf"] = [*result.get("anyOf", []), *result.pop("oneOf")]
        for key in _SCHEMA_VALUES:
            if isinstance(result.get(key), dict):
                result[key] = self._schema(result[key])
        for key in _SCHEMA_LISTS:
            if isinstance(result.get(key), list):
                result[key] = [
                    self._schema(item) if isinstance(item, dict) else item for item in result[key]
                ]
        if "$defs" in result and isinstance(result["$defs"], dict):
            result["$defs"] = {name: self._schema(item) for name, item in result["$defs"].items()}
        properties = result.get("properties")
        if isinstance(properties, dict):
            required = set(result.get("required", []))
            result["properties"] = {
                name: self._property(item, name in required or self.flavour != "openai")
                for name, item in properties.items()
            }
            if self.flavour == "openai":
                result["required"] = list(properties)
        if result.get("type") == "object" or isinstance(properties, dict):
            result["additionalProperties"] = False
            if self.flavour == "openai":
                result.setdefault("properties", {})
                result.setdefault("required", [])
        return result

    def _property(self, node: Any, required: bool) -> Any:
        if not isinstance(node, dict):
            return node
        converted = self._schema(node)
        if required or _nullable(converted):
            return converted
        return {"anyOf": [converted, dict(_NULL)]}


def _nullable(node: dict[str, Any]) -> bool:
    kind = node.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    if None in node.get("enum", []):
        return True
    return any(_nullable(item) for item in node.get("anyOf", []) if isinstance(item, dict))
