"""The local preview's bot stub must keep answering everything the app asks.

`scripts/preview_bot.py` is a dev tool and gets no test coverage for its
contents -- what it returns is invented and only has to look plausible. One
thing about it is worth pinning, though: it stands in for `BotAPIClient`, and
if the real client grows a method the dashboard calls, the preview starts
failing with an `AttributeError` deep inside a request.

That failure is cheap to prevent and expensive to meet: it appears the next
time somebody opens the preview to look at a page, which is exactly when they
are trying to think about something else.
"""

import os
import pathlib
import sys

import pytest

pytest.importorskip("flask")

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from dashboard.botapi import BotAPIClient, BotAPIError  # noqa: E402
from preview_bot import (  # noqa: E402
    FREE,
    GUILDS,
    INSTALLED,
    NOT_ADDED,
    PREMIUM,
    UNREACHABLE,
    PreviewBotAPI,
)


def public_methods(cls) -> set:
    return {
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name))
    }


def test_the_stub_answers_everything_the_real_client_does():
    """The one thing that can silently break the preview."""
    missing = public_methods(BotAPIClient) - public_methods(PreviewBotAPI)
    assert not missing, f"preview_bot.PreviewBotAPI is missing: {sorted(missing)}"


def test_the_stub_has_not_grown_methods_the_client_lacks():
    """The other direction, which is a subtler kind of wrong: a stub answering
    a call the real client cannot means work done against the preview that
    cannot possibly work in production."""
    extra = public_methods(PreviewBotAPI) - public_methods(BotAPIClient)
    assert not extra, f"PreviewBotAPI answers what the bot cannot: {sorted(extra)}"


def test_every_preview_server_is_obviously_invented():
    """The one risk the stub adds is mistaking canned output for real output.

    Names carry the warning, because the name is what is on screen.
    """
    for guild in GUILDS:
        assert guild["name"].startswith("Preview:"), guild["name"]


def test_the_two_failing_servers_fail_for_different_reasons():
    """They are not interchangeable, and the difference is the point.

    "Bot not added" must be absent from `admin_guild_ids`, because that is
    what the picker draws its un-installed card from. "Bot unreachable" must
    be present: a server the bot is in but cannot answer for is an outage,
    which is a real production state and the one `error.html` exists for.
    """
    stub = PreviewBotAPI()
    installed = stub.admin_guild_ids(1, [g["id"] for g in GUILDS])

    assert installed == INSTALLED
    assert NOT_ADDED not in installed
    assert UNREACHABLE in installed

    # Both refuse every read, by different statuses that render differently.
    for guild_id, status in ((NOT_ADDED, 404), (UNREACHABLE, 503)):
        with pytest.raises(BotAPIError) as refusal:
            stub.settings(1, guild_id)
        assert refusal.value.status == status

    # And the ones that are genuinely usable answer.
    for guild_id in (PREMIUM, FREE):
        assert stub.settings(1, guild_id)["guild_id"] == guild_id


def test_the_preview_never_reads_the_real_environment():
    """The guarantee dev_dashboard.py has carried since it was written, now
    with more moving parts behind it. A stub cannot dial out, but a config read
    from a real .env could still point the *store* somewhere real."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    assert "dotenv" not in source
    assert "load_dotenv" not in source
    # `.env` may be discussed in the docstring; it must not be opened.
    assert 'open(".env"' not in source and "open('.env'" not in source


def test_the_preview_binds_only_to_loopback():
    """It runs on laptops on untrusted networks, and it is now signed in by
    default -- so a bind to 0.0.0.0 would put a session-carrying dashboard on
    the coffee shop wifi."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    assert 'HOST = "127.0.0.1"' in source
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "0.0.0.0" not in code


def test_the_stub_remembers_what_was_saved():
    """A preview where nothing persists cannot show the difference between a
    switch that saved and one that only looked like it -- which is the whole
    hazard #133 phase 4 is about."""
    stub = PreviewBotAPI()
    guild_id = sorted(INSTALLED)[0]

    before = stub.settings(1, guild_id)["fields"]["auto_verify_new_members"]["value"]
    stub.update_settings(1, guild_id, {"auto_verify_new_members": not before})
    after = stub.settings(1, guild_id)["fields"]["auto_verify_new_members"]["value"]

    assert after is not before


def test_the_launch_config_offers_every_preview_state():
    """Three, because three states of the app cannot be reached from one
    another: signed in (most of the work), signed out (#134 redesigns that
    page), and the bot refusing everything (the picker's unknown cards and
    error.html, which have no other local route)."""
    import json
    import re

    raw = (REPO / ".vscode" / "launch.json").read_text(encoding="utf-8")
    configs = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))["configurations"]
    previews = [
        c.get("env", {})
        for c in configs
        if c.get("program", "").endswith("dev_dashboard.py")
    ]

    assert {} in previews
    assert {"PREVIEW_SIGNED_IN": "0"} in previews
    assert {"PREVIEW_BOT_DOWN": "1"} in previews
    assert len(previews) == 3


def test_the_scratch_session_file_is_not_inside_the_repo():
    """It holds an authenticated session now. Committing one would be
    harmless -- the credentials are fake -- and still a bad habit to leave
    lying where a real one could follow it."""
    source = (REPO / "scripts" / "dev_dashboard.py").read_text(encoding="utf-8")
    line = next(
        one for one in source.splitlines() if "SESSION_DB_PATH" in one
    )
    path = line.split("=", 1)[1].strip().strip('",')
    assert os.path.isabs(path)
    assert not path.startswith(str(REPO))
