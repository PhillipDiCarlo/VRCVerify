"""Unit tests for the what's-new feed's data and routing rules (issue #136,
phase 1).

Everything here is pure: `changelog.py` takes entries, seen-state and a plan
and returns what to show where, so all of it is a function call. No Flask, no
request, no cookie jar -- those arrive in phases 2 and 4, and the rules they
enforce are already pinned here.

WHY MOST OF THIS USES FIXTURE ENTRIES RATHER THAN `ENTRIES`
------------------------------------------------------------
There are deliberately **no premium entries shipping today**: the feed starts
at the 2026-08 dashboard revamp and everything in that revamp is free. The
module's own editorial rule says a release with no premium feature in it has
no premium entry, so the shipped constant is correct as it stands and will
gain its first premium entry when the next premium feature does.

That leaves the premium routing -- the part this whole feature exists for --
with nothing real to route. So it is built and tested against fixtures here,
complete and correct before there is anything to put through it. The tests
that DO assert on `ENTRIES` are the ones that must hold whatever it contains:
that it is well-formed, ordered, and free of markup.
"""

from datetime import date

import pytest

pytest.importorskip("flask")

from dashboard import changelog  # noqa: E402
from dashboard.changelog import Entry  # noqa: E402
from dashboard.overview_view import build_next_step  # noqa: E402

GUILD = "111111111111111111"
OTHER_GUILD = "222222222222222222"


def entry(entry_id="2026-09-thing", premium=False, **overrides):
    fields = dict(
        id=entry_id,
        date=date(2026, 9, 1),
        title="A thing",
        body="It does a thing.",
        premium=premium,
    )
    fields.update(overrides)
    return Entry(**fields)


PREMIUM = entry("2026-09-log-channel", premium=True, title="Log channel",
                body="Every verification, written to a channel you choose.")
ORDINARY = entry("2026-09-faster", title="Faster", body="It is faster now.")


class TestTheShippedConstant:
    """The checks that must hold whatever `ENTRIES` grows to contain."""

    def test_it_is_well_formed(self):
        # One assertion covering ids, ordering, markup and CTA endpoints,
        # because `validate_entries` reports every problem at once rather than
        # failing on the first -- which is what you want when adding three
        # entries in one PR.
        assert changelog.validate_entries() == []

    def test_ids_are_unique_because_they_are_the_dismissal_key(self):
        ids = [item.id for item in changelog.ENTRIES]
        assert len(ids) == len(set(ids))

    def test_no_body_carries_markup(self):
        # The one path from this data to the DOM stays boring. Jinja escapes
        # what it renders, so markup here would show as literal angle brackets
        # rather than as a hole -- but an entry containing any is a sign
        # somebody assumed it would be interpreted.
        for item in changelog.ENTRIES:
            assert "<" not in item.body and "<" not in item.title

    def test_every_entry_is_public_by_default(self):
        # Nothing shipping today is signed-in-only. This is not a rule the
        # module enforces -- it is a check that nobody set `public=False`
        # without meaning to, since #137, #138 and #139 all silently drop such
        # an entry.
        assert all(item.public for item in changelog.ENTRIES)

    def test_there_are_no_premium_entries_yet_and_that_is_correct(self):
        # Pinning the editorial rule, not the current contents. If a premium
        # entry is added, this test is the one that should be deleted -- and
        # deleting it should be a deliberate act, because it is also the
        # moment the Overview card starts appearing for every free server.
        assert [item for item in changelog.ENTRIES if item.premium] == []


class TestTheUnreadDot:
    def test_a_browser_that_has_seen_nothing_has_unread(self):
        assert changelog.has_unread(None, (ORDINARY,)) is True

    def test_seeing_the_newest_clears_it(self):
        assert changelog.has_unread(ORDINARY.id, (ORDINARY, PREMIUM)) is False

    def test_seeing_an_older_one_does_not(self):
        assert changelog.has_unread(PREMIUM.id, (ORDINARY, PREMIUM)) is True

    def test_an_empty_feed_has_nothing_unread(self):
        assert changelog.has_unread(None, ()) is False

    def test_an_unrecognised_cookie_value_is_treated_as_seen_nothing(self):
        # A hand-edited cookie, or one written by a deploy whose entry has
        # since been renamed. Showing the dot once more is the right failure:
        # the alternative is trusting the value and hiding entries this
        # browser has never seen.
        assert changelog.read_seen("not-an-entry", (ORDINARY,)) is None
        assert changelog.read_seen("", (ORDINARY,)) is None
        assert changelog.read_seen(None, (ORDINARY,)) is None

    def test_a_recognised_value_survives(self):
        assert changelog.read_seen(ORDINARY.id, (ORDINARY,)) == ORDINARY.id


