"""Canned answers standing in for the bot, so the preview can render pages.

Used only by scripts/dev_dashboard.py. See issue #149.

WHY THIS EXISTS
---------------
`dev_dashboard.py` could previously serve the sign-in page and nothing else,
because every page past it needs a session and a reachable bot. That was fine
while the only thing being restyled was the theme picker in the header, which
renders signed out. It stops being fine for the sidebar, the server picker, the
settings switches, the overview and the error page -- none of which could be
looked at in a browser before merging.

`create_app()` already takes a `client=`, so nothing in the application changes
to make this work. This is injected from the outside, exactly as the tests do.

THIS MAKES THE PREVIEW *LESS* ABLE TO REACH PRODUCTION, NOT MORE
----------------------------------------------------------------
Worth being explicit, because "give the dev tool some data" is the shape of
change that usually goes the other way. `BOT_API_URL` points at a closed port
so a stray call fails immediately; with this in place, no call is attempted at
all. Nothing here opens a socket. The preview still never reads `.env`.

The one risk that does go up is mistaking canned output for real output, so
every server below is named "Preview:" something and every number is a round
one. If you are ever unsure whether you are looking at real data, the answer is
no -- this file is the only thing that has ever answered a question on
127.0.0.1:5001.

WHY IT IMPORTS FROM tests/
--------------------------
`make_settings()` and `make_overview()` build payloads shaped exactly as
`read_dashboard_settings` and `read_dashboard_overview` return, and the
contract tests in `tests/test_dashboard.py` hold them to it. Writing a second
set here would mean a second thing to keep in step with the bot, with nothing
watching it drift -- and a preview that lies about the shape of the data is
worse than no preview, because the work done against it looks finished.

The cost is that this dev-only script imports a test module, which is unusual.
It is the cheaper of the two mistakes.
"""

from __future__ import annotations

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO / "src", REPO / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from dashboard.botapi import BotAPIError  # noqa: E402

try:
    from test_dashboard import (  # noqa: E402
        DEFAULT_CHANNELS,
        DEFAULT_ROLES,
        LOG_CHANNEL,
        WRITABLE,
        make_overview,
        make_settings,
    )
except ImportError as missing:  # pragma: no cover - a dev-tool setup problem
    # The test module needs pytest at import time. Somebody running the
    # preview from a bot-only environment would otherwise get a traceback
    # about `pytest` from a file that does not obviously use it.
    raise SystemExit(
        f"\n  The signed-in preview needs the test dependencies ({missing}).\n\n"
        "    pip install -r config/other_configs/requirements-dev.txt\n\n"
        "  Or run the signed-out preview, which needs none of this:\n\n"
        "    PREVIEW_SIGNED_IN=0 python scripts/dev_dashboard.py\n"
    ) from missing

# A bot that answers nothing, for the states an outage produces.
#
# Not the same as the one unreachable server below: that one is a single guild
# refusing while the bot is otherwise fine. This is `admin_guild_ids` itself
# failing, which is what makes the picker unable to say which servers the bot
# is in -- and #133 phase 3 turned that into its own "unknown" card state
# rather than showing everything as un-installed. There is no way to look at
# those cards without being able to break this call.
BOT_DOWN = os.environ.get("PREVIEW_BOT_DOWN") == "1"


def _outage() -> BotAPIError:
    return BotAPIError("preview: the whole bot is pretending to be down", status=503)


# Who the preview is signed in as. Matches the tests' actor so anything copied
# between the two lines up.
ACTOR = "424242424242"

# Four servers, chosen to cover the states that are hard to reach and easy to
# get wrong rather than to look like somebody's real Discord.
PREMIUM = "700000000000000001"
FREE = "700000000000000002"
NOT_ADDED = "700000000000000003"
UNREACHABLE = "700000000000000004"

# `icon: None` throughout, deliberately: an icon hash would build a
# cdn.discordapp.com URL for a file that does not exist, and a page full of
# broken images is a worse preview than one full of initials.
GUILDS = [
    {"id": PREMIUM, "name": "Preview: premium server", "icon": None, "admin_hint": True},
    {"id": FREE, "name": "Preview: free server", "icon": None, "admin_hint": True},
    {"id": NOT_ADDED, "name": "Preview: bot not added", "icon": None, "admin_hint": True},
    {
        "id": UNREACHABLE,
        "name": "Preview: bot unreachable",
        "icon": None,
        "admin_hint": True,
    },
]

# What `admin_guild_ids` answers with: which servers the bot is in. NOT_ADDED
# is absent on purpose -- it is the un-installed card on the picker, which #133
# phase 3 rebuilds around card content rather than colour. UNREACHABLE *is*
# installed, so clicking into it reaches error.html by the path a real outage
# would take.
INSTALLED = {PREMIUM, FREE, UNREACHABLE}

