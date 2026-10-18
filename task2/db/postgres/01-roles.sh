#!/bin/sh
# Two roles (ADR 0002): the migration owner, which owns the schema, and the consumer, which may
# read and write data but never change the schema. Runs once, when the data volume is empty.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v owner_password="$ORDERS_OWNER_PASSWORD" \
    -v consumer_password="$ORDER_CONSUMER_PASSWORD" <<'SQL'
CREATE ROLE orders_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE order_consumer LOGIN PASSWORD :'consumer_password';
CREATE DATABASE orders OWNER orders_owner;
REVOKE ALL ON DATABASE orders FROM PUBLIC;
GRANT CONNECT ON DATABASE orders TO order_consumer;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname orders <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO order_consumer;
SQL
