"""The correlation id is the trace id of the record's `traceparent` header (ADR 0008): continued
when valid, a new trace when missing, replaced when invalid."""

import pytest
from opentelemetry import trace

from order_consumer.application.ports import TraceSource
from order_consumer.tracing import incoming_trace

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


def parent_ids(headers: tuple[tuple[str, bytes | None], ...]) -> tuple[str, str] | None:
    context, _source = incoming_trace(headers)
    parent = trace.get_current_span(context).get_span_context()
    if not parent.is_valid:
        return None
    return format(parent.trace_id, "032x"), format(parent.span_id, "016x")


def source_of(headers: tuple[tuple[str, bytes | None], ...]) -> TraceSource:
    return incoming_trace(headers)[1]


def test_a_valid_traceparent_is_continued() -> None:
    headers = (("traceparent", TRACEPARENT.encode()),)

    assert source_of(headers) is TraceSource.HEADER
    assert parent_ids(headers) == (TRACE_ID, SPAN_ID)


def test_a_future_version_with_more_fields_is_continued() -> None:
    headers = (("traceparent", f"01-{TRACE_ID}-{SPAN_ID}-01-more".encode()),)

    assert source_of(headers) is TraceSource.HEADER
    assert parent_ids(headers) == (TRACE_ID, SPAN_ID)


def test_without_a_traceparent_a_new_trace_is_started() -> None:
    headers = (("other", b"value"),)

    assert source_of(headers) is TraceSource.NEW
    assert parent_ids(headers) is None


@pytest.mark.parametrize(
    "value",
    [
        b"not a traceparent",
        f"00-{TRACE_ID.upper()}-{SPAN_ID}-01".encode(),
        f"00-{'0' * 32}-{SPAN_ID}-01".encode(),
        f"00-{TRACE_ID}-{'0' * 16}-01".encode(),
        f"ff-{TRACE_ID}-{SPAN_ID}-01".encode(),
        f"00-{TRACE_ID}-{SPAN_ID}-01-extra".encode(),
        f"01-{TRACE_ID}-{SPAN_ID}-01-{'x' * 80}".encode(),  # longer than 128 characters
        f'00-{TRACE_ID}-{SPAN_ID}-01\n{{"event":"forged"}}'.encode(),
        b"\xff\xfe",
        b"",
        None,
    ],
    ids=[
        "garbage",
        "upper case",
        "zero trace id",
        "zero span id",
        "version ff",
        "version 00 with more fields",
        "too long",
        "a forged log line",
        "not UTF-8",
        "empty",
        "no value",
    ],
)
def test_an_invalid_traceparent_is_replaced_by_a_new_trace(value: bytes | None) -> None:
    headers = (("traceparent", value),)

    assert source_of(headers) is TraceSource.REPLACED
    assert parent_ids(headers) is None


def test_a_repeated_traceparent_is_replaced() -> None:
    headers = (("traceparent", TRACEPARENT.encode()), ("traceparent", TRACEPARENT.encode()))

    assert source_of(headers) is TraceSource.REPLACED
    assert parent_ids(headers) is None
