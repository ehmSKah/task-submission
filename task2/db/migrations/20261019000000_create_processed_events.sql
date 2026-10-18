-- migrate:up
-- Every event id that brought or repeated an order, with the outcome and where it was read
-- (ADR 0002). Conflicting events get no row: the dead-letter topic is their record.
CREATE TABLE processed_events (
    event_id    uuid        PRIMARY KEY,
    order_id    uuid        NOT NULL REFERENCES orders (order_id),
    outcome     text        NOT NULL CHECK (outcome IN ('accepted', 'duplicate')),
    received_at timestamptz NOT NULL DEFAULT now(),
    topic       text        NOT NULL,
    partition   integer     NOT NULL,
    "offset"    bigint      NOT NULL
);

-- "Which events arrived for this order?"
CREATE INDEX processed_events_order_id ON processed_events (order_id);

-- Rows are only ever added.
GRANT SELECT, INSERT ON processed_events TO order_consumer;

-- migrate:down
DROP TABLE processed_events;
