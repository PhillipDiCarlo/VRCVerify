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


# The optional toggles: on/off only, no health question behind either state.
# The wording carries the difference from the two rows above them -- these are
# ordinary choices, and marking one red for being off would tell an admin
# their working server is broken.
OPTIONAL_ROWS = (
    ("auto_verify", "Auto-verify on join", "Members are checked when they join."),
    ("unverified_role", "Unverified role", "Optional. Removed once someone verifies."),
    ("log_channel", "Verification log", "Optional. Premium."),
)

# Where each fixable row's action points, as a fragment on the Settings page
# rather than a full URL -- this module stays Flask-free, like `settings_view`,
# so the template is what turns this into `url_for('guild_settings', ...) +
# '#' + anchor`.
_SETTINGS_ANCHOR = {"verified_role": "f-role_id", "panel": "panel_channel_id"}


def _role_row(configured: dict) -> dict:
    """Verified role: set, still exists, and the bot can actually grant it.

    All three ways this can be unfinished point at the same fix -- the role
    picker in Settings, which already flags an unassignable role (see
    `read_dashboard_roles`'s `assignable`) when choosing one -- so every
    non-done state gets the same action rather than three different ones.
    """
    action = {"label": "Go to Settings", "anchor": _SETTINGS_ANCHOR["verified_role"]}
    if not configured.get("verified_role"):
        return {
            "label": "Verified role",
            "state": "todo",
            "note": "Required — verification can't finish without one.",
            "action": action,
        }
    if configured.get("verified_role_exists") is False:
        return {
            "label": "Verified role",
            "state": "broken",
            "note": "The role that was set has been deleted. Choose another.",
            "action": action,
        }
    if configured.get("verified_role_assignable") is False:
        return {
            "label": "Verified role",
            "state": "broken",
            "note": "VRCVerify's own role needs to sit above this one to grant it.",
            "action": action,
        }
    # `verified_role_assignable` of None means the hierarchy could not be
    # checked -- not proof it is broken, so this stays "done" rather than
    # crying wolf on a working server. Same restraint `read_dashboard_panel`
    # takes with a channel it cannot confirm is postable.
    return {
        "label": "Verified role",
        "state": "done",
        "note": "Set, and VRCVerify can grant it.",
        "action": None,
    }


def _panel_row(panel: Optional[dict]) -> dict:
    """Instructions panel: posted, in a channel that still exists, and one the
    bot can still post to. Mirrors `_role_row`'s three-way split for the same
    reason -- "not set up" and "set up and now broken" need different notes
    even though both need the same fix."""
    action = {"label": "Go to Settings", "anchor": _SETTINGS_ANCHOR["panel"]}
    if not panel or not panel.get("posted"):
        return {
            "label": "Instructions panel",
            "state": "todo",
            "note": "Members need a message to start from.",
            "action": action,
        }
    if panel.get("channel_exists") is False:
        return {
            "label": "Instructions panel",
            "state": "broken",
            "note": "The channel it was posted in was deleted.",
            "action": action,
        }
    if panel.get("channel_postable") is False:
        return {
            "label": "Instructions panel",
            "state": "broken",
            "note": "VRCVerify can't post there anymore — check its permissions.",
            "action": action,
        }
    return {
        "label": "Instructions panel",
        "state": "done",
        "note": "Posted, and VRCVerify can still reach it.",
        "action": None,
    }


def build_setup(overview: Optional[dict]) -> Optional[dict]:
    """The Apollo-pattern list: setup and health merged into one, each row
    carrying its own state and its own fix.

    Two lists reporting different questions -- "is it configured" and "is it
    working" -- used to sit apart on this page, which is worse than either
    alone: an admin staring at four ticks has no way to learn the role behind
    one of them was deleted last week. One row per concern instead, in the
    order that blocks verification: the role first, since nothing after it
    matters without one; the panel second, since nothing starts without it;
    the three ordinary choices last, where being off is never a fault.

    `complete` is true exactly when the two required rows are both "done" --
    not when every row is, since an optional toggle left off is not something
    left to finish. Phase 4 rewires the premium slot to appear only then.

    Returns None -- not a dict with empty rows -- when nothing could be read,
    so `{% if setup %}` keeps hiding the whole section exactly as it did when
    this returned an empty list.
    """
    if not overview:
        return None
    configured = overview.get("configured")
    if configured is None:
        # The settings read failed behind the overview. Saying nothing beats
        # reporting every row as switched off.
        return None

    role = _role_row(configured)
    panel = _panel_row(overview.get("panel"))

    rows = [role, panel] + [
        {
            "label": label,
            "state": "done" if configured.get(key) else "off",
            "note": note,
            "action": None,
        }
        for key, label, note in OPTIONAL_ROWS
    ]

    return {
        "rows": rows,
        "complete": role["state"] == "done" and panel["state"] == "done",
    }


