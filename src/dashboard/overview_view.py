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

from typing import Callable, Optional

# The no-op translation marker (#97). Tables here are built at import, so they
# hold msgids and the lookup happens per request against the callable passed
# in. This module is pure -- no Flask, no network, no clock -- and i18n.py is
# too, so importing `N_` costs it nothing it promises.
from dashboard.i18n import N_


def _untranslated(text: str) -> str:
    """The default `t`: hand back the English, unchanged.

    Keeps every entry point below callable with no request in sight, which is
    what the tests asserting a zero-count tile is not a blank one rely on.
    """
    return text


# How each window is labelled. "Today (UTC)" rather than "last 24 hours"
# because the rollup stores days, not moments -- see `_verification_windows` in
# bot.py. A tile promising a rolling day and delivering a calendar one is a
# small lie that gets noticed at midnight.
WINDOWS = (
    ("today", N_("Today (UTC)")),
    ("last_7_days", N_("Last 7 days")),
    ("last_30_days", N_("Last 30 days")),
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
        t: Callable[[str], str] = _untranslated,
    ):
        # Kept for `display`, which is a property and so runs after this
        # object is built. Underscored: it is machinery, and everything else
        # here is something Jinja reads.
        self._t = t
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
            return self._t(N_("Couldn't check"))
        if self.state == "blank" or self.value is None:
            return "—"
        return f"{self.value:,}"


def _window_tile(
    label: str,
    count,
    known: bool,
    collecting_since: Optional[str],
    t: Callable[[str], str] = _untranslated,
) -> Tile:
    """One window's tile, in whichever of the three states applies."""
    if not known:
        return Tile(
            t(label), state="unknown", note=t(N_("The bot didn't answer this one.")), t=t
        )
    if count is None:
        note = t(N_("Not collecting that far back yet."))
        if collecting_since:
            note = t(N_("Only counting since %(date)s.")) % {"date": collecting_since}
        return Tile(t(label), state="blank", note=note, t=t)
    return Tile(t(label), count, t=t)


