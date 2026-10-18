-- migrate:up
-- The trace id each order and event was processed in: the correlation id that finds the order's
-- trace and log lines (ADR 0002, 0008). Empty for rows written before traces were recorded.
ALTER TABLE orders
    ADD COLUMN trace_id text CHECK (trace_id ~ '^[0-9a-f]{32}$');

ALTER TABLE processed_events
    ADD COLUMN trace_id text CHECK (trace_id ~ '^[0-9a-f]{32}$');

-- migrate:down
ALTER TABLE processed_events DROP COLUMN trace_id;
ALTER TABLE orders DROP COLUMN trace_id;
