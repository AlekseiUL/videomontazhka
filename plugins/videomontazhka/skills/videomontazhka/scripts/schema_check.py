#!/usr/bin/env python3
"""Validate a JSON instance against the schema subset used by SPRUT.

This module deliberately depends on the Python standard library only.  It is
not a complete JSON Schema implementation; it implements the Draft 2020-12
keywords used by ``semantic-plan.schema.json`` and ``edl.schema.json`` plus a
few closely related constraints that make diagnostics useful.

Exit status:
    0  the instance is valid (or the self-test passed)
    1  the instance does not satisfy the schema
    2  the command, JSON document, or schema is invalid
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse


JsonPath = tuple[str | int, ...]


class SchemaDefinitionError(ValueError):
    """Raised when a supported schema keyword is malformed."""


@dataclass(frozen=True)
class ValidationError:
    """One instance validation failure with instance and schema locations."""

    instance_path: JsonPath
    schema_path: JsonPath
    message: str

    def render(self) -> str:
        return (
            f"{format_instance_path(self.instance_path)}: {self.message} "
            f"(schema {format_schema_path(self.schema_path)})"
        )


def format_instance_path(path: JsonPath) -> str:
    """Render a path in a readable JSONPath-like form."""

    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            result += f".{part}"
        else:
            result += f"[{json.dumps(part, ensure_ascii=False)}]"
    return result


def _pointer_escape(part: str | int) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def format_schema_path(path: JsonPath) -> str:
    if not path:
        return "#"
    return "#/" + "/".join(_pointer_escape(part) for part in path)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _json_equal(left: Any, right: Any) -> bool:
    """Compare with JSON Schema equality (booleans are not numbers)."""

    if _is_number(left) and _is_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right


def _type_matches(value: Any, expected: str) -> bool:
    checks = {
        "null": lambda item: item is None,
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "number": _is_number,
        "integer": _is_integer,
        "string": lambda item: isinstance(item, str),
    }
    if expected not in checks:
        raise SchemaDefinitionError(f"unsupported JSON Schema type: {expected!r}")
    return bool(checks[expected](value))


def _type_label(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if _is_integer(value):
        return "integer"
    if _is_number(value):
        return "number"
    return type(value).__name__


class Validator:
    """Small Draft 2020-12 validator for SPRUT's checked-in schemas."""

    def __init__(self, root_schema: dict[str, Any] | bool):
        if not isinstance(root_schema, (dict, bool)):
            raise SchemaDefinitionError("schema root must be an object or boolean")
        self.root_schema = root_schema
        self._active_refs: set[tuple[str, int]] = set()

    def validate(self, instance: Any) -> list[ValidationError]:
        self._active_refs.clear()
        return self._validate(self.root_schema, instance, (), ())

    def _error(
        self,
        instance_path: JsonPath,
        schema_path: JsonPath,
        message: str,
    ) -> list[ValidationError]:
        return [ValidationError(instance_path, schema_path, message)]

    def _resolve_ref(self, ref: str) -> tuple[dict[str, Any] | bool, JsonPath]:
        if not isinstance(ref, str):
            raise SchemaDefinitionError("$ref must be a string")
        if ref == "#":
            return self.root_schema, ()
        if not ref.startswith("#/"):
            raise SchemaDefinitionError(
                f"only local JSON Pointer references are supported, got {ref!r}"
            )
        raw_parts = unquote(ref[2:]).split("/")
        parts: list[str] = [part.replace("~1", "/").replace("~0", "~") for part in raw_parts]
        current: Any = self.root_schema
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                raise SchemaDefinitionError(f"unresolvable $ref: {ref!r}")
            current = current[part]
        if not isinstance(current, (dict, bool)):
            raise SchemaDefinitionError(f"$ref target is not a schema: {ref!r}")
        return current, tuple(parts)

    def _validate(
        self,
        schema: dict[str, Any] | bool,
        instance: Any,
        instance_path: JsonPath,
        schema_path: JsonPath,
    ) -> list[ValidationError]:
        if schema is True:
            return []
        if schema is False:
            return self._error(instance_path, schema_path, "value is forbidden")
        if not isinstance(schema, dict):
            raise SchemaDefinitionError(
                f"schema at {format_schema_path(schema_path)} must be an object or boolean"
            )

        errors: list[ValidationError] = []

        if "$ref" in schema:
            ref = schema["$ref"]
            target, target_path = self._resolve_ref(ref)
            ref_key = (ref, id(instance))
            if ref_key in self._active_refs:
                raise SchemaDefinitionError(f"cyclic $ref without instance progress: {ref!r}")
            self._active_refs.add(ref_key)
            try:
                errors.extend(self._validate(target, instance, instance_path, target_path))
            finally:
                self._active_refs.remove(ref_key)

        errors.extend(self._validate_combiners(schema, instance, instance_path, schema_path))

        expected_types = schema.get("type")
        if expected_types is not None:
            if isinstance(expected_types, str):
                types = [expected_types]
            elif isinstance(expected_types, list) and expected_types and all(
                isinstance(item, str) for item in expected_types
            ):
                types = expected_types
            else:
                raise SchemaDefinitionError(
                    f"type at {format_schema_path(schema_path + ('type',))} must be a string "
                    "or non-empty string array"
                )
            if not any(_type_matches(instance, expected) for expected in types):
                expected = " or ".join(types)
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + ("type",),
                        f"expected {expected}, got {_type_label(instance)}",
                    )
                )
                return errors

        if "const" in schema and not _json_equal(instance, schema["const"]):
            errors.extend(
                self._error(
                    instance_path,
                    schema_path + ("const",),
                    f"must equal {json.dumps(schema['const'], ensure_ascii=False)}",
                )
            )

        if "enum" in schema:
            options = schema["enum"]
            if not isinstance(options, list) or not options:
                raise SchemaDefinitionError("enum must be a non-empty array")
            if not any(_json_equal(instance, option) for option in options):
                rendered = ", ".join(json.dumps(option, ensure_ascii=False) for option in options)
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + ("enum",),
                        f"must be one of: {rendered}",
                    )
                )

        if isinstance(instance, dict):
            errors.extend(self._validate_object(schema, instance, instance_path, schema_path))
        if isinstance(instance, list):
            errors.extend(self._validate_array(schema, instance, instance_path, schema_path))
        if isinstance(instance, str):
            errors.extend(self._validate_string(schema, instance, instance_path, schema_path))
        if _is_number(instance):
            errors.extend(self._validate_number(schema, instance, instance_path, schema_path))
        return errors

    def _validate_combiners(
        self,
        schema: dict[str, Any],
        instance: Any,
        instance_path: JsonPath,
        schema_path: JsonPath,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []

        all_of = schema.get("allOf")
        if all_of is not None:
            self._require_schema_array(all_of, schema_path + ("allOf",), allow_empty=True)
            for index, branch in enumerate(all_of):
                errors.extend(
                    self._validate(branch, instance, instance_path, schema_path + ("allOf", index))
                )

        for keyword, exact_one in (("anyOf", False), ("oneOf", True)):
            branches = schema.get(keyword)
            if branches is None:
                continue
            self._require_schema_array(branches, schema_path + (keyword,), allow_empty=False)
            results = [
                self._validate(branch, instance, instance_path, schema_path + (keyword, index))
                for index, branch in enumerate(branches)
            ]
            matches = [index for index, branch_errors in enumerate(results) if not branch_errors]
            is_valid = len(matches) == 1 if exact_one else bool(matches)
            if is_valid:
                continue
            expectation = "exactly one option" if exact_one else "at least one option"
            errors.extend(
                self._error(
                    instance_path,
                    schema_path + (keyword,),
                    f"must match {expectation}; matched {len(matches)}",
                )
            )
            if not matches:
                for index, branch_errors in enumerate(results):
                    if branch_errors:
                        first = branch_errors[0]
                        errors.append(
                            ValidationError(
                                first.instance_path,
                                first.schema_path,
                                f"{keyword} option {index + 1}: {first.message}",
                            )
                        )

        if "not" in schema:
            forbidden = schema["not"]
            self._require_schema(forbidden, schema_path + ("not",))
            if not self._validate(forbidden, instance, instance_path, schema_path + ("not",)):
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + ("not",),
                        "must not match the disallowed schema",
                    )
                )

        if "if" in schema:
            condition = schema["if"]
            self._require_schema(condition, schema_path + ("if",))
            condition_matches = not self._validate(
                condition, instance, instance_path, schema_path + ("if",)
            )
            branch_name = "then" if condition_matches else "else"
            if branch_name in schema:
                branch = schema[branch_name]
                self._require_schema(branch, schema_path + (branch_name,))
                errors.extend(
                    self._validate(
                        branch,
                        instance,
                        instance_path,
                        schema_path + (branch_name,),
                    )
                )
        return errors

    @staticmethod
    def _require_schema(value: Any, path: JsonPath) -> None:
        if not isinstance(value, (dict, bool)):
            raise SchemaDefinitionError(
                f"{format_schema_path(path)} must contain a schema object or boolean"
            )

    def _require_schema_array(self, value: Any, path: JsonPath, *, allow_empty: bool) -> None:
        if not isinstance(value, list) or (not allow_empty and not value):
            suffix = "" if allow_empty else " non-empty"
            raise SchemaDefinitionError(f"{format_schema_path(path)} must be a{suffix} array")
        for index, branch in enumerate(value):
            self._require_schema(branch, path + (index,))

    def _validate_object(
        self,
        schema: dict[str, Any],
        instance: dict[str, Any],
        instance_path: JsonPath,
        schema_path: JsonPath,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        length = len(instance)
        errors.extend(
            self._check_count(
                schema,
                length,
                instance_path,
                schema_path,
                "minProperties",
                "maxProperties",
                "properties",
            )
        )

        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise SchemaDefinitionError("required must be an array of strings")
        for name in required:
            if name not in instance:
                errors.extend(
                    self._error(
                        instance_path + (name,),
                        schema_path + ("required",),
                        "required property is missing",
                    )
                )

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaDefinitionError("properties must be an object")
        for name, child_schema in properties.items():
            self._require_schema(child_schema, schema_path + ("properties", name))
            if name in instance:
                errors.extend(
                    self._validate(
                        child_schema,
                        instance[name],
                        instance_path + (name,),
                        schema_path + ("properties", name),
                    )
                )

        if "propertyNames" in schema:
            names_schema = schema["propertyNames"]
            self._require_schema(names_schema, schema_path + ("propertyNames",))
            for name in instance:
                name_errors = self._validate(
                    names_schema,
                    name,
                    instance_path + (name,),
                    schema_path + ("propertyNames",),
                )
                errors.extend(
                    ValidationError(error.instance_path, error.schema_path, f"property name {error.message}")
                    for error in name_errors
                )

        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            self._require_schema(additional, schema_path + ("additionalProperties",))
            for name in instance.keys() - properties.keys():
                if additional is False:
                    errors.extend(
                        self._error(
                            instance_path + (name,),
                            schema_path + ("additionalProperties",),
                            "additional property is not allowed",
                        )
                    )
                elif additional is not True:
                    errors.extend(
                        self._validate(
                            additional,
                            instance[name],
                            instance_path + (name,),
                            schema_path + ("additionalProperties",),
                        )
                    )
        return errors

    def _validate_array(
        self,
        schema: dict[str, Any],
        instance: list[Any],
        instance_path: JsonPath,
        schema_path: JsonPath,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        errors.extend(
            self._check_count(
                schema,
                len(instance),
                instance_path,
                schema_path,
                "minItems",
                "maxItems",
                "items",
            )
        )

        if schema.get("uniqueItems") is True:
            for right in range(len(instance)):
                for left in range(right):
                    if _json_equal(instance[left], instance[right]):
                        errors.extend(
                            self._error(
                                instance_path + (right,),
                                schema_path + ("uniqueItems",),
                                f"duplicates item at index {left}",
                            )
                        )
                        break
        elif "uniqueItems" in schema and schema["uniqueItems"] is not False:
            raise SchemaDefinitionError("uniqueItems must be boolean")

        prefix_items = schema.get("prefixItems", [])
        if not isinstance(prefix_items, list):
            raise SchemaDefinitionError("prefixItems must be an array")
        for index, child_schema in enumerate(prefix_items[: len(instance)]):
            self._require_schema(child_schema, schema_path + ("prefixItems", index))
            errors.extend(
                self._validate(
                    child_schema,
                    instance[index],
                    instance_path + (index,),
                    schema_path + ("prefixItems", index),
                )
            )

        if "items" in schema:
            items_schema = schema["items"]
            self._require_schema(items_schema, schema_path + ("items",))
            start = len(prefix_items) if "prefixItems" in schema else 0
            for index in range(start, len(instance)):
                errors.extend(
                    self._validate(
                        items_schema,
                        instance[index],
                        instance_path + (index,),
                        schema_path + ("items",),
                    )
                )
        return errors

    def _validate_string(
        self,
        schema: dict[str, Any],
        instance: str,
        instance_path: JsonPath,
        schema_path: JsonPath,
    ) -> list[ValidationError]:
        errors = self._check_count(
            schema,
            len(instance),
            instance_path,
            schema_path,
            "minLength",
            "maxLength",
            "characters",
        )
        if "pattern" in schema:
            pattern = schema["pattern"]
            if not isinstance(pattern, str):
                raise SchemaDefinitionError("pattern must be a string")
            try:
                matches = re.search(pattern, instance) is not None
            except re.error as exc:
                raise SchemaDefinitionError(f"invalid regular expression {pattern!r}: {exc}") from exc
            if not matches:
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + ("pattern",),
                        f"must match pattern {pattern!r}",
                    )
                )

        if "format" in schema:
            format_name = schema["format"]
            if not isinstance(format_name, str):
                raise SchemaDefinitionError("format must be a string")
            if format_name == "uri" and not self._is_uri(instance):
                errors.extend(
                    self._error(instance_path, schema_path + ("format",), "must be a valid URI")
                )
            elif format_name == "date-time" and not self._is_datetime(instance):
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + ("format",),
                        "must be an RFC 3339 date-time",
                    )
                )
        return errors

    @staticmethod
    def _is_uri(value: str) -> bool:
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        return bool(parsed.scheme and not any(character.isspace() for character in value))

    @staticmethod
    def _is_datetime(value: str) -> bool:
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return False
        return "T" in value and parsed.tzinfo is not None

    def _validate_number(
        self,
        schema: dict[str, Any],
        instance: int | float,
        instance_path: JsonPath,
        schema_path: JsonPath,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        comparisons = (
            ("minimum", lambda actual, limit: actual >= limit, ">="),
            ("maximum", lambda actual, limit: actual <= limit, "<="),
            ("exclusiveMinimum", lambda actual, limit: actual > limit, ">"),
            ("exclusiveMaximum", lambda actual, limit: actual < limit, "<"),
        )
        for keyword, check, operator in comparisons:
            if keyword not in schema:
                continue
            limit = schema[keyword]
            if not _is_number(limit):
                raise SchemaDefinitionError(f"{keyword} must be a finite number")
            if not check(instance, limit):
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + (keyword,),
                        f"must be {operator} {limit}",
                    )
                )

        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if not _is_number(divisor) or divisor <= 0:
                raise SchemaDefinitionError("multipleOf must be a positive finite number")
            try:
                quotient = Decimal(str(instance)) / Decimal(str(divisor))
                is_multiple = quotient == quotient.to_integral_value()
            except (InvalidOperation, ZeroDivisionError) as exc:
                raise SchemaDefinitionError(f"cannot evaluate multipleOf: {exc}") from exc
            if not is_multiple:
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + ("multipleOf",),
                        f"must be a multiple of {divisor}",
                    )
                )
        return errors

    def _check_count(
        self,
        schema: dict[str, Any],
        actual: int,
        instance_path: JsonPath,
        schema_path: JsonPath,
        minimum_keyword: str,
        maximum_keyword: str,
        noun: str,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if minimum_keyword in schema:
            minimum = schema[minimum_keyword]
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
                raise SchemaDefinitionError(f"{minimum_keyword} must be a non-negative integer")
            if actual < minimum:
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + (minimum_keyword,),
                        f"must contain at least {minimum} {noun}; got {actual}",
                    )
                )
        if maximum_keyword in schema:
            maximum = schema[maximum_keyword]
            if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
                raise SchemaDefinitionError(f"{maximum_keyword} must be a non-negative integer")
            if actual > maximum:
                errors.extend(
                    self._error(
                        instance_path,
                        schema_path + (maximum_keyword,),
                        f"must contain at most {maximum} {noun}; got {actual}",
                    )
                )
        return errors


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"non-JSON numeric constant {token!r}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonfinite,
        )
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def run_self_test() -> int:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["version", "name", "choice", "tuple", "unique", "enabled"],
        "additionalProperties": False,
        "propertyNames": {"pattern": "^[a-z_]+$"},
        "properties": {
            "version": {"const": 1},
            "name": {"$ref": "#/$defs/name"},
            "choice": {
                "oneOf": [
                    {"type": "integer", "minimum": 2, "maximum": 10, "multipleOf": 2},
                    {"type": "string", "enum": ["auto", "manual"]},
                ]
            },
            "tuple": {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "number", "exclusiveMinimum": 0}],
                "items": False,
                "minItems": 2,
                "maxItems": 2,
            },
            "unique": {"type": "array", "uniqueItems": True, "items": {"type": "number"}},
            "enabled": {"type": "boolean"},
            "detail": {"type": "string", "minLength": 3},
        },
        "allOf": [
            {
                "if": {"properties": {"enabled": {"const": True}}, "required": ["enabled"]},
                "then": {"required": ["detail"]},
            },
            {"not": {"required": ["forbidden"]}},
            {"anyOf": [{"required": ["name"]}, {"required": ["detail"]}]},
        ],
        "$defs": {
            "name": {"type": "string", "minLength": 2, "maxLength": 8, "pattern": "^[A-Z]"}
        },
    }
    validator = Validator(schema)
    assertions = 0

    valid = {
        "version": 1,
        "name": "Sprut",
        "choice": 4,
        "tuple": ["x", 0.5],
        "unique": [1, 2.0],
        "enabled": True,
        "detail": "yes",
    }
    assert validator.validate(valid) == []
    assertions += 1

    invalid = {
        "version": 2,
        "name": "sprut-too-long",
        "choice": 3,
        "tuple": ["x", 0, "extra"],
        "unique": [1, 1.0],
        "enabled": True,
        "Bad-Key": 1,
    }
    rendered = "\n".join(error.render() for error in validator.validate(invalid))
    expected_paths = (
        "$.version",
        "$.name",
        "$.choice",
        "$.tuple[1]",
        "$.tuple[2]",
        "$.unique[1]",
        "$.detail",
        '$["Bad-Key"]',
    )
    for path in expected_paths:
        assert path in rendered, f"missing expected error path {path}:\n{rendered}"
        assertions += 1

    overlap_schema = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
    overlap_errors = Validator(overlap_schema).validate(2)
    assert overlap_errors and "matched 2" in overlap_errors[0].message
    assertions += 1

    conditional_schema = {
        "if": {"const": "a"},
        "then": {"const": "a"},
        "else": {"const": "b"},
    }
    assert Validator(conditional_schema).validate("b") == []
    assertions += 1

    print(f"SELF-TEST PASS ({assertions} assertions)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate semantic_plan.json or edl.json using the local, standard-library-only "
            "SPRUT JSON Schema checker."
        )
    )
    parser.add_argument("--schema", type=Path, help="path to a Draft 2020-12 JSON schema")
    parser.add_argument("--instance", type=Path, help="path to the JSON document to validate")
    parser.add_argument(
        "--max-errors",
        type=int,
        default=100,
        help="maximum diagnostics to print (default: 100)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run built-in keyword and diagnostic smoke tests",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        if args.schema is not None or args.instance is not None:
            parser.error("--self-test cannot be combined with --schema or --instance")
        return run_self_test()
    if args.schema is None or args.instance is None:
        parser.error("--schema and --instance are required unless --self-test is used")
    if args.max_errors <= 0:
        parser.error("--max-errors must be positive")

    try:
        schema = load_json(args.schema.expanduser().resolve())
        instance = load_json(args.instance.expanduser().resolve())
        errors = Validator(schema).validate(instance)
    except (ValueError, SchemaDefinitionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not errors:
        print(f"PASS: {args.instance} satisfies {args.schema}")
        return 0

    print(f"FAIL: {len(errors)} schema violation(s) in {args.instance}", file=sys.stderr)
    for error in errors[: args.max_errors]:
        print(f"- {error.render()}", file=sys.stderr)
    if len(errors) > args.max_errors:
        print(f"- ... {len(errors) - args.max_errors} more violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
