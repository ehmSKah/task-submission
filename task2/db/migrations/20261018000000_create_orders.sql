-- migrate:up
-- Orders and their lines (ADR 0002), with the handover state of ADR 0004.
CREATE TABLE orders (
    order_id            uuid PRIMARY KEY,
    customer_id         uuid        NOT NULL,
    placed_at           timestamptz NOT NULL,
    currency            char(3)     NOT NULL,
    total               bigint      NOT NULL CHECK (total >= 0),
    item_count          integer     NOT NULL CHECK (item_count > 0),
    accepted_at         timestamptz NOT NULL DEFAULT now(),
    first_event_id      uuid        NOT NULL,
    handover_status     text        NOT NULL
        CHECK (handover_status IN ('awaiting_handover', 'handed_over', 'handover_failed')),
    fulfilment_order_id text,
    handed_over_at      timestamptz,
    CHECK ((handover_status = 'handed_over') = (fulfilment_order_id IS NOT NULL))
);

CREATE TABLE order_lines (
    order_id   uuid    NOT NULL REFERENCES orders (order_id),
    sku        text    NOT NULL,
    quantity   integer NOT NULL CHECK (quantity > 0),
    unit_price bigint  NOT NULL CHECK (unit_price >= 0),
    line_total bigint  NOT NULL CHECK (line_total = quantity * unit_price),
    PRIMARY KEY (order_id, sku)
);

-- The consumer reads and writes data only; it can't change the schema.
GRANT SELECT, INSERT, UPDATE ON orders, order_lines TO order_consumer;

-- migrate:down
DROP TABLE order_lines;
DROP TABLE orders;
