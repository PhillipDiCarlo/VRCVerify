#!/usr/bin/env bash
#
# Run the test suite against a disposable local Postgres seeded from
# schema/production.sql.
#
# WHY. The default suite runs on SQLite, which cannot reproduce a disagreement
# about a column's type: it gives VARCHAR columns TEXT affinity, so an integer
# written into one comes back as a string. #164 shipped through that gap with
# two regression tests passing over it.
#
# CI RUNS THIS MODE ON EVERY PULL REQUEST NOW (#297), as the `postgres` job in
# .github/workflows/test.yml, so it no longer depends on somebody remembering
# before a release. This script is still the way to run it HERE: to see a
# failure without pushing, to pass -k or a path through to pytest, and to
# reproduce something against a restored copy of real data, which is a thing a
# runner cannot do and must not.
#
# SEEDED FROM THE DUMP, NOT FROM create_all(). A Postgres database built by
# create_all() has the columns the MODELS describe, which is the half of the
# disagreement that was never in question -- it would not reproduce #164 either.
# The container is loaded from the committed snapshot of the real schema, and
# bot.py's create_all() at import then finds every table already present and
# leaves the deployed types alone.
#
# THE DATABASE IS THROWAWAY AND LOCAL, and both halves matter: the suite's
# teardown deletes rows. conftest.local_database_only refuses any URL that is
# not loopback, so this cannot be repointed at production by editing a variable.
# To reproduce something against real data, restore a copy locally and point
# this at the copy. Never at production.
#
#   ./scripts/test_postgres.sh                 # whole suite
#   ./scripts/test_postgres.sh tests/test_premium.py -k grandfather
#
# Anything after the script name is passed through to pytest.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT="$REPO_ROOT/schema/production.sql"
PG_IMAGE="${PG_IMAGE:-postgres:15-alpine}"
CONTAINER="vrcverify-test-pg-$$"
# Not 5432: a developer running a local Postgres for anything else should not
# have to stop it, and a collision here would silently use the wrong database.
PORT="${VRCVERIFY_TEST_PG_PORT:-55432}"
PASSWORD="test-only-not-a-secret"

if [ ! -f "$SNAPSHOT" ]; then
    echo "Missing $SNAPSHOT. Run ./scripts/refresh_schema_snapshot.sh first." >&2
    exit 1
fi

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Starting $PG_IMAGE on port $PORT..."
docker run -d --rm --name "$CONTAINER" \
    -e POSTGRES_PASSWORD="$PASSWORD" \
    -e POSTGRES_DB=vrcverify_test \
    -p "127.0.0.1:$PORT:5432" \
    "$PG_IMAGE" >/dev/null

# pg_isready rather than a fixed sleep: the first start pulls the image and
# initializes a cluster, and "long enough on my machine" is how a flaky suite
# begins. Postgres also comes up once, shuts down and comes up again during
# initdb, so this waits for a real connection rather than for the port.
echo -n "Waiting for Postgres"
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -U postgres -d vrcverify_test -q 2>/dev/null; then
        break
    fi
    echo -n "."
    sleep 1
done
echo
if ! docker exec "$CONTAINER" pg_isready -U postgres -d vrcverify_test -q 2>/dev/null; then
    echo "Postgres did not become ready. Container logs:" >&2
    docker logs "$CONTAINER" >&2
    exit 1
fi

echo "Loading schema/production.sql..."
# ON_ERROR_STOP so a snapshot that does not load is a failure here rather than
# a confusing cascade of missing-table errors inside pytest.
docker exec -i "$CONTAINER" \
    psql -v ON_ERROR_STOP=1 -q -U postgres -d vrcverify_test < "$SNAPSHOT"

export VRCVERIFY_TEST_DATABASE_URL="postgresql://postgres:$PASSWORD@127.0.0.1:$PORT/vrcverify_test"

echo "Running pytest against Postgres..."
cd "$REPO_ROOT"
python3 -m pytest "$@"
