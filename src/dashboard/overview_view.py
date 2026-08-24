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


# --- the trend chart ---
#
# viewBox units, not pixels: the SVG scales to whatever width the CSS gives it,
# and every coordinate below is computed in this fixed space regardless of the
# reader's screen.
CHART_VIEW_WIDTH = 300
CHART_VIEW_HEIGHT = 64
CHART_BAR_GAP = 1.0
# A real zero has to remain a bar, not a point -- this is the floor that keeps
# it visible rather than collapsing to a 0px rect indistinguishable from the
# blank space where an unmeasured day draws nothing at all.
CHART_MIN_BAR_HEIGHT = 2.0


class ChartBar:
    """One day's column.

    `height` is None for an unmeasured day, and THAT IS THE POINT: the
    template draws a <rect> only when `height` is not None, so a day before
    `collecting_since` is an honest gap in the chart -- no element there at
    all -- rather than a bar interpolating across a day nothing is known
    about. A bar chart can represent "nothing here" natively; a line cannot.
    """

    def __init__(self, day: str, count: Optional[int], x: float, height: Optional[float]):
        self.day = day
        self.count = count
        self.x = x
        self.height = height

    @property
    def y(self) -> float:
        """The rect's top edge. Only meaningful when `height` is not None; the
        template never reads it otherwise."""
        return CHART_VIEW_HEIGHT - (self.height or 0.0)


class Chart:
    """The verification trend, with every SVG coordinate already computed.

    Nothing in overview.html does arithmetic -- geometry lives here so the
    cases that matter (an empty series, a spike, a window straddling the
    collection floor) are testable without rendering a template, the same
    reasoning `Tile` is built on.

    THE SAME THREE STATES AS A TILE, for the same reason: "unknown" is the
    bot failing to answer, which is not the same fact as "blank", which is a
    successful read of a question the data cannot answer yet. A chart with
    every day genuinely unmeasured (`state="blank"`) must not be confused
    with a chart nobody could ask (`state="unknown"`) -- the copy beside each
    says a different thing, and collapsing them would repeat the exact
    mistake `Tile.display` exists to prevent, just one level up.
    """

    def __init__(self, bars=None, *, state: str = "value", note: Optional[str] = None,
                 bar_width: float = 0.0):
        self.bars = bars or []
        self.state = state
        self.note = note
        self.bar_width = bar_width
        self.width = CHART_VIEW_WIDTH
        self.height = CHART_VIEW_HEIGHT


def build_chart(overview: Optional[dict]) -> Chart:
    """The verification trend chart, ready for the template to draw.

    Mirrors `_window_tile`'s three-way split rather than inventing a fourth
    vocabulary for the same idea:

    * **value** -- at least one day in the series was actually measured, so
      there is something to draw. This covers "all measured days are zero"
      too, which is real data and gets bars at the floor height, not the
      blank state -- the chart-level version of the falsy-zero bug `Tile`
      guards against.
    * **blank** -- the read succeeded and every day came back unmeasured,
      which happens for a server on a fleet that has never collected
      anything anywhere. Not a failure; there is simply nothing yet.
    * **unknown** -- the rollup could not be read at all. Mirrors
      `_window_tile`'s `known` check exactly, from the same payload flag, so
      the tiles and the chart can never disagree about whether the read
      itself succeeded.
    """
    if not overview:
        return Chart(state="unknown", note="The bot didn't answer this one.")

    counts = overview.get("verifications") or {}
    known = bool(counts.get("known", True))
    daily = counts.get("daily")
    since = counts.get("collecting_since")

    if not known or daily is None:
        return Chart(state="unknown", note="The bot didn't answer this one.")

    if not any(entry.get("count") is not None for entry in daily):
        note = f"Only counting since {since}." if since else "Not collecting yet."
        return Chart(state="blank", note=note)

    day_count = len(daily)
    bar_width = (
        (CHART_VIEW_WIDTH - CHART_BAR_GAP * (day_count - 1)) / day_count
        if day_count
        else 0.0
    )

    measured = [entry["count"] for entry in daily if entry.get("count") is not None]
    peak = max(measured) if measured else 0

    bars = []
    for index, entry in enumerate(daily):
        x = index * (bar_width + CHART_BAR_GAP)
        count = entry.get("count")
        if count is None:
            bars.append(ChartBar(entry["day"], None, x, None))
            continue
        if peak > 0:
            # Floored rather than left to round to nothing: a small count
            # against a tall spike must still read as "measured, and low" --
            # not vanish into the same nothing an unmeasured day draws.
            height = max(CHART_MIN_BAR_HEIGHT, (count / peak) * CHART_VIEW_HEIGHT)
        else:
            # Every measured day is zero. There is no spike to scale against,
            # so every bar sits at the floor -- visibly present, at the
            # baseline, which is exactly what "measured and quiet" looks like.
            height = CHART_MIN_BAR_HEIGHT
        bars.append(ChartBar(entry["day"], count, x, height))

    return Chart(bars, state="value", bar_width=bar_width)


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
