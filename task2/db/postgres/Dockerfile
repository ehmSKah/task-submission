# PostgreSQL with the role setup baked in, so the stack also runs against a remote Docker daemon,
# which can't bind-mount files from the client.
FROM postgres:18.6-alpine
COPY --chmod=0755 01-roles.sh /docker-entrypoint-initdb.d/
