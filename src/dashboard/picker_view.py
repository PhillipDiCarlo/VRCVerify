"""What each card on the server picker says about its server.

Pure, like the other view modules: no Flask, no network, no clock. The summary
payload arrives as an argument, so "this server is installed but nobody ever
picked a verified role" is a function call in a test rather than a rendered
page somebody has to read.

FIVE CARD STATES, AND THE FIRST TWO ARE NOT ABOUT SETUP AT ALL
--------------------------------------------------------------

* **unknown** -- the bot could not be asked. Every card, not one of them.
  Offers nothing, because an install button here is an invitation to reinstall
  something that is already working. `picker.html` has the full reasoning.

* **absent** -- the caller has no standing in this server. TWO ANSWERS WEARING
  ONE NAME, on purpose: the bot is not there, or it is and this person does not
  administer it. The summary endpoint cannot tell them apart and must not, so
  neither can this. See `handle_guild_summaries` in `bot_api.py`.

The remaining three are the ones this module was added for (#164). They are the
same distinction the Overview's setup list draws, at the width of a card:

* **todo** -- installed, and something required was never configured. Invisible
  before this: a server with no verified role rendered exactly like a working
  one, and the only way to find out was to click into each in turn.

* **broken** -- configured and not working. A role that was deleted, a role the
  bot cannot grant, a panel channel it can no longer post in. The most valuable
  of the three, because it is the one nothing else on any screen surfaces: the
  server looked finished the last time anyone checked, and then quietly stopped.

* **done** -- set up, and working.

WHY THE STATES ARE DERIVED AND NOT SENT
---------------------------------------
The bot returns the same `configured` and `panel` blocks the Overview consumes,
and this runs `build_setup` over them rather than reading a verdict the bot
computed. That is what stops the picker and the Overview disagreeing about
whether a server is finished, which is an acceptance criterion of #164 rather
than a nicety: two implementations of "is this wired up" is two answers that
diverge the first time a required step is added on one side only.

For the same reason the required rows come from `build_setup`'s own `required`
tuple instead of a list kept here. Adding a third required step should change
what the cards say without anybody remembering this file exists.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

from dashboard.i18n import N_
from dashboard.overview_view import build_setup


def _untranslated(text: str) -> str:
    return text


# How many gradients a server can be drawn in. Twelve, and the number is a
# judgement rather than a constraint: with a handful of servers on screen a
# repeat is going to happen, and the question is only whether it reads as two
# servers sharing a look or as the page failing to tell them apart. Twelve
# well-separated hues collide often enough to be obviously a palette.
TILE_COUNT = 12


def tile_class(guild_id) -> str:
    """Which of the twelve gradients this server is drawn in.

    DERIVED, NEVER STORED. A column would have to be written at join time,
    backfilled for every existing guild, and kept in step with a palette that
    is a design decision -- for something whose only job is to make one square
    look unlike the square beside it.

    HASHED RATHER THAN `id % 12`, AND THE DIFFERENCE IS NOT SUBTLE. A Discord
    snowflake is `(ms << 22) | (worker << 17) | (process << 12) | increment`,
    where the increment counts events within one process-millisecond -- so for
    something as rare as creating a guild it is almost always 0. That makes the
    whole id a multiple of 4096, and 4096 is congruent to 4 modulo 12, so
    `id % 12` can only ever return 0, 4 or 8.

    A twelve-colour palette would have rendered in three colours. Measured, not
    reasoned about: 500 realistic snowflakes with increment 0 yield exactly
    {0, 4, 8}, and the same 500 through sha256 use all twelve. There is a test.

    The bug this avoids is the quiet kind -- nothing errors, the tiles are
    simply far more alike than the palette says they should be, and it reads as
    the page failing to tell servers apart rather than as an arithmetic fault.

    THE PALETTE IS ROTATED OFF THE ACCENT ON PURPOSE -- see style.css. Nothing
    here needs to know that; this returns a name, and the stylesheet owns what
    the name looks like.
    """
    digest = hashlib.sha256(str(guild_id).encode("utf-8")).digest()
    return f"g{digest[0] % TILE_COUNT}"


# What the slot says, per state. A card carries exactly one of these, and the
# absent and unknown wordings are the ones the page already shipped -- they are
# here so that all five live together rather than three in Python and two in
# the template.
_NOTES = {
    "done": N_("Set up and working"),
    "todo": N_("Setup isn't finished"),
    "broken": N_("Something isn't working"),
    "absent": N_("Not set up here, or not yours to manage"),
    "unknown": N_("Can't check this server right now"),
}


def card_state(summary: Optional[dict]) -> str:
    """One of done / todo / broken, for a server the caller administers.

    `broken` outranks `todo`, which is the opposite of how the Overview orders
    its list and right for a single line: the list has room to say "this one is
    missing and that one is broken", and a card has to pick the more urgent.
    A server that was working and stopped is losing verifications right now; a
    server that was never finished has not started.

    `unknown` when the summary is unreadable. `build_setup` returns None when
    the configuration could not be read, and a card must not turn that into
    "working" -- saying nothing is the honest answer to a question nobody
    managed to ask.
    """
    setup = build_setup(summary)
    if setup is None:
        return "unknown"

    required = [row for row in setup["rows"] if row["key"] in setup["required"]]
    if any(row["state"] == "broken" for row in required):
        return "broken"
    if any(row["state"] != "done" for row in required):
        return "todo"
    return "done"


def build_cards(
    servers: list,
    summaries: Optional[dict],
    *,
    reachable: bool,
    t: Callable[[str], str] = _untranslated,
) -> list:
    """One entry per card, in the order they should be drawn.

    `summaries` is keyed by guild id and holds ONLY the servers this caller
    administers. A guild missing from it is `absent`, and that absence carries
    the deliberate ambiguity the endpoint is built to preserve: the bot is not
    there, or it is and this person does not administer it.
    None means the call failed outright, which is `unknown` for every card.

    Installed first, then alphabetical. The ones that can actually be
    configured are the reason somebody came to this page. Within the installed
    group the state does NOT reorder anything: a card moving because a role was
    deleted would shuffle the grid under an admin who is trying to find one
    server, and the state is already visible on the card itself.
    """
    cards = []
    for server in servers:
        summary = None if summaries is None else summaries.get(str(server["id"]))
        if not reachable:
            state = "unknown"
        elif summary is None:
            state = "absent"
        else:
            state = card_state(summary)
        cards.append({
            **server,
            "state": state,
            # `installed` stays a separate boolean rather than being inferred
            # from the state, because "unknown" is not a claim about presence
            # and the template must not be able to read one out of it.
            "installed": state not in {"absent", "unknown"},
            "note": t(_NOTES[state]),
            # Drawn on every card, including the ones carrying a Discord icon.
            # The icon covers the gradient rather than replacing it, so a
            # server whose icon fails to load falls back to its own colour
            # instead of to a grey hole.
            "tile": tile_class(server["id"]),
        })

    cards.sort(key=lambda card: (not card["installed"], card["name"].lower()))
    return cards