def build_tiles(
    overview: Optional[dict], t: Callable[[str], str] = _untranslated
) -> list:
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
        Tile(t(N_("Members")), members, t=t)
        if isinstance(members, int)
        else Tile(t(N_("Members")), state="unknown", t=t)
    )

    for key, label in WINDOWS:
        tiles.append(_window_tile(label, counts.get(key), known, since, t))

    total = counts.get("total")
    if isinstance(total, int):
        tiles.append(
            Tile(
                t(N_("Verified, all time")),
                total,
                note=t(N_("Counted since this server's records began.")),
                t=t,
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

    @property
    def peak(self) -> Optional[int]:
        """The busiest measured day in the window, or None if none was.

        THE SCALE THIS CHART HAD NO WAY TO SHOW (#195 phase 8). Bars are drawn
        against the peak, so without naming it a reader has heights and no
        units: one tall bar beside a row of floor-height slivers is the shape
        of a quiet month and the shape of a broken chart, and nothing on the
        page said which.

        Read off the bars rather than recomputed from the payload -- the bars
        are what was drawn, so a peak taken from anywhere else could disagree
        with the tallest thing on screen. Unmeasured days carry `count` None
        and are skipped; they are a gap, not a zero.
        """
        counts = [bar.count for bar in self.bars if bar.count is not None]
        return max(counts) if counts else None


def build_chart(
    overview: Optional[dict], t: Callable[[str], str] = _untranslated
) -> Chart:
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
        return Chart(state="unknown", note=t(N_("The bot didn't answer this one.")))

    counts = overview.get("verifications") or {}
    known = bool(counts.get("known", True))
    daily = counts.get("daily")
    since = counts.get("collecting_since")

    if not known or daily is None:
        return Chart(state="unknown", note=t(N_("The bot didn't answer this one.")))

    if not any(entry.get("count") is not None for entry in daily):
        note = (
            t(N_("Only counting since %(date)s.")) % {"date": since}
            if since
            else t(N_("Not collecting yet."))
        )
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
    (
        "auto_verify",
        N_("Auto-verify on join"),
        N_("Members are checked when they join."),
    ),
    (
        "unverified_role",
        N_("Unverified role"),
        N_("Optional. Removed once someone verifies."),
    ),
    ("log_channel", N_("Verification log"), N_("Optional. Premium.")),
)

# Where each fixable row's action points: which settings group, and which field
# on it. Not a full URL -- this module stays Flask-free, like `settings_view`,
# so the template is what turns this into `url_for('guild_settings', ...,
# group=...) + '#' + anchor`.
#
# THE GROUP IS NOT DECORATION. Settings is a page per group now (#140), and a
# fragment alone would land wherever the bare /settings URL redirects to. That
# happens to be the Verification page, so `f-role_id` would have gone on
# working by luck while `panel_channel_id` scrolled to nothing at all -- no
# error, no log line, a button that simply stops taking you anywhere. The slugs
# are the fix, which is why they ship in the same phase as the split.
_SETTINGS_ANCHOR = {
    "verified_role": ("verification", "f-role_id"),
    "panel": ("panel", "panel_channel_id"),
}


def _settings_action(
    key: str,
    label: str = N_("Go to Settings"),
    t: Callable[[str], str] = _untranslated,
) -> dict:
    """A "fix it here" button, aimed at the field rather than at the page."""
    group, anchor = _SETTINGS_ANCHOR[key]
    return {"label": t(label), "group": group, "anchor": anchor}


def _role_row(configured: dict, t: Callable[[str], str] = _untranslated) -> dict:
    """Verified role: set, still exists, and the bot can actually grant it.

    All three ways this can be unfinished point at the same fix -- the role
    picker in Settings, which already flags an unassignable role (see
    `read_dashboard_roles`'s `assignable`) when choosing one -- so every
    non-done state gets the same action rather than three different ones.
    """
    action = _settings_action("verified_role", t=t)
    label = t(N_("Verified role"))
    # `key` rather than the label is what `_setup_step` looks this row up by.
    # The label is translated; the key is not, and must not be.
    key = "verified_role"
    if not configured.get("verified_role"):
        return {
            "key": key,
            "label": label,
            "state": "todo",
            "note": t(N_("Required — verification can't finish without one.")),
            "action": action,
        }
    if configured.get("verified_role_exists") is False:
        return {
            "key": key,
            "label": label,
            "state": "broken",
            "note": t(N_("The role that was set has been deleted. Choose another.")),
            "action": action,
        }
    if configured.get("verified_role_assignable") is False:
        return {
            "key": key,
            "label": label,
            "state": "broken",
            "note": t(N_(
                "VRCVerify's own role needs to sit above this one to grant it."
            )),
            "action": action,
        }
    # `verified_role_assignable` of None means the hierarchy could not be
    # checked -- not proof it is broken, so this stays "done" rather than
    # crying wolf on a working server. Same restraint `read_dashboard_panel`
    # takes with a channel it cannot confirm is postable.
    return {
        "key": key,
        "label": label,
        "state": "done",
        "note": t(N_("Set, and VRCVerify can grant it.")),
        "action": None,
    }


def _panel_row(
    panel: Optional[dict], t: Callable[[str], str] = _untranslated
) -> dict:
    """Instructions panel: posted, in a channel that still exists, and one the
    bot can still post to. Mirrors `_role_row`'s three-way split for the same
    reason -- "not set up" and "set up and now broken" need different notes
    even though both need the same fix."""
    action = _settings_action("panel", t=t)
    label = t(N_("Instructions panel"))
    key = "panel"
    if not panel or not panel.get("posted"):
        return {
            "key": key,
            "label": label,
            "state": "todo",
            "note": t(N_("Members need a message to start from.")),
            "action": action,
        }
    if panel.get("channel_exists") is False:
        return {
            "key": key,
            "label": label,
            "state": "broken",
            "note": t(N_("The channel it was posted in was deleted.")),
            "action": action,
        }
    if panel.get("channel_postable") is False:
        return {
            "key": key,
            "label": label,
            "state": "broken",
            "note": t(N_(
                "VRCVerify can't post there anymore — check its permissions."
            )),
            "action": action,
        }
    return {
        "key": key,
        "label": label,
        "state": "done",
        "note": t(N_("Posted, and VRCVerify can still reach it.")),
        "action": None,
    }


def build_setup(
    overview: Optional[dict], t: Callable[[str], str] = _untranslated
) -> Optional[dict]:
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

    role = _role_row(configured, t)
    panel = _panel_row(overview.get("panel"), t)

    rows = [role, panel] + [
        {
            "key": key,
            "label": t(label),
            "state": "done" if configured.get(key) else "off",
            "note": t(note),
            "action": None,
        }
        for key, label, note in OPTIONAL_ROWS
    ]

    return {
        "rows": rows,
        "complete": role["state"] == "done" and panel["state"] == "done",
    }


def _setup_step(setup: dict, t: Callable[[str], str] = _untranslated) -> dict:
    """The single most useful setup row to surface at the top of the page,
    for whichever of the two required rows isn't done.

    Reuses `build_setup`'s own row -- state, note, and all -- rather than a
    second copy of "is the role missing" that could disagree with the list
    right below it. `state` decides only the *title*, because "todo" and
    "broken" already have distinct, accurate notes from #135 phase 3 and
    duplicating that wording here is how the two drift.
    """
    # BY `key`, NEVER BY `label` (#97). `label` is translated now, so
    # `by_label["Verified role"]` is a KeyError the moment somebody reads this
    # page in German -- and the label is the one thing about a row that is
    # guaranteed to change. `key` is the stable identifier the rows have
    # always carried for the optional three; the two required rows now carry
    # it too, for exactly this lookup.
    by_key = {row["key"]: row for row in setup["rows"]}

    role = by_key["verified_role"]
    if role["state"] != "done":
        title = (
            t(N_("No verified role is set"))
            if role["state"] == "todo"
            else t(N_("The verified role needs attention"))
        )
        # Named, not left to the template's fallback: this is the one
        # candidate whose button already knew which field it was about, and
        # after the split (#140) "Settings" is five pages, only one of which
        # has a role picker on it.
        return {
            "title": title,
            "body": role["note"],
            "action": "settings",
            "group": _SETTINGS_ANCHOR["verified_role"][0],
        }

    panel = by_key["panel"]
    title = (
        t(N_("No instructions panel is posted"))
        if panel["state"] == "todo"
        else t(N_("The instructions panel needs attention"))
    )
    return {
        "title": title,
        "body": panel["note"],
        "action": "settings",
        "group": _SETTINGS_ANCHOR["panel"][0],
    }


def _demo_step(
    overview: dict,
    t: Callable[[str], str] = _untranslated,
    ngettext: Optional[Callable] = None,
) -> Optional[dict]:
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
    if ngettext is None:
        # The English rule, for a caller that passed no catalogue: one form for
        # 1 and one for everything else.
        def ngettext(one, many, n):
            return one if n == 1 else many

    premium = overview.get("premium") or {}
    if premium.get("premium"):
        return None

    counts = overview.get("verifications") or {}
    if not counts.get("known", True):
        return None
    count = counts.get("last_30_days")
    if not isinstance(count, int) or count <= 0:
        return None

    # `ngettext`, not an English `'' if count == 1 else 's'`. Plural rules are
    # a property of the language, not of the number: Russian has three forms,
    # Japanese and Chinese have one, and Arabic has six. Appending an "s" is
    # correct for exactly one of the twelve, so the whole sentence is a plural
    # msgid and gettext picks the form from the catalogue's own rule.
    pitch = ngettext(
        "%(count)s member verified here in the last 30 days. Premium logs each "
        "one to a channel of your choice.",
        "%(count)s members verified here in the last 30 days. Premium logs each "
        "one to a channel of your choice.",
        count,
    ) % {"count": f"{count:,}"}
    if premium.get("grandfathered"):
        # Leads with what stays free, per the issue's own rule -- the model
        # is settings.html's upgrade card, which makes the same two-sentence
        # move: reassure, then offer. Truthful either way, since the activity
        # log was never one of the grandfathered extras (see
        # GRANDFATHERED_FEATURES in bot.py) -- this server really would be
        # buying something new, not being asked to pay for what it already
        # has.
        return {
            "title": t(N_("Add VRCVerify Premium")),
            "body": t(N_("Your grandfathered extras stay free whatever you decide."))
            + " "
            + pitch,
            "action": "subscription",
        }
    return {
        "title": t(N_("Upgrade to VRCVerify Premium")),
        "body": pitch,
        "action": "subscription",
    }


def build_next_step(
    overview: Optional[dict],
    changelog_entry: Optional[dict] = None,
    t: Callable[[str], str] = _untranslated,
    ngettext: Optional[Callable] = None,
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

       #140 ADDS A SILENT REQUIREMENT TO THAT SHAPE: `action: "settings"` now
       also needs a `"group"` key, one of `settings_view.SETTINGS_SLUGS`, or
       overview.html's template falls back to `"verification"` -- which is
       right for the two setup steps that use it today, both of which set it,
       but would be wrong for any future changelog entry pointing at a
       different group. Nothing enforces this yet because nothing can: #136
       has no caller. Whoever writes one has to set `group` explicitly.
    3. The data-backed demo, `_demo_step` -- suppressed for a premium server,
       and never rendered from a figure this page cannot stand behind.

    Returns None when nothing applies, which is the common case: a fully
    configured premium server with nothing new to announce should not be
    nagged about anything.
    """
    if not overview:
        return None

    setup = build_setup(overview, t)
    if setup is None:
        # The settings read failed behind the overview -- build_setup already
        # says nothing rather than guess, and repeating half its checks here
        # with the other half missing would be worse than matching it.
        return None
    if not setup["complete"]:
        return _setup_step(setup, t)

    if changelog_entry:
        return changelog_entry

    return _demo_step(overview, t, ngettext)