# One field held back from `writable`, so the read-only switch state can be
# seen at all.
#
# It cannot be seen anywhere else: `bot.DASHBOARD_WRITABLE_FIELDS` currently
# contains every declared setting, so a not-`writable` field does not occur in
# production today. #133 phase 4 still has to render it distinctly from
# `locked` -- the two are non-interactive for entirely different reasons and
# need different copy -- and this is the only place that can be checked.
WITHHELD = "custom_verification_requested_message"

PREVIEW_AUDIT = [
    {
        "actor_id": ACTOR,
        "actor_name": "Preview Admin",
        "field": "role_id",
        "old_value": None,
        "new_value": DEFAULT_ROLES[0]["id"],
        "changed_at": "2026-08-20T14:02:00+00:00",
    },
    {
        "actor_id": ACTOR,
        "actor_name": "Preview Admin",
        "field": "auto_verify_new_members",
        "old_value": False,
        "new_value": True,
        "changed_at": "2026-08-19T09:30:00+00:00",
    },
]


class PreviewBotAPI:
    """The same method surface as BotAPIClient, answering from memory.

    Saves are kept in `_saved_values` so a switch you flip stays flipped until
    the process restarts. That matters more than it sounds for #133 phase 4:
    the whole hazard there is a control that looks like it saved and did not,
    and a preview where nothing ever persists cannot show the difference
    between working and broken.
    """

    def __init__(self) -> None:
        self._saved_values: dict[str, dict] = {}

    # --- reads ---

    def healthz(self) -> dict:
        if BOT_DOWN:
            raise _outage()
        return {"ok": True}

    def admin_guild_ids(self, actor_id, guild_ids) -> set:
        if BOT_DOWN:
            raise _outage()
        return {g for g in map(str, guild_ids) if g in INSTALLED}

    def settings(self, actor_id, guild_id) -> dict:
        guild_id = self._check(guild_id)
        premium = guild_id == PREMIUM
        payload = make_settings(
            premium=premium,
            values=self._saved_values.get(guild_id),
            writable=WRITABLE if premium else WRITABLE - {WITHHELD},
        )
        payload["guild_id"] = guild_id
        return payload

    def overview(self, actor_id, guild_id) -> dict:
        guild_id = self._check(guild_id)
        payload = make_overview(premium=guild_id == PREMIUM)
        payload["guild_id"] = guild_id
        return payload

    def roles(self, actor_id, guild_id) -> list:
        self._check(guild_id)
        return DEFAULT_ROLES

    def channels(self, actor_id, guild_id) -> list:
        self._check(guild_id)
        return DEFAULT_CHANNELS

    def panel(self, actor_id, guild_id) -> dict:
        guild_id = self._check(guild_id)
        if guild_id != PREMIUM:
            # The never-posted state, which is what a new server looks like.
            return {"posted": False}
        return {
            "posted": True,
            "channel_id": LOG_CHANNEL,
            "message_id": "999999999999999999",
            "channel_name": "verify-log",
            "channel_exists": True,
            "channel_postable": True,
        }

    def audit(self, actor_id, guild_id) -> list:
        guild_id = self._check(guild_id)
        # Empty for the free server, so the "no changes yet" state is reachable
        # without editing this file.
        return PREVIEW_AUDIT if guild_id == PREMIUM else []

    # --- writes ---

    def update_settings(self, actor_id, guild_id, changes: dict) -> dict:
        guild_id = self._check(guild_id)
        self._saved_values.setdefault(guild_id, {}).update(changes)
        return self.settings(actor_id, guild_id)

    def post_panel(self, actor_id, guild_id, channel_id) -> dict:
        self._check(guild_id)
        return {"action": "posted", "channel_id": str(channel_id)}

    def verify_group(self, actor_id, guild_id) -> dict:
        guild_id = self._check(guild_id)
        return {"guild_id": guild_id, "group_invite": {"state": "checking"}}

    def put_stripe_subscription(self, guild_id, subscription: dict) -> dict:
        return {"guild_id": str(guild_id), "applied": True}

    # --- the two servers that answer with nothing ---

    def _check(self, guild_id) -> str:
        """Every read and write goes through here.

        Both failing servers refuse on every endpoint rather than on the one
        somebody remembered to break, because a page that half-loads is not
        the state either of them is meant to demonstrate.

        They produce two DIFFERENT pages, and both need to look right --
        `error.html` is reached when the bot is down, which is the worst
        possible moment for a page that looks broken.

        404 gets "that server isn't available", which deliberately conflates
        "the bot is not in it" with "you do not administer it": the bot tells
        those apart and the web must not, or a signed-in user could walk guild
        ids and build a census of communities running 18+ gating. 403 collapses
        into the same page for that reason.

        503 is kept separate on purpose, and is not a leak: "the bot cannot
        answer right now" says nothing about any particular server, and telling
        an admin to try again beats telling them their server does not exist.
        See `_guild_page_unavailable`.
        """
        if BOT_DOWN:
            raise _outage()
        guild_id = str(guild_id)
        if guild_id == NOT_ADDED:
            raise BotAPIError("preview: the bot is not in this server", status=404)
        if guild_id == UNREACHABLE:
            raise BotAPIError("preview: this server is pretending to be down", status=503)
        return guild_id
