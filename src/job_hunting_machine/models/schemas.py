"""Pydantic-to-strict-JSON schema conversion and independent response validation."""

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import BaseModel, ConfigDict, ValidationError


class StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(mode="validation")
    if schema.get("type") != "object":
        raise ValueError("Structured output requires an object root")

    def visit(node: object) -> None:
        if isinstance(node, list):
            for value in node:
                visit(value)
        elif isinstance(node, dict):
            if "$ref" in node and not str(node["$ref"]).startswith("#/"):
                raise ValueError("Only local schema references are supported")
            node.pop("default", None)
            if node.get("type") == "object":
                if node.get("additionalProperties") not in (None, False):
                    raise ValueError("Open-ended dictionaries are not supported by strict outputs")
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)

    visit(schema)
    Draft202012Validator.check_schema(schema)
    return schema


class StructuredValidationError(ValueError):
    """Output failed JSON, schema, or Pydantic validation; text is deliberately omitted."""


def validate_output[T: BaseModel](text: str, model: type[T], schema: dict[str, Any]) -> T:
    def invalid_constant(value: str) -> None:
        raise ValueError("Non-JSON numeric constant")

    try:
        parsed = json.loads(text, parse_constant=invalid_constant)
        Draft202012Validator(schema).validate(parsed)
        return model.model_validate_json(text, strict=True, extra="forbid")
    except (ValueError, JSONSchemaValidationError, ValidationError):
        raise StructuredValidationError("invalid_structured_output") from None
