"""Server-side sessions, in SQLite.

Server-side rather than a signed cookie for two reasons that matter here:

* **They can be destroyed.** A cookie session is valid until it expires, no
  matter what you learn afterwards. A row can be deleted, which is what makes
  logout mean something and what lets a compromised session be cut off.
* **The cached guild list would not fit in a cookie**, and putting it there
  would hand every user an editable copy of what the picker renders from.

What is deliberately *not* stored: the Discord access token, and the refresh
token. Both are discarded the moment login completes (see `oauth.py`). The
public host therefore holds no credential that can act as the user against
Discord — only their id and a short-lived copy of their guild list, which is
data rather than authority. Refreshing that list means logging in again, and
that is the intended trade rather than an omission.

The schema is created on first use. There is no migration story because there
is nothing here worth migrating: worst case the file is deleted and everyone
logs in again.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

# 32 bytes of urandom, url-safe. The session id is the only thing standing
# between a cookie and an account, so it is generated the same way the API
# tokens are rather than from anything predictable.
SESSION_ID_BYTES = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    sid            TEXT PRIMARY KEY,
    discord_id     TEXT,
    oauth_state    TEXT,
    csrf_token     TEXT NOT NULL,
    guilds_json    TEXT,
    guilds_at      INTEGER,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions (expires_at);
"""


@dataclass
class Session:
    sid: str
    discord_id: Optional[str]
    oauth_state: Optional[str]
    csrf_token: str
    guilds: Optional[list]
    guilds_at: Optional[int]
    created_at: int
    expires_at: int

    @property
    def authenticated(self) -> bool:
        """A session exists from the moment login *starts*, not when it ends.

        The pre-auth row is what carries the OAuth state across the redirect.
        It is a session with no identity, and must never be treated as one.
        """
        return self.discord_id is not None

    def guilds_fresh(self, ttl: int, now: Optional[float] = None) -> bool:
        if self.guilds is None or self.guilds_at is None:
            return False
        current = now if now is not None else time.time()
        return (current - self.guilds_at) < ttl


class SessionStore:
    def __init__(self, path: str, max_age: int):
        self.path = path
        self.max_age = max_age
        self._connect().close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    # ----- lifecycle -----
    def begin_login(self, oauth_state: str, now: Optional[float] = None) -> Session:
        """Create the pre-auth row that carries OAuth state across the redirect."""
        current = int(now if now is not None else time.time())
        session = Session(
            sid=secrets.token_urlsafe(SESSION_ID_BYTES),
            discord_id=None,
            oauth_state=oauth_state,
            csrf_token=secrets.token_urlsafe(32),
            guilds=None,
            guilds_at=None,
            created_at=current,
            # A login that is started and abandoned should not sit around for
            # the full session lifetime.
            expires_at=current + 600,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (sid, discord_id, oauth_state, csrf_token,"
                " guilds_json, guilds_at, created_at, expires_at)"
                " VALUES (?, NULL, ?, ?, NULL, NULL, ?, ?)",
                (
                    session.sid,
                    session.oauth_state,
                    session.csrf_token,
                    session.created_at,
                    session.expires_at,
                ),
            )
        return session

    def complete_login(
        self, sid: str, discord_id: str, guilds: list, now: Optional[float] = None
    ) -> Session:
        """Promote a pre-auth row to an authenticated one, under a NEW id.

        The id change is not cosmetic. Without it, an attacker who can set a
        victim's cookie before login (session fixation) still knows the id
        afterwards, and the victim authenticates the attacker's session for
        them. Issuing a fresh id at the moment privilege is granted breaks
        that, and the old row is deleted rather than left usable.
        """
        current = int(now if now is not None else time.time())
        session = Session(
            sid=secrets.token_urlsafe(SESSION_ID_BYTES),
            discord_id=str(discord_id),
            oauth_state=None,
            csrf_token=secrets.token_urlsafe(32),
            guilds=guilds,
            guilds_at=current,
            created_at=current,
            expires_at=current + self.max_age,
        )
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            conn.execute(
                "INSERT INTO sessions (sid, discord_id, oauth_state, csrf_token,"
                " guilds_json, guilds_at, created_at, expires_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    session.sid,
                    session.discord_id,
                    session.csrf_token,
                    json.dumps(guilds),
                    session.guilds_at,
                    session.created_at,
                    session.expires_at,
                ),
            )
        return session

    def load(self, sid: Optional[str], now: Optional[float] = None) -> Optional[Session]:
        if not sid:
            return None
        current = int(now if now is not None else time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE sid = ?", (sid,)
            ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= current:
            # Expired rows are removed on sight rather than waiting for a
            # sweep, so an expired session can never be resurrected by a clock
            # adjustment.
            self.destroy(sid)
            return None
        return Session(
            sid=row["sid"],
            discord_id=row["discord_id"],
            oauth_state=row["oauth_state"],
            csrf_token=row["csrf_token"],
            guilds=json.loads(row["guilds_json"]) if row["guilds_json"] else None,
            guilds_at=row["guilds_at"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def destroy(self, sid: Optional[str]) -> None:
        if not sid:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))

    def purge_expired(self, now: Optional[float] = None) -> int:
        current = int(now if now is not None else time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (current,)
            )
            return cursor.rowcount
