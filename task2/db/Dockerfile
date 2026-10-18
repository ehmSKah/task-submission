# The one-shot migration step (ADR 0002): dbmate with the migrations baked in.
FROM ghcr.io/amacneil/dbmate:2.36.0
COPY migrations /db/migrations
ENTRYPOINT ["dbmate", "--migrations-dir", "/db/migrations", "--no-dump-schema", "--wait"]
CMD ["up"]
