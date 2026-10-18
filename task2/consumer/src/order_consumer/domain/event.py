"""The order placed event: a CloudEvent in structured mode carrying the order (ADR 0001)."""

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry, Resource

from order_consumer.domain.rules import business_rule_errors
from order_consumer.domain.validation import FieldError, InvalidEvent, Layer, cut


@dataclass(frozen=True, slots=True)
class OrderPlacedEvent:
    event_id: UUID
    data: Mapping[str, Any]


def _ecma_pattern(
    validator: Any, pattern: str, instance: Any, _schema: Any
) -> Iterator[ValidationError]:
    """JSON Schema patterns are ECMA-262 regular expressions, where `$` is the end of the input.
    Python's `$` also matches before a final newline, which would let "SKU\\n" through."""
    if validator.is_type(instance, "string") and not _compiled(pattern).search(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    """`$` outside a character class, unescaped, becomes Python's end of input, `\\Z`."""
    translated, escaped, in_class = [], False, False
    for char in pattern:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "$" and not in_class:
            translated.append(r"\Z")
            continue
        translated.append(char)
    return re.compile("".join(translated))


# The stubs leave `extend` untyped; it returns a Draft 2020-12 validator class.
_Validator: type[Draft202012Validator] = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator, {"pattern": _ecma_pattern}
)


def _reject_constant(constant: str) -> None:
    raise ValueError(f"{constant} is not valid JSON")


class OrderPlacedReader:
    """Reads order placed events, checking the layers of ADR 0001 in order against the event's
    JSON Schema. Each layer is one part of the schema; checking stops at the first that fails."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        Draft202012Validator.check_schema(schema)
        registry: Registry[Any] = Registry().with_resource(
            schema["$id"], Resource.from_contents(schema)
        )

        def part(name: str) -> Draft202012Validator:
            return _Validator(
                {"$ref": f"{schema['$id']}#/$defs/{name}"},
                registry=registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )

        self._layers = (
            (Layer.NOT_A_CLOUDEVENT, part("cloudEvent")),
            (Layer.UNSUPPORTED_TYPE_OR_VERSION, part("supportedTypeAndVersion")),
            (Layer.SCHEMA_VIOLATION, part("orderPlaced")),
        )

    @classmethod
    def from_file(cls, path: Path) -> "OrderPlacedReader":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def read(self, key: bytes | None, value: bytes) -> OrderPlacedEvent:
        envelope = _parse(value)
        layer, errors = self._first_failing_layer(key, envelope)
        if errors:
            data = envelope.get("data") if isinstance(envelope, dict) else None
            raise InvalidEvent(
                layer,
                errors,
                event_id=_readable_uuid(envelope, "id"),
                order_id=_readable_uuid(data, "orderId"),
            )
        return OrderPlacedEvent(event_id=UUID(envelope["id"]), data=envelope["data"])

    def _first_failing_layer(
        self, key: bytes | None, envelope: Any
    ) -> tuple[Layer, list[FieldError]]:
        for layer, validator in self._layers:
            errors = [_field_error(error) for error in validator.iter_errors(envelope)]
            if errors:
                return layer, errors
        return Layer.BUSINESS_RULE, business_rule_errors(key, envelope["data"])


def _readable_uuid(container: Any, name: str) -> UUID | None:
    """A UUID in its canonical form, or None: nothing else is trusted into a dead letter's
    headers."""
    value = container.get(name) if isinstance(container, dict) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def _field_error(error: ValidationError) -> FieldError:
    """jsonschema's message quotes the offending value; it is cut to 64 characters (ADR 0008)."""
    quoted = repr(error.instance)
    message = error.message.replace(quoted, cut(quoted))
    return FieldError(pointer=_pointer(error.absolute_path), message=message)


def _pointer(path: Iterable[str | int]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _parse(value: bytes) -> Any:
    """Layer 1. Decoded explicitly: `json.loads` on bytes would also accept UTF-16 and UTF-32."""
    try:
        return json.loads(value.decode("utf-8"), parse_constant=_reject_constant)
    except UnicodeDecodeError as error:
        message = f"not UTF-8: byte {error.start} can't be decoded"
    except ValueError as error:
        message = f"not JSON: {error}"
    raise InvalidEvent(Layer.UNREADABLE, [FieldError(pointer="", message=message)])