class TestTheBell:
    def test_an_empty_feed_renders_no_bell_at_all(self):
        # Rather than an empty dropdown, which is a control that does nothing
        # and is worse than an absent one.
        assert changelog.build_bell(None, ()).show is False

    def test_it_shows_the_most_recent_handful(self):
        many = tuple(entry(f"2026-09-item-{n}") for n in range(9))
        bell = changelog.build_bell(None, many)
        assert len(bell.entries) == changelog.BELL_LIMIT
        assert bell.entries[0] is many[0]

    def test_it_carries_the_unread_state(self):
        assert changelog.build_bell(None, (ORDINARY,)).unread is True
        assert changelog.build_bell(ORDINARY.id, (ORDINARY,)).unread is False

    def test_entries_carry_the_tag_the_panel_renders(self):
        assert ORDINARY.tag == "New"
        assert PREMIUM.tag == "Premium"


class TestThePublicFlag:
    """One flag, one filter, three downstream surfaces (#137, #138, #139)."""

    def test_public_entries_are_included(self):
        assert changelog.public_entries((ORDINARY,)) == (ORDINARY,)

    def test_a_signed_in_only_entry_is_withheld(self):
        private = entry("2026-09-your-server", public=False)
        assert changelog.public_entries((ORDINARY, private)) == (ORDINARY,)

    def test_the_default_is_public(self):
        assert entry().public is True


class TestTheDismissalCookie:
    def test_a_dismissal_is_per_guild(self):
        # The property that actually matters: an admin managing four servers
        # is pitched once per server, not once in total.
        value = changelog.add_dismissal((), GUILD, PREMIUM.id)
        dismissed = changelog.parse_dismissed(value, (PREMIUM,))
        assert changelog.is_dismissed(dismissed, GUILD, PREMIUM.id) is True
        assert changelog.is_dismissed(dismissed, OTHER_GUILD, PREMIUM.id) is False

    def test_a_dismissal_is_per_entry(self):
        dismissed = changelog.parse_dismissed(
            changelog.add_dismissal((), GUILD, PREMIUM.id), (PREMIUM, ORDINARY)
        )
        assert changelog.is_dismissed(dismissed, GUILD, ORDINARY.id) is False

    def test_it_round_trips(self):
        value = changelog.add_dismissal((), GUILD, changelog.ENTRIES[0].id)
        value = changelog.add_dismissal(
            changelog.parse_dismissed(value), OTHER_GUILD, changelog.ENTRIES[0].id
        )
        dismissed = changelog.parse_dismissed(value)
        assert len(dismissed) == 2
        assert changelog.is_dismissed(dismissed, GUILD, changelog.ENTRIES[0].id)
        assert changelog.is_dismissed(dismissed, OTHER_GUILD, changelog.ENTRIES[0].id)

    def test_it_is_bounded_and_drops_the_oldest(self):
        # A cookie is sent on every request and the browser silently drops the
        # lot once the domain goes over about 4KB -- which would take the
        # session with it. Losing the oldest dismissal re-shows one card once.
        real = changelog.ENTRIES[0].id
        value = ""
        for n in range(changelog.MAX_DISMISSALS + 5):
            value = changelog.add_dismissal(
                changelog.parse_dismissed(value), f"{100000000000000000 + n}", real
            )
        dismissed = changelog.parse_dismissed(value)
        assert len(dismissed) == changelog.MAX_DISMISSALS
        # The first five guilds fell off the front; the last one is still there.
        assert changelog.is_dismissed(dismissed, "100000000000000000", real) is False
        assert changelog.is_dismissed(
            dismissed, f"{100000000000000000 + changelog.MAX_DISMISSALS + 4}", real
        ) is True

    def test_re_dismissing_moves_to_the_back_rather_than_duplicating(self):
        first, second = changelog.ENTRIES[0].id, changelog.ENTRIES[1].id
        value = changelog.add_dismissal((), GUILD, first)
        value = changelog.add_dismissal(changelog.parse_dismissed(value), GUILD, second)
        value = changelog.add_dismissal(changelog.parse_dismissed(value), GUILD, first)
        dismissed = changelog.parse_dismissed(value)
        assert dismissed == ((GUILD, second), (GUILD, first))

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "garbage",
            "notaguild:2026-08-theme-choice",
            f"{GUILD}:no-such-entry",       # renamed or removed entry
            f"{GUILD}",                     # truncated
            f":{GUILD}",
            "<script>",
        ],
    )
    def test_malformed_values_are_dropped_rather_than_raising(self, value):
        # This is a cookie. A truncated or hand-edited one is ordinary, and
        # the cost of not understanding a pair is re-showing one card.
        assert changelog.parse_dismissed(value) == ()

    def test_a_renamed_entry_stops_occupying_a_slot(self):
        # Validating the entry id on read rather than at the point of use is
        # what keeps a bounded cookie from filling with ids nothing will ever
        # match again.
        real = changelog.ENTRIES[0].id
        mixed = f"{GUILD}:gone-in-a-later-deploy,{GUILD}:{real}"
        assert changelog.parse_dismissed(mixed) == ((GUILD, real),)

    def test_ids_cannot_contain_the_cookie_separators(self):
        # The reason ID_PATTERN excludes `:` and `,`: without that, one
        # entry's id could be read back as two pairs, or as a guild id.
        assert not changelog.ID_PATTERN.match("a:b")
        assert not changelog.ID_PATTERN.match("a,b")
        for item in changelog.ENTRIES:
            assert changelog.ID_PATTERN.match(item.id)


