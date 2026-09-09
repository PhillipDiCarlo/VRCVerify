"""The "is this VRChat account already someone else's?" check (#281).

VRCUsernameModal had no tests at all, and the one branch worth having them
was wrong: it compared `users.discord_id` -- a bigint column, so an int on
production -- against `str(interaction.user.id)`. `123 != "123"` is True, so
the check answered "yes, somebody else's" for every caller, including the
account that actually owns the id. The "it is the same Discord user" case its
own comment contemplates was unreachable on the deployed database.

HOW BAD IT WAS: latent, not live. process_verification only opens this modal
when the member has no stored VRChat id (case B) or no row at all (case C),
and in neither can the row found by `filter_by(vrc_user_id=...)` be their own
-- so the broken comparison had no caller that could reach it. The defensive
branch simply could not have done its job if one ever arrived. That is worth
fixing on its own, and it is worth a test so the next route into this modal
does not inherit it.

WHY IT MATTERS ANYWAY: this is the #164 shape exactly -- no exception, no log
line, just a comparison between two types that can never be equal -- and it
sat one line away from the `filter_by()` and `int()` uses that
test_schema_snapshot.KNOWN cited as the reason declaring the column honestly
could wait. It was invisible to the fast suite until #281 declared it as the
bigint it is and SQLite started handing back an int too.
"""

import asyncio
from types import SimpleNamespace

import pytest

import bot
import locales

GUILD_ID = "123456789"
MEMBER_ID = 555000222
VRC_ID = "usr_1234d567-b12e-123d-a1c2-fd12345a67ea"


def run(coro):
    return asyncio.run(coro)


def make_user(discord_id, vrc_user_id=VRC_ID, verified=True):
    with bot.session_scope() as session:
        session.add(
            bot.User(
                discord_id=discord_id,
                vrc_user_id=vrc_user_id,
                verification_status=verified,
            )
        )


@pytest.fixture(autouse=True)
def clean_rows():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.User).delete()
            session.query(bot.PendingVerification).delete()

    wipe()
    yield
    wipe()


class Interaction(SimpleNamespace):
    """Enough of discord.Interaction for the modal's submit path."""

    def __init__(self, user_id=MEMBER_ID):
        self.sent = []

        async def send_message(content, **kwargs):
            self.sent.append(content)

        super().__init__(
            guild_id=int(GUILD_ID),
            user=SimpleNamespace(id=user_id),
            locale="en-US",
            response=SimpleNamespace(send_message=send_message),
        )


def submit(interaction, value=VRC_ID):
    modal = bot.VRCUsernameModal(interaction)
    # The TextInput is a descriptor on the class; the submit path only reads
    # `.value`, so a stand-in carrying one is all this needs.
    modal.vrc_username = SimpleNamespace(value=value)
    run(modal.on_submit(interaction))
    return interaction.sent


def pending_rows():
    with bot.session_scope() as session:
        return session.query(bot.PendingVerification).count()


class TestTheAccountIsAlreadyLinkedCheck:
    def test_a_different_member_is_refused(self):
        """The case the check exists for, and the half that always worked."""
        make_user(discord_id="999000111")

        sent = submit(Interaction(user_id=MEMBER_ID))

        assert locales.VRC_ID_ALREADY_LINKED in sent[0]
        assert pending_rows() == 0

    def test_the_owner_of_the_id_is_not_refused(self):
        """THE BUG. Re-submitting your own VRChat id is not a collision.

        Before #281 this compared an int column against a string, so it took
        the owner of the id for an impostor and answered "already registered
        to another Discord account" about their own account. Unreachable
        through today's call sites (see the module docstring); this pins the
        branch so it is correct for the first one that does reach it.
        """
        make_user(discord_id=str(MEMBER_ID))

        sent = submit(Interaction(user_id=MEMBER_ID))

        assert locales.VRC_ID_ALREADY_LINKED not in sent[0]
        assert "VRChat userID saved" in sent[0]
        assert pending_rows() == 1

    def test_an_unclaimed_id_is_not_refused(self):
        """No row at all, which is the ordinary first-time path."""
        sent = submit(Interaction(user_id=MEMBER_ID))

        assert locales.VRC_ID_ALREADY_LINKED not in sent[0]
        assert pending_rows() == 1

    def test_a_member_with_no_linked_account_yet_is_not_refused(self):
        """`vrc_user_id=""` is the "verified, nothing linked" state that
        #281 left reachable after making the column NOT NULL. Such a row must
        not match the lookup for a real id, and must not block its owner."""
        make_user(discord_id=str(MEMBER_ID), vrc_user_id="", verified=False)

        sent = submit(Interaction(user_id=MEMBER_ID))

        assert locales.VRC_ID_ALREADY_LINKED not in sent[0]
        assert pending_rows() == 1
