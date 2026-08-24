"""What shipped, as data -- and the rules deciding where each item is allowed
to appear.

Pure, like `settings_view` and `overview_view`: no Flask, no network, no clock.
Everything arrives as an argument, so "this admin, on this browser, on this
server, has already dismissed this entry" is a function call in a test rather
than three cookies and a request.

THIS CONSTANT IS THE SOURCE OF TRUTH FOR FOUR SURFACES
------------------------------------------------------
The in-app feed (this issue), the public changelog on the marketing site
(#137), the Discord announcement channel (#138) and the update emails (#139)
all render from `ENTRIES`. None of them keeps its own list. Four lists of the
same features is four lists that disagree by the third release.

A Python constant, shipped with a deploy, rather than a table or a CMS:
version-controlled, reviewable in the same PR as the feature it announces,
testable, and needing no new infrastructure and no publishing auth model.

`id` IS PERMANENT
-----------------
It is what "already seen" and "already dismissed" are recorded against, in
cookies that outlive any deploy. Editing an entry's wording is free. Changing
its `id` re-shows the entry to every admin who already dismissed it, and there
is no way to undo that. Treat the id as immutable from the moment the PR
merges.

The convention is `YYYY-MM-<slug>` -- the month it shipped, then a short
stable name for the thing. The month is part of the id only to keep ids
unique and roughly ordered by eye; `date` is what gets displayed, and
`ENTRIES` order is what decides sequence.

TWO FLAGS, AND THEY ANSWER DIFFERENT QUESTIONS
-----------------------------------------------
* `premium` decides *how loudly it arrives*. An ordinary entry appears in the
  bell and on the changelog page and never interrupts. A premium entry does
  all of that and additionally takes the Overview `next_step` slot once per
  server, with a CTA. That routing is the whole reason this feature exists as
  something other than a nice-to-have: a bell on its own converts nothing,
  because an unread dot is the most trained-past element on the web.

* `public` decides *who is allowed to read it*. An in-app entry can address
  "you, the admin of this server". A changelog page, a Discord post and a
  marketing email are read by strangers and cannot. An entry whose wording
  only makes sense signed in sets `public=False` and stays in the feed alone.
  #137, #138 and #139 all filter on this one flag rather than each inventing
  a rule. It defaults to `True`, because most entries are fine in public and
  the ones that are not should have to say so.

`cta_endpoint` IS AN ENDPOINT NAME, NEVER A URL
------------------------------------------------
Looked up in `CTA_ACTIONS` below, which is a fixed table. This is the same
rule `set_nav_preference` and `set_theme_preference` follow for `return_to`,
stated in `base.html` as *"An endpoint NAME from a fixed table, never a path
-- a hidden field carrying a URL is how a preference toggle becomes an open
redirect."* The feed is repo data rather than user input, so the risk here is
lower; the pattern is established, costs nothing, and means nobody has to
re-derive whether it matters the next time this file grows a field.

BODIES ARE PLAIN TEXT
---------------------
No Markdown, no HTML, in the constant or anywhere downstream. Jinja escapes
what it renders, and keeping the one path from this data to the DOM boring is
worth more than a link inside a sentence. If an entry ever needs a link, give
the model a structured field for it rather than accepting markup.

EDITORIAL RULES, because the failure mode of this feature is becoming an ad
channel and getting ignored:

* Entries are about things that **shipped**. Not roadmap, not marketing.
* **A premium entry must announce something new.** "Premium exists" is not an
  entry. A release with no premium feature in it has no premium entry, and the
  feed is expected to go long stretches carrying none.
* Ordinary entries should outnumber premium ones. The bell's credibility is
  what makes a premium card land as news instead of as an advert, and it is
  spent every time an entry is not really news.
* Every entry is one more English string for #97 to catch. Keep them short.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Entry:
    """One thing that shipped.

    Frozen because these are module-level constants shared by every request in
    the process -- a surface that mutated one while rendering would change it
    for everybody else.
    """

    id: str
    date: date
    title: str
    body: str
    premium: bool = False
    # See the module docstring: `True` means "safe for strangers to read".
    public: bool = True
    # Only meaningful when `premium` is set; the Overview card is the only
    # surface that renders a CTA.
    cta_endpoint: Optional[str] = None
    cta_label: Optional[str] = None

    @property
    def tag(self) -> str:
        """The badge the bell and the changelog page put beside the title."""
        return "Premium" if self.premium else "New"


# The fixed table. A `cta_endpoint` not named here is not renderable, and
# `test_changelog.py` fails on one -- see `validate_entries`.
#
# The value is the token `overview.html` already switches on to decide which
# button to draw, so a premium entry needs no new template branch: it reuses
# the two the setup step and the data-backed demo already share.
CTA_ACTIONS = {
    "guild_subscription": "subscription",
    "guild_settings": "settings",
}

# Lowercase, digits and hyphens. Deliberately excludes `:` and `,`, which are
# the two separators the dismissal cookie is built from -- an id containing
# either would let one entry's id be read back as two, or as a guild id.
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# How many entries the bell's panel shows before deferring to the changelog
# page. A dropdown is a summary; the page is the list.
BELL_LIMIT = 5

# The dismissal cookie's ceiling, in `guild:entry` pairs. An admin managing
# several servers through several premium releases must not be able to grow
# this without limit -- a cookie is sent on every request, and a browser will
# silently drop the lot once the domain's total goes over about 4KB, which
# would take the session with it. Twenty pairs is roughly 800 bytes at the
# lengths `ID_PATTERN` and a snowflake permit, and the oldest fall off the
# front. Losing the oldest dismissal re-shows one card once, which is the
# cheapest possible failure here.
MAX_DISMISSALS = 20

_PAIR_SEPARATOR = ","
_FIELD_SEPARATOR = ":"


# WHAT SHIPPED
# ============
# Newest first. Position decides order everywhere; `date` is display only, so
# correcting a date never reshuffles the feed.
#
# The feed starts at the 2026-08 dashboard revamp and goes forward. The four
# premium features that closed on 2026-08-03 -- the log channel, the priority
# queue, scheduled re-verification and the branded embed -- are deliberately
# NOT backfilled: no admin was ever told about them, which is a real argument
# for adding them, but the bell's first impression would be a backlog rather
# than news, and dating a July feature to the month we got round to announcing
# it is the first small lie that makes the whole feed untrustworthy.
#
# The VRChat group invite is the one exception, and the line it sits on the
# right side of is REACHABILITY rather than sentiment. It became something an
# admin could actually turn on when the settings page grew controls for it on
# 2026-08-19/20, days before the revamp -- so it is the most recent thing that
# shipped, not a rediscovered one, and its entry is dated when it became
# usable rather than when the issue was filed. The other four had been
# reachable for three weeks by then.
ENTRIES = (
    Entry(
        id="2026-08-overview-trends",
        date=date(2026, 8, 24),
        title="See how verification is going",
        body=(
            "The Overview page now charts the last 30 days of verifications, "
            "and checks the things that quietly break a server -- a verified "
            "role that was deleted, a role the bot cannot assign, an "
            "instructions panel that is no longer where it was posted."
        ),
    ),
    Entry(
        id="2026-08-dashboard-redesign",
        date=date(2026, 8, 24),
        title="The dashboard has been rebuilt",
        body=(
            "A new header and sidebar, servers you can pick from cards "
            "instead of a list, switches that show you what is on at a "
            "glance, and a warning before you leave a page with unsaved "
            "changes."
        ),
    ),
    Entry(
        id="2026-08-theme-choice",
        date=date(2026, 8, 23),
        title="Dark, light, or whatever your device says",
        body=(
            "The dashboard is dark by default now, with a theme button in the "
            "header. Your choice is remembered on this browser."
        ),
    ),
    Entry(
        id="2026-08-group-invite",
        date=date(2026, 8, 20),
        title="Invite verified members to your VRChat group",
        body=(
            "Link a private VRChat group to your server, and members who "
            "finish verification are offered an invite to it. Nothing is sent "
            "to VRChat unless the member asks for it."
        ),
        premium=True,
        # Named rather than left to the default, so the entry says where its
        # button goes instead of the renderer assuming. Only the free and
        # grandfathered framings use it -- a server already on Premium is sent
        # to Settings, because it has nothing left to buy.
        cta_endpoint="guild_subscription",
        cta_label="See Premium",
    ),
)


# READING THE COOKIES
# ===================
# Both are written by the browser as well as the server, so both are hostile
# input and neither is trusted further than the checks below. Neither can be
# httponly for that reason, which is exactly the theme cookie's bargain in
# #123 and reasoned there.


def read_seen(value: Optional[str], entries=ENTRIES) -> Optional[str]:
    """The id of the newest entry this browser has already seen, or None.

    Validated against the ids actually shipped. An id we no longer recognise
    -- a hand-edited cookie, or one written by an older deploy whose entry has
    since been renamed -- is treated as having seen nothing, which shows the
    dot once more rather than hiding entries the browser never saw.
    """
    if not value:
        return None
    known = {entry.id for entry in entries}
    return value if value in known else None


def newest_id(entries=ENTRIES) -> Optional[str]:
    """The id the unread dot is compared against. None when there is no feed."""
    return entries[0].id if entries else None


def has_unread(seen_id: Optional[str], entries=ENTRIES) -> bool:
    """Whether to draw the dot.

    "Newest differs from seen" rather than a count, because a count needs an
    ordering the cookie does not carry and would be wrong the moment an entry
    is inserted anywhere but the front.
    """
    newest = newest_id(entries)
    return newest is not None and seen_id != newest


def parse_dismissed(value: Optional[str], entries=ENTRIES) -> tuple:
    """The `guild:entry` pairs this browser has dismissed, oldest first.

    Anything malformed is dropped rather than raising: this is a cookie, a
    truncated or hand-edited one is ordinary, and the cost of not
    understanding a pair is re-showing one card. Order is preserved because it
    is what `add_dismissal` evicts by.
    """
    if not value:
        return ()

    pairs = []
    known = {entry.id for entry in entries}
    for chunk in value.split(_PAIR_SEPARATOR):
        guild, _, entry_id = chunk.partition(_FIELD_SEPARATOR)
        # A guild id is a snowflake and an entry id has to be one we shipped.
        # Validating the entry id here rather than at the point of use means a
        # renamed entry stops occupying a slot in a cookie that has a ceiling.
        if guild.isdigit() and entry_id in known and (guild, entry_id) not in pairs:
            pairs.append((guild, entry_id))
    return tuple(pairs)


def is_dismissed(dismissed, guild_id, entry_id: str) -> bool:
    """Whether this server has already had this entry's card dismissed.

    Per guild, not per admin: an admin managing four servers is pitched once
    per server, which is the property that actually matters. Per browser is
    the accepted cost -- see the issue.
    """
    return (str(guild_id), entry_id) in tuple(dismissed)


def add_dismissal(dismissed, guild_id, entry_id: str) -> str:
    """The cookie value recording one more dismissal, bounded.

    Re-dismissing something already dismissed moves it to the back rather than
    duplicating it, so a card an admin keeps meeting on their most-used server
    is the last one evicted rather than a pair that accumulates.
    """
    pair = (str(guild_id), entry_id)
    pairs = [existing for existing in tuple(dismissed) if existing != pair]
    pairs.append(pair)
    pairs = pairs[-MAX_DISMISSALS:]
    return _PAIR_SEPARATOR.join(
        guild + _FIELD_SEPARATOR + eid for guild, eid in pairs
    )


# WHAT TO SHOW WHERE
# ==================


@dataclass(frozen=True)
class Bell:
    """What the header's disclosure renders, if anything."""

    entries: tuple
    unread: bool

    @property
    def show(self) -> bool:
        """No entries means no bell at all, rather than an empty dropdown."""
        return bool(self.entries)


