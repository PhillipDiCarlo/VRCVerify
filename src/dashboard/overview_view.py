"""Turns the bot API's overview payload into tiles a template can render.

Pure, like `settings_view`: no Flask, no network, no clock. Everything arrives
as an argument, so the cases worth pinning -- a window with nothing behind it,
a rollup that could not be read, a count that is genuinely zero -- are testable
without a request.

THREE STATES PER TILE, AND THEY MUST STAY APART
-----------------------------------------------
A number on this page answers "is verification working in my server". Each of
the three ways a tile can be non-numeric means something different, and
collapsing any two of them tells an admin something untrue:

* **A count.** `12`. The window is covered by collected data and that is what
  happened in it.
* **Zero.** `0`. Also a real answer, and often the interesting one -- a panel
  is up and nobody is using it. Rendering this as blank would hide a server
  that is quietly broken.
* **Blank.** The window reaches back further than the rollup has been
  collecting, so no number would be true. Not zero: nothing was measured.
* **Unknown.** The bot could not answer at all. Not blank: blank is a
  successful read of a question the data cannot answer yet, and this is the
  page failing. Says "Couldn't check", the same words the settings page uses.

`Tile.state` is what the template switches on, so adding a fourth state is a
change here rather than a new branch scattered through the HTML.
"""

from __future__ import annotations

from typing import Optional

# How each window is labelled. "Today (UTC)" rather than "last 24 hours"
# because the rollup stores days, not moments -- see `_verification_windows` in
# bot.py. A tile promising a rolling day and delivering a calendar one is a
# small lie that gets noticed at midnight.
WINDOWS = (
    ("today", "Today (UTC)"),
    ("last_7_days", "Last 7 days"),
    ("last_30_days", "Last 30 days"),
)


class Tile:
    """One figure on the Overview. Attributes, because Jinja reads them cleanly."""

    def __init__(
        self,
        label: str,
        value=None,
        *,
        state: str = "value",
        note: Optional[str] = None,
    ):
        self.label = label
        # None unless `state` is "value". The template never formats this
        # itself, so a None can't reach the page as the word "None".
        self.value = value
        # "value" | "blank" | "unknown"
        self.state = state
        self.note = note

    @property
    def display(self) -> str:
        """What the tile actually prints.

        Zero prints as "0". That is the whole reason this is a method and not
        `value or "-"`, which is exactly the falsy-zero bug this page cannot
        afford.
        """
        if self.state == "unknown":
            return "Couldn't check"
        if self.state == "blank" or self.value is None:
            return "—"
        return f"{self.value:,}"


def _window_tile(label: str, count, known: bool, collecting_since: Optional[str]) -> Tile:
    """One window's tile, in whichever of the three states applies."""
    if not known:
        return Tile(label, state="unknown", note="The bot didn't answer this one.")
    if count is None:
        note = "Not collecting that far back yet."
        if collecting_since:
            note = f"Only counting since {collecting_since}."
        return Tile(label, state="blank", note=note)
    return Tile(label, count)


def build_tiles(overview: Optional[dict]) -> list:
    """The stat row: members, then one tile per window.

    An all-time total is included only when the bot reported one. It is None
    for a deployment whose `servers` table never got the `verification_count`
    column, and a tile reading 0 for a server with thousands of verifications
    is worse than no tile at all.
    """
    if not overview:
        return []

    counts = overview.get("verifications") or {}
    known = bool(counts.get("known", True))
    since = counts.get("collecting_since")

    tiles = []

    members = overview.get("member_count")
    tiles.append(
        Tile("Members", members)
        if isinstance(members, int)
        else Tile("Members", state="unknown")
    )

    for key, label in WINDOWS:
        tiles.append(_window_tile(label, counts.get(key), known, since))

    total = counts.get("total")
    if isinstance(total, int):
        tiles.append(
            Tile(
                "Verified, all time",
                total,
                note="Counted since this server's records began.",
            )
        )

    return tiles


# Which configuration the Overview reports, and what each one being off
# actually means. The wording carries the difference: a missing verified role
# stops verification, while a missing log channel is a choice most servers make
# and must not read as a fault.
SETUP_ROWS = (
    ("verified_role", "Verified role", "Required — verification can't finish without one."),
    ("auto_verify", "Auto-verify on join", "Members are checked when they join."),
    ("unverified_role", "Unverified role", "Optional. Removed once someone verifies."),
    ("log_channel", "Verification log", "Optional. Premium."),
)


def build_setup(overview: Optional[dict]) -> list:
    """Which pieces of configuration are in place, as yes/no.

    Booleans from the bot, never the ids themselves -- the values live on the
    Settings page, and a second rendering of them here would be a second place
    for them to be wrong. This page answers "is it wired up", which is the
    question you have while looking at a count you did not expect.
    """
    if not overview:
        return []
    configured = overview.get("configured")
    if configured is None:
        # The settings read failed behind the overview. Saying nothing beats
        # reporting four features as switched off.
        return []

    return [
        {
            "label": label,
            "on": bool(configured.get(key)),
            "note": note,
            # Only the verified role is a problem when missing. The other three
            # are ordinary choices, and marking them red would tell an admin
            # their working server is broken.
            "required": key == "verified_role",
        }
        for key, label, note in SETUP_ROWS
    ]


# What to tell an admin to do, in the order it stops verification working. Only
# the first one is shown: a page listing four things wrong is a page nobody
# acts on, and the verified role genuinely blocks everything after it.
def build_next_step(overview: Optional[dict]) -> Optional[dict]:
    """The single most useful thing this server could do next, if anything.

    Deliberately one item. The two conditions below are the entire reason a
    working install produces no verifications, and they are ordered: without a
    verified role the bot cannot finish a verification at all, and without a
    panel nobody can start one.

    Returns None when neither applies, which is the common case -- a configured
    server should not be nagged about anything.
    """
    if not overview:
        return None

    configured = overview.get("configured")
    if configured is not None and not configured.get("verified_role"):
        return {
            "title": "No verified role is set",
            "body": (
                "VRCVerify can't finish a verification without a role to give "
                "people. Set one in Settings."
            ),
        }

    panel = overview.get("panel")
    # `posted: false` is not proof there is no panel -- the bot returns the
    # same thing when it could not read the record -- but the advice is
    # harmless either way, and reposting an existing panel refreshes it rather
    # than duplicating it.
    if isinstance(panel, dict) and not panel.get("posted"):
        return {
            "title": "No instructions panel is posted",
            "body": (
                "Members need a message to start from. Post one from the "
                "Instructions panel section in Settings."
            ),
        }

    return None
