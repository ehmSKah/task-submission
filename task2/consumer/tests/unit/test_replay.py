"""Which dead letters a replay republishes, and with which headers (ADR 0006)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from order_consumer.application.ports import DeadLetterReason
from order_consumer.application.replay import DeadLetterRecord, Held, Replay, Selection, decide

ORDER_ID = UUID("7d2e1f3a-0000-4000-8000-000000000001")
WRITTEN_AT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def dead_letter(
    reason: str = "retries-exhausted",
    detail: str = '{"status": 503, "error": "503 Service Unavailable"}',
    order_id: UUID | None = ORDER_ID,
    original: tuple[tuple[str, bytes | None], ...] = (),
    timestamp: datetime | None = WRITTEN_AT,
) -> DeadLetterRecord:
    failure: list[tuple[str, bytes | None]] = [
        ("dlt.reason", reason.encode()),
        ("dlt.detail", detail.encode()),
        ("dlt.attempts", b"5"),
        ("dlt.source", b"orders/2/1187"),
    ]
    if order_id is not None:
        failure.append(("dlt.order-id", str(order_id).encode()))
    return DeadLetterRecord(
        topic="orders.dlt",
        partition=1,
        offset=42,
        key=None if order_id is None else str(order_id).encode(),
        value=b"{}",
        headers=(*original, *failure),
        timestamp=timestamp,
    )


def test_a_replay_keeps_the_original_headers_and_names_the_dead_letter_instead_of_the_failure() -> (
    None
):
    traceparent = b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    original = (("traceparent", traceparent), ("tracestate", b"vendor=1"), ("empty", None))

    decision = decide(dead_letter(original=original), Selection())

    assert decision == Replay(
        headers=(
            ("traceparent", traceparent),
            ("tracestate", b"vendor=1"),
            ("empty", None),
            ("replayed-from", b"orders.dlt/1/42"),
        )
    )


def test_a_dead_letter_of_an_earlier_replay_names_only_the_dead_letter_it_is_replayed_from() -> (
    None
):
    original = (("Replayed-From", b"orders.dlt/0/7"), ("traceparent", b"00-ab-cd-01"))

    decision = decide(dead_letter(original=original), Selection())

    assert decision == Replay(
        headers=(("traceparent", b"00-ab-cd-01"), ("replayed-from", b"orders.dlt/1/42"))
    )


def invalid(layer: str) -> DeadLetterRecord:
    return dead_letter("invalid", f'{{"layer":"{layer}","errorCount":1,"errors":[]}}')


@pytest.mark.parametrize(
    "replayable",
    [
        dead_letter("retries-exhausted"),
        dead_letter("rejected", '{"status": 422, "error": "422 Unprocessable Content"}'),
        dead_letter("unexpected", '{"type": "KeyError", "message": "\'lines\'"}'),
        invalid("unsupported-type-or-version"),
    ],
    ids=["retries-exhausted", "rejected", "unexpected", "unsupported-type-or-version"],
)
def test_dead_letters_a_fix_on_our_side_can_resolve_are_replayed(
    replayable: DeadLetterRecord,
) -> None:
    assert isinstance(decide(replayable, Selection()), Replay)


@pytest.mark.parametrize(
    "unchanged_fails_again",
    [
        invalid("unreadable"),
        invalid("not-a-cloudevent"),
        invalid("schema-violation"),
        invalid("business-rule"),
        dead_letter("invalid", "not json"),
        dead_letter("conflict", '{"acceptedEventId": "7d2e1f3a", "fields": ["/data/total"]}'),
    ],
    ids=["unreadable", "not-a-cloudevent", "schema", "business-rule", "no-layer", "conflict"],
)
def test_dead_letters_only_a_corrected_event_resolves_are_held_unless_forced(
    unchanged_fails_again: DeadLetterRecord,
) -> None:
    held = decide(unchanged_fails_again, Selection())
    forced = decide(unchanged_fails_again, Selection(force=True))

    assert held == Held("unchanged bytes fail again; --force replays it")
    assert isinstance(forced, Replay)


@pytest.mark.parametrize("reason", ["", "retried-too-often"])
def test_a_record_without_a_known_reason_is_held_even_when_forced(reason: str) -> None:
    decision = decide(dead_letter(reason), Selection(force=True))

    assert decision == Held("no known dlt.reason: not a dead letter of this consumer")


def test_a_reason_filter_leaves_out_dead_letters_of_other_reasons() -> None:
    selection = Selection(reasons=frozenset({DeadLetterReason.RETRIES_EXHAUSTED}))

    assert isinstance(decide(dead_letter("retries-exhausted"), selection), Replay)
    assert decide(dead_letter("rejected"), selection) is None
    assert decide(dead_letter(""), selection) is None


def test_an_order_filter_leaves_out_dead_letters_of_other_orders() -> None:
    selection = Selection(order_ids=frozenset({ORDER_ID}))

    assert isinstance(decide(dead_letter(order_id=ORDER_ID), selection), Replay)
    assert decide(dead_letter(order_id=uuid4()), selection) is None
    assert decide(dead_letter(order_id=None), selection) is None


def test_an_order_filter_matches_the_key_when_the_order_id_could_not_be_read() -> None:
    unreadable = dead_letter("invalid", '{"layer":"unreadable"}', order_id=None)
    keyed = dataclasses.replace(unreadable, key=str(ORDER_ID).upper().encode())

    decision = decide(keyed, Selection(order_ids=frozenset({ORDER_ID})))

    assert isinstance(decision, Held), "selected, and held as an invalid event"


def test_a_time_filter_selects_dead_letters_written_from_since_up_to_until() -> None:
    selection = Selection(since=WRITTEN_AT, until=WRITTEN_AT + timedelta(hours=1))

    assert isinstance(decide(dead_letter(timestamp=WRITTEN_AT), selection), Replay)
    assert decide(dead_letter(timestamp=WRITTEN_AT - timedelta(milliseconds=1)), selection) is None
    assert decide(dead_letter(timestamp=WRITTEN_AT + timedelta(hours=1)), selection) is None
    assert decide(dead_letter(timestamp=None), selection) is None