class TestThePremiumCard:
    def test_nothing_to_announce_means_no_card(self):
        assert changelog.build_premium_card(GUILD, entries=(ORDINARY,)) is None

    def test_an_ordinary_entry_never_reaches_the_overview(self):
        # The routing decision the whole issue turns on: ordinary entries make
        # the product feel maintained, and never interrupt.
        assert (
            changelog.build_premium_card(GUILD, entries=(ORDINARY, ORDINARY))
            is None
        )

    def test_a_free_server_is_pitched(self):
        card = changelog.build_premium_card(GUILD, entries=(PREMIUM,))
        assert card["title"] == "New in Premium: Log channel"
        assert card["action"] == "subscription"
        assert card["entry_id"] == PREMIUM.id

    def test_a_premium_server_is_told_how_to_switch_it_on(self):
        # Retention, not upsell -- and it points at Settings, because there is
        # nothing left to sell this server.
        card = changelog.build_premium_card(GUILD, premium=True, entries=(PREMIUM,))
        assert card["title"] == "New in your plan: Log channel"
        assert card["action"] == "settings"
        assert "subscription" not in card["body"].lower() or "included" in card["body"]

    def test_a_premium_server_is_never_upsold(self):
        card = changelog.build_premium_card(GUILD, premium=True, entries=(PREMIUM,))
        lowered = (card["title"] + " " + card["body"]).lower()
        assert "upgrade" not in lowered
        assert "new in premium" not in lowered

    def test_a_grandfathered_server_is_told_what_it_keeps_first(self):
        # The rule from #59: never copy implying a grandfathered server could
        # lose what it has. Same sentence `_demo_step` uses, deliberately.
        card = changelog.build_premium_card(
            GUILD, grandfathered=True, entries=(PREMIUM,)
        )
        assert card["body"].startswith(
            "Your grandfathered extras stay free whatever you decide."
        )
        lowered = card["body"].lower()
        assert "lose" not in lowered and "expire" not in lowered

    def test_dismissal_hides_it_for_that_guild_only(self):
        dismissed = changelog.parse_dismissed(
            changelog.add_dismissal((), GUILD, PREMIUM.id), (PREMIUM,)
        )
        assert changelog.build_premium_card(
            GUILD, dismissed=dismissed, entries=(PREMIUM,)
        ) is None
        assert changelog.build_premium_card(
            OTHER_GUILD, dismissed=dismissed, entries=(PREMIUM,)
        ) is not None

    def test_it_falls_through_to_the_next_undismissed_entry(self):
        newer = entry("2026-10-newer", premium=True, date=date(2026, 10, 1),
                      title="Newer")
        card = changelog.build_premium_card(
            GUILD, dismissed=((GUILD, newer.id),), entries=(newer, PREMIUM)
        )
        assert card["title"] == "New in Premium: Log channel"

    def test_the_cta_resolves_from_an_endpoint_name(self):
        # Never a URL from data. The same rule `return_to` follows.
        pointed = entry("2026-09-pointed", premium=True,
                        cta_endpoint="guild_settings")
        card = changelog.build_premium_card(GUILD, entries=(pointed,))
        assert card["action"] == "settings"

    def test_an_unknown_endpoint_renders_nothing_rather_than_a_broken_button(self):
        bad = entry("2026-09-bad", premium=True, cta_endpoint="https://evil.example")
        assert changelog.build_premium_card(GUILD, entries=(bad,)) is None
        # And CI catches it before it can ship.
        assert changelog.validate_entries((bad,)) != []