def build_bell(seen_id: Optional[str], entries=ENTRIES) -> Bell:
    """The most recent handful, and whether any of them is new to this browser."""
    return Bell(entries=tuple(entries[:BELL_LIMIT]), unread=has_unread(seen_id, entries))


def public_entries(entries=ENTRIES) -> tuple:
    """Everything a stranger may read: the changelog page (#137), the
    announcement channel (#138) and the update emails (#139) all start here.

    One flag, one filter, three surfaces -- rather than each of those issues
    deciding for itself what "safe to publish" means.
    """
    return tuple(entry for entry in entries if entry.public)


def build_premium_card(
    guild_id,
    *,
    dismissed=(),
    premium: bool = False,
    grandfathered: bool = False,
    entries=ENTRIES,
) -> Optional[dict]:
    """The newest undismissed premium entry, phrased for this server's plan --
    or None, which is the common case.

    Returns the `{"title", "body", "action"}` shape `build_next_step()`
    already returns for its other two candidates -- plus `cta_label` and
    `entry_id`, which that function passes through untouched and only phase
    4's template reads. It asks for this shape by name: *"#136 can hand back exactly what
    should be shown without this function needing to know anything about
    changelogs."* Nothing here knows where in the ranking it lands, and
    `build_next_step` stays the one place that decides.

    THREE AUDIENCES, THREE FRAMINGS
    -------------------------------
    * **Free.** An upsell, and the only one of the three that is.
    * **Already premium.** "New in your plan, here's how to switch it on",
      pointing at Settings rather than Subscriptions. Retention, not upsell --
      and it stops the feed nagging a paying customer about something they
      have already bought, which is the fastest way to make a bell get muted.
    * **Grandfathered.** Leads with what stays free, then offers. The sentence
      is deliberately the same one `_demo_step` uses, because a server that
      sees both over time should hear one product speaking, and because the
      rule it encodes -- never imply a grandfathered server could lose what it
      has -- is easier to keep when there is one string to check.
    """
    candidates = [entry for entry in entries if entry.premium]
    for entry in candidates:
        if is_dismissed(dismissed, guild_id, entry.id):
            continue

        if premium:
            return {
                "title": f"New in your plan: {entry.title}",
                "body": f"{entry.body} It's included in your subscription.",
                "action": "settings",
                # Not the entry's own label: that one sells, and this server
                # has already bought. The button goes to the thing itself.
                "cta_label": "Set it up",
                "entry_id": entry.id,
            }

        # Both remaining cases sell, so both resolve their button through the
        # fixed table rather than assuming Subscriptions -- an entry may point
        # somewhere else, and the endpoint name is the only thing it is
        # allowed to say about that.
        action = CTA_ACTIONS.get(entry.cta_endpoint or "guild_subscription")
        if action is None:
            # An entry naming an endpoint we do not render is a bug, caught by
            # `validate_entries` in CI. At render time it degrades to showing
            # nothing rather than to a broken button.
            continue

        body = entry.body
        if grandfathered:
            body = f"Your grandfathered extras stay free whatever you decide. {body}"
        return {
            "title": f"New in Premium: {entry.title}",
            "body": body,
            "action": action,
            "cta_label": entry.cta_label or "See Premium",
            "entry_id": entry.id,
        }
    return None


