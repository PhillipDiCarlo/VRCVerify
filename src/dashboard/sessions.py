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
import logging
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

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
    expires_at     INTEGER NOT NULL,
    -- A one-shot notice for the next render: "saved", "panel:moved",
    -- "error:requires_premium". Server-side so the page cannot be made to
    -- claim something happened by anyone who can craft a link.
    notice         TEXT
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
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Owner-only on the file holding every live session id.

        These ids are bearer credentials: anything that can read this file can
        act as any admin signed in right now, without a password, a cookie, or
        Discord. It sits on the public host, which section 2 of the audit tells
        you to assume is compromised eventually -- so the point is not to stop
        an attacker who is already root, it is to keep the file out of reach of
        a second process, a stray backup, or another container user.

        A failure is logged, never fatal. Refusing to serve because a chmod
        returned EPERM would take the dashboard down over a hardening measure,
        and on Windows -- where developers run this -- the call is close to a
        no-op anyway. The log line is what turns it into something an operator
        can notice.
        """
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as error:
            logger.warning(
                "Could not restrict permissions on the session database at %s "
                "(%s). Every live session id is readable by anything that can "
                "open it; check the file mode by hand.",
                self.path,
                error,
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # `notice` arrived after the first sessions.sqlite files existed, and a
        # session store is not worth a migration framework: an existing file
        # would otherwise keep working right up until the first save, then fail
        # on an unknown column.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "notice" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN notice TEXT")
            conn.commit()
        return conn

    # ----- one-shot notices -----
    def set_notice(self, sid: Optional[str], notice: Optional[str]) -> None:
        """Park a notice for the next page this session renders.

        Notices used to travel as query parameters, which meant anyone who could
        get an admin to open a link could show them "Saved." for a save that
        never happened -- most usefully to stop them noticing something was
        broken. Here it is written by the request that actually did the thing.
        """
        if not sid:
            return
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET notice = ? WHERE sid = ?", (notice, sid))

    def take_notice(self, sid: Optional[str]) -> Optional[str]:
        """Read the pending notice and clear it, so a reload does not repeat it."""
        if not sid:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notice FROM sessions WHERE sid = ?", (sid,)
            ).fetchone()
            if row is None or row["notice"] is None:
                return None
            conn.execute("UPDATE sessions SET notice = NULL WHERE sid = ?", (sid,))
            return row["notice"]

    # ----- lifecycle -----
    def begin_login(self, oauth_state: str, now: Optional[float] = None) -> Session:
        """Create the pre-auth row that carries OAuth state across the redirect.

        Sweeps expired rows on the way in, and that placement is the point.
        Starting a login is the only unauthenticated way to add a row to this
        file, so the request that grows the table is the request that prunes
        it. Nothing schedules this and nothing needs to: with the Cloudflare
        Access policy removed (A-14) `/login` faces the open internet, and
        anybody hammering it to pile up abandoned pre-auth rows is also
        clearing the ones they made ten minutes ago.

        `load()` already drops an expired row it is handed, but only that one,
        and only when someone presents its id -- which is exactly what an
        abandoned row never gets. Lazy deletion alone leaves the file growing
        forever.
        """
        current = int(now if now is not None else time.time())
        self.purge_expired(current)
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

    def destroy_all_for(self, discord_id: Optional[str]) -> int:
        """Every session belonging to one Discord user. Returns how many.

        The answer to "my laptop was stolen" and to "I think someone has my
        cookie". Signing out normally ends the session in front of you, which
        is precisely the one an attacker is not using -- theirs stays good
        until `max_age`, and nothing else in this codebase would ever cut it
        off.

        Scoped per USER and never per guild, even though a guild is what an
        admin would think to secure. A session is one person's, and it spans
        every server they administer; "revoke everyone's access to this
        server" would either destroy sessions belonging to people who did
        nothing, or destroy nothing at all. Neither is what the words promise.

        Pre-auth rows are untouched: they carry no identity to match on, hold
        no authority, and expire in ten minutes.
        """
        if not discord_id:
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE discord_id = ?", (str(discord_id),)
            )
            return cursor.rowcount

    def purge_expired(self, now: Optional[float] = None) -> int:
        current = int(now if now is not None else time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (current,)
            )
            return cursor.rowcount