class TestItFitsTheRankerBuiltIn135:
    """`build_next_step()` already ranks; this phase only feeds it.

    Its docstring asks for exactly the shape `build_premium_card` returns, so
    these tests are the seam between the two issues -- if either side changes
    that shape, one of these fails rather than the Overview quietly rendering
    a card with no title.
    """

    @staticmethod
    def configured_overview(**premium_flags):
        # A free server with both required setup rows done, so the ranker
        # falls past rank 1. The shape is `build_setup`'s and `_demo_step`'s,
        # not one invented here -- `TestTheFakePayloadMatchesTheRealOne` in
        # test_overview.py is what keeps it honest against the bot's.
        return {
            "premium": {"premium": False, "grandfathered": False, **premium_flags},
            "configured": {
                "verified_role": "5",
                "verified_role_exists": True,
                "verified_role_assignable": True,
            },
            "panel": {"posted": True, "channel_exists": True, "channel_postable": True},
            "verifications": {"known": True, "last_30_days": 40},
        }

    def test_the_card_has_the_keys_the_ranker_passes_through(self):
        card = changelog.build_premium_card(GUILD, entries=(PREMIUM,))
        assert {"title", "body", "action"} <= set(card)

    def test_a_setup_step_still_outranks_a_premium_entry(self):
        # Rank 1 is absolute: a server that cannot finish a verification must
        # be fixed, not sold to.
        broken = self.configured_overview()
        broken["configured"]["verified_role"] = None
        card = changelog.build_premium_card(GUILD, entries=(PREMIUM,))
        step = build_next_step(broken, card)
        assert step["action"] == "settings"
        assert "Log channel" not in step["title"]

    def test_a_premium_entry_outranks_the_data_backed_demo(self):
        card = changelog.build_premium_card(GUILD, entries=(PREMIUM,))
        step = build_next_step(self.configured_overview(), card)
        assert step["title"] == "New in Premium: Log channel"

    def test_with_nothing_to_announce_the_demo_still_shows(self):
        # The default path, unchanged from #135: passing None must leave the
        # page exactly as it was before this issue existed.
        card = changelog.build_premium_card(GUILD, entries=(ORDINARY,))
        assert card is None
        step = build_next_step(self.configured_overview(), card)
        assert step["action"] == "subscription"
        assert "40 members verified" in step["body"]

    def test_only_ever_one_item(self):
        card = changelog.build_premium_card(GUILD, entries=(PREMIUM,))
        step = build_next_step(self.configured_overview(), card)
        assert isinstance(step, dict)


class TestTheClientCookieWriterCarriesSecure:
    """#161, fixed here because this issue routes two more cookies through it.

    `prefs.js`'s `writeCookie` omitted `Secure` outright, on a comment
    claiming it only ran on the plain-HTTP loopback preview. It runs on every
    instant theme change in production, so the first theme switch replaced the
    server's Secure cookie with a non-Secure one lasting a year.
    """

    @staticmethod
    def source():
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "src" / "dashboard" / "static" / "prefs.js"
        ).read_text(encoding="utf-8")

    def test_secure_is_conditional_on_https(self):
        source = self.source()
        assert 'location.protocol === "https:"' in source
        assert '"; Secure"' in source

    def test_both_the_write_and_the_delete_carry_it(self):
        # A cookie set Secure is not overwritten by a non-Secure one of the
        # same name, so a delete missing the attribute silently does nothing.
        # Split on `;` statement terminators rather than newlines: the write is
        # a continued expression across three lines.
        writes = [
            statement for statement in self.source().split(";\n")
            if "document.cookie =" in statement
        ]
        assert len(writes) == 2, "expected exactly the write and the delete"
        assert all("SECURE" in statement for statement in writes)