def validate_entries(entries=ENTRIES) -> list:
    """Everything wrong with the constant, as a list of English strings.

    Called from `test_changelog.py` rather than at import time on purpose. A
    malformed entry is a mistake worth blocking a deploy over, and CI is where
    that block should happen -- asserting here would take the entire dashboard
    down at import for a typo in a cosmetic feature, which is a far worse
    trade than a red build.
    """
    problems = []
    seen_ids = set()
    for entry in entries:
        where = f"entry {entry.id!r}"
        if not ID_PATTERN.match(entry.id):
            problems.append(
                f"{where}: id must be lowercase letters, digits and hyphens "
                "-- see ID_PATTERN, which excludes the cookie's separators"
            )
        if entry.id in seen_ids:
            problems.append(f"{where}: duplicate id; ids are the dismissal key")
        seen_ids.add(entry.id)

        for field, text in (("title", entry.title), ("body", entry.body)):
            if "<" in text or "&" in text:
                problems.append(
                    f"{where}: {field} contains markup. Bodies are plain text "
                    "-- see the module docstring."
                )

        if entry.cta_endpoint is not None and entry.cta_endpoint not in CTA_ACTIONS:
            problems.append(
                f"{where}: cta_endpoint {entry.cta_endpoint!r} is not in "
                "CTA_ACTIONS. It is an endpoint name from a fixed table, "
                "never a URL."
            )
        if entry.cta_endpoint and not entry.premium:
            problems.append(
                f"{where}: a CTA on a non-premium entry has nowhere to render "
                "-- only the Overview card draws one."
            )

    dates = [entry.date for entry in entries]
    if dates != sorted(dates, reverse=True):
        problems.append(
            "ENTRIES is not newest-first. Position decides order everywhere, "
            "so an out-of-order date means the feed reads wrong."
        )
    return problems
