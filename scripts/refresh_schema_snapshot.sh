#!/usr/bin/env bash
#
# Refresh schema/production.sql from the deployed database.
#
# READ-ONLY. pg_dump --schema-only reads catalog tables and writes nothing, and
# nothing here imports src/bot.py -- which matters, because importing bot.py
# opens a writable engine against whatever DATABASE_URL says, and that is how
# the servers table got emptied on 2026-09-08 (VPS_RUNBOOK.md section 14b).
#
# The dump runs inside a throwaway postgres container so no local pg_dump is
# needed, and so the client version is pinned rather than being whatever the
# machine happens to have. Keep PG_IMAGE's major version matching the server:
# pg_dump refuses to dump a server newer than itself.
#
# Usage, from the repo root:
#
#   ./scripts/refresh_schema_snapshot.sh                  # reads .env
#   DATABASE_URL=postgresql://... ./scripts/refresh_schema_snapshot.sh
#
# Then read the diff before committing. A change is either a migration you
# meant to run or one somebody ran by hand, and both are worth knowing about.

set -euo pipefail

PG_IMAGE="${PG_IMAGE:-postgres:15-alpine}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/schema/production.sql"

if [ -z "${DATABASE_URL:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
    # Only this one variable, and without echoing it. Sourcing the whole file
    # would also pull in the Discord token and the VRChat credentials, which
    # this script has no business holding.
    DATABASE_URL="$(grep -m1 '^DATABASE_URL=' "$REPO_ROOT/.env" | cut -d= -f2-)"
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL is not set and .env has no DATABASE_URL line." >&2
    exit 1
fi

case "$DATABASE_URL" in
    sqlite*)
        # The suite pins DATABASE_URL to SQLite, so a shell that has run the
        # tests can reach here with the wrong value and produce an empty dump.
        echo "DATABASE_URL points at SQLite. This snapshot has to come from Postgres." >&2
        exit 1
        ;;
esac

# The note at the top of the snapshot is carried across refreshes, with only its
# capture date rewritten -- so a refresh cannot leave a stale date above a
# schema that has moved on, and cannot lose the explanation either.
#
# It is delimited by a sentinel line rather than by the first blank line. That
# was the first version and it was wrong in a way worth recording: `$(...)`
# strips trailing newlines, so the blank line separating note from dump was
# dropped on write, the next run's search for the first blank line then ran
# three lines into pg_dump's own header comment, and the file grew by three
# lines per refresh. A sentinel cannot drift like that.
SENTINEL="-- (end of note; everything below is pg_dump output)"

if ! grep -qF "$SENTINEL" "$OUT"; then
    echo "$OUT has lost its sentinel line. Restore it before refreshing:" >&2
    echo "  $SENTINEL" >&2
    exit 1
fi

HEADER="$(sed -n "1,/^$(printf '%s' "$SENTINEL" | sed 's/[][\.*^$\/()]/\\&/g')\$/p" "$OUT" |
    sed "s/^-- VRCVerify production schema, captured .*/-- VRCVerify production schema, captured $(date -u +%Y-%m-%d)./")"

{
    printf '%s\n\n' "$HEADER"
    docker run --rm -e DBURL="$DATABASE_URL" "$PG_IMAGE" \
        sh -c 'pg_dump "$DBURL" --schema-only --no-owner --no-privileges --no-comments' |
        # See the note in the snapshot header: the token in these two lines is
        # random per dump, so leaving them in makes every refresh diff dirty.
        sed '/^\\restrict /d; /^\\unrestrict /d'
} > "$OUT.tmp"

mv "$OUT.tmp" "$OUT"
echo "Wrote $OUT"
git -C "$REPO_ROOT" --no-pager diff --stat -- "$OUT"