def _setup_step(setup: dict) -> dict:
    """The single most useful setup row to surface at the top of the page,
    for whichever of the two required rows isn't done.

    Reuses `build_setup`'s own row -- state, note, and all -- rather than a
    second copy of "is the role missing" that could disagree with the list
    right below it. `state` decides only the *title*, because "todo" and
    "broken" already have distinct, accurate notes from #135 phase 3 and
    duplicating that wording here is how the two drift.
    """
    by_label = {row["label"]: row for row in setup["rows"]}

    role = by_label["Verified role"]
    if role["state"] != "done":
        title = "No verified role is set" if role["state"] == "todo" \
            else "The verified role needs attention"
        return {"title": title, "body": role["note"], "action": "settings"}

    panel = by_label["Instructions panel"]
    title = "No instructions panel is posted" if panel["state"] == "todo" \
        else "The instructions panel needs attention"
    return {"title": title, "body": panel["note"], "action": "settings"}


def _demo_step(overview: dict) -> Optional[dict]:
    """The data-backed pitch: a real number this server produced, paired with
    what Premium would do with it. The lock reads as a loss instead of an
    abstract paywall specifically because the number is theirs.

    Three ways this stays silent rather than guessing: a `blank` or `unknown`
    30-day figure is never rendered (that would be inventing a number this
    page doesn't have), a covered-but-empty window gets no pitch either ("0
    members verified, upgrade to log them" argues against buying, not for
    it), and an already-premium server sees nothing here at all -- there is
    nothing left to sell it.
    """
    premium = overview.get("premium") or {}
    if premium.get("premium"):
        return None

    counts = overview.get("verifications") or {}
    if not counts.get("known", True):
        return None
    count = counts.get("last_30_days")
    if not isinstance(count, int) or count <= 0:
        return None

    pitch = (
        f"{count:,} member{'' if count == 1 else 's'} verified here in the "
        "last 30 days. Premium logs each one to a channel of your choice."
    )
    if premium.get("grandfathered"):
        # Leads with what stays free, per the issue's own rule -- the model
        # is settings.html's upgrade card, which makes the same two-sentence
        # move: reassure, then offer. Truthful either way, since the activity
        # log was never one of the grandfathered extras (see
        # GRANDFATHERED_FEATURES in bot.py) -- this server really would be
        # buying something new, not being asked to pay for what it already
        # has.
        return {
            "title": "Add VRCVerify Premium",
            "body": f"Your grandfathered extras stay free whatever you decide. {pitch}",
            "action": "subscription",
        }
    return {
        "title": "Upgrade to VRCVerify Premium",
        "body": pitch,
        "action": "subscription",
    }


def build_next_step(
    overview: Optional[dict], changelog_entry: Optional[dict] = None
) -> Optional[dict]:
    """The single most useful thing to put in the best attention slot on the
    page, if anything. At most one item, ranked:

    1. A genuine setup step -- always wins. A server that cannot finish a
       verification must not be sold to instead of fixed.
    2. `changelog_entry`, an undismissed premium changelog entry. This
       parameter is #136's contract, not #135's: nothing calls this with one
       yet, and the default keeps today's behaviour exactly as it was. #135
       only defines where it ranks and what shape it needs -- the same shape
       this function itself returns, `{"title", "body", "action"}` -- so #136
       can hand back exactly what should be shown without this function
       needing to know anything about changelogs.
    3. The data-backed demo, `_demo_step` -- suppressed for a premium server,
       and never rendered from a figure this page cannot stand behind.

    Returns None when nothing applies, which is the common case: a fully
    configured premium server with nothing new to announce should not be
    nagged about anything.
    """
    if not overview:
        return None

    setup = build_setup(overview)
    if setup is None:
        # The settings read failed behind the overview -- build_setup already
        # says nothing rather than guess, and repeating half its checks here
        # with the other half missing would be worse than matching it.
        return None
    if not setup["complete"]:
        return _setup_step(setup)

    if changelog_entry:
        return changelog_entry

    return _demo_step(overview)
