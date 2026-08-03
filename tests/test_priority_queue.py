"""Unit tests for premium priority placement in the verification queue (#56).

The dangerous part of this feature is not the ordering, it's the declaration.
Both services declare the same queue, and RabbitMQ rejects a declare whose
arguments differ from the existing queue's with 406 PRECONDITION_FAILED — which
takes down publishing and consuming simultaneously. Most of what follows guards
that, not the priority itself.
"""

import asyncio
from types import SimpleNamespace

import pika
import pytest

import bot
import vrc_online_checker as checker

GUILD_ID = "987654321"
OWNER_ID = "77"
SKU_ID = 555000111
OLD_ID = 100
NEW_ID = 5000


def run(coro):
    return asyncio.run(coro)


class FakeEntitlement:
    def __init__(self, sku_id=SKU_ID):
        self.sku_id = sku_id
        self.deleted = False
        self.guild_id = int(GUILD_ID)

    def is_expired(self):
        return False


def entitlements_api(*items):
    def _entitlements(**kwargs):
        async def generate():
            for item in items:
                yield item

        return generate()

    return _entitlements


def make_server(server_id=GUILD_ID, row_id=OLD_ID, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=OWNER_ID,
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.User).delete()

    wipe()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.premium_status_cache.clear()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


# ---------------------------------------------------------------
# The cross-service invariant
# ---------------------------------------------------------------
class TestQueueArgumentsMatch:
    """If these two ever drift, production 406s on both services at once."""

    def test_both_services_declare_identical_arguments(self):
        assert bot.request_queue_arguments() == checker.request_queue_arguments()

    def test_both_services_agree_on_the_priority_ceiling(self):
        assert bot.QUEUE_MAX_PRIORITY == checker.QUEUE_MAX_PRIORITY

    def test_arguments_carry_the_priority_ceiling(self):
        assert bot.request_queue_arguments() == {
            "x-max-priority": bot.QUEUE_MAX_PRIORITY
        }

    def test_priority_stays_within_the_declared_ceiling(self):
        # Publishing above x-max-priority is silently clamped by RabbitMQ, which
        # would make premium and free indistinguishable at the top end.
        assert bot.PREMIUM_REQUEST_PRIORITY <= bot.QUEUE_MAX_PRIORITY
        assert bot.DEFAULT_REQUEST_PRIORITY < bot.PREMIUM_REQUEST_PRIORITY

    def test_the_ceiling_stays_small(self):
        # RabbitMQ allocates per-level structures; its own guidance is to keep
        # this in the 1-10 range rather than reaching for 255.
        assert 1 <= bot.QUEUE_MAX_PRIORITY <= 10


# ---------------------------------------------------------------
# Gating
# ---------------------------------------------------------------
class TestRequestPriority:
    def test_premium_gets_the_high_priority(self, enforced):
        flags = bot.PremiumFlags(premium=True, grandfathered=False)
        assert flags.request_priority() == bot.PREMIUM_REQUEST_PRIORITY

    def test_free_gets_the_default(self, enforced):
        flags = bot.PremiumFlags(premium=False, grandfathered=False)
        assert flags.request_priority() == bot.DEFAULT_REQUEST_PRIORITY

    def test_grandfathering_does_not_unlock_it(self, enforced):
        # Brand new feature, so an old server has nothing to preserve.
        flags = bot.PremiumFlags(premium=False, grandfathered=True)
        assert flags.request_priority() == bot.DEFAULT_REQUEST_PRIORITY

    def test_everyone_is_prioritised_while_the_tier_is_off(self):
        # PREMIUM_SKU_ID unset: the whole premium system is inert, so nothing
        # should be sorted below anything else.
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.request_priority() == bot.PREMIUM_REQUEST_PRIORITY


# ---------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------
@pytest.fixture
def publish_spy(monkeypatch):
    """Capture what publish_to_vrc_checker hands to pika."""
    calls = SimpleNamespace(declares=[], publishes=[])

    class FakeChannel:
        def queue_declare(self, queue, durable=False, arguments=None):
            calls.declares.append(
                {"queue": queue, "durable": durable, "arguments": arguments}
            )

        def basic_publish(self, exchange, routing_key, body, properties):
            calls.publishes.append(
                {"routing_key": routing_key, "body": body, "properties": properties}
            )

    class FakeConn:
        is_open = True

        def channel(self):
            return FakeChannel()

        def close(self):
            pass

    monkeypatch.setattr(bot, "_rabbitmq_connect_with_retry", lambda **kw: FakeConn())
    return calls


class TestPublish:
    def test_declares_with_priority_arguments(self, publish_spy):
        run(bot.publish_to_vrc_checker("1", "usr_x", GUILD_ID, None))
        assert publish_spy.declares[0]["arguments"] == bot.request_queue_arguments()
        assert publish_spy.declares[0]["durable"] is True

    def test_premium_priority_reaches_the_message(self, publish_spy):
        run(
            bot.publish_to_vrc_checker(
                "1", "usr_x", GUILD_ID, None, priority=bot.PREMIUM_REQUEST_PRIORITY
            )
        )
        assert (
            publish_spy.publishes[0]["properties"].priority
            == bot.PREMIUM_REQUEST_PRIORITY
        )

    def test_default_priority_when_unspecified(self, publish_spy):
        run(bot.publish_to_vrc_checker("1", "usr_x", GUILD_ID, None))
        assert (
            publish_spy.publishes[0]["properties"].priority
            == bot.DEFAULT_REQUEST_PRIORITY
        )

    def test_messages_stay_persistent(self, publish_spy):
        # delivery_mode=2 predates this change and must survive it: a restart
        # losing queued verifications would be a real regression.
        run(bot.publish_to_vrc_checker("1", "usr_x", GUILD_ID, None))
        assert publish_spy.publishes[0]["properties"].delivery_mode == 2


class TestCheckerDeclare:
    """The consumer side of the invariant.

    TestQueueArgumentsMatch only proves the two helpers agree; it says nothing
    about whether the checker actually calls its own. Dropping `arguments=`
    here is exactly the change that produces the 406 in production, so it needs
    asserting against the real consume loop.
    """

    def declares_from_listen(self, monkeypatch):
        declares = []

        class FakeChannel:
            def queue_declare(self, queue, durable=False, arguments=None):
                declares.append(
                    {"queue": queue, "durable": durable, "arguments": arguments}
                )
                # BaseException, so the loop's `except Exception` handlers
                # cannot swallow it and spin forever.
                raise KeyboardInterrupt

        class FakeConn:
            is_open = False

            def channel(self):
                return FakeChannel()

        monkeypatch.setattr(
            checker, "_rabbitmq_connect_with_retry", lambda **kw: FakeConn()
        )
        try:
            checker.listen_for_verifications()
        except KeyboardInterrupt:
            pass
        return declares

    def test_consume_declares_with_priority_arguments(self, monkeypatch):
        declares = self.declares_from_listen(monkeypatch)
        assert declares, "the consume loop never declared the queue"
        assert declares[0]["arguments"] == checker.request_queue_arguments()

    def test_consume_declares_durable(self, monkeypatch):
        # Losing durability would drop every queued verification on a broker
        # restart, which is a far worse regression than the priority itself.
        declares = self.declares_from_listen(monkeypatch)
        assert declares[0]["durable"] is True


# ---------------------------------------------------------------
# The 406
# ---------------------------------------------------------------
def channel_closed_406():
    return pika.exceptions.ChannelClosedByBroker(
        406, "PRECONDITION_FAILED - inequivalent arg 'x-max-priority'"
    )


class TestQueueArgumentMismatch:
    def test_recognised_in_both_services(self):
        error = channel_closed_406()
        assert bot.is_queue_argument_mismatch(error) is True
        assert checker.is_queue_argument_mismatch(error) is True

    def test_other_channel_errors_are_not_mistaken_for_it(self):
        other = pika.exceptions.ChannelClosedByBroker(404, "NOT_FOUND")
        assert bot.is_queue_argument_mismatch(other) is False
        assert checker.is_queue_argument_mismatch(other) is False

    def test_unrelated_exceptions(self):
        assert bot.is_queue_argument_mismatch(RuntimeError("boom")) is False

    def test_publish_gives_up_immediately_rather_than_retrying(self, monkeypatch):
        """Retrying a 406 is pointless and buries the cause.

        The queue's arguments will not change between attempts, so each retry
        fails identically and the operator sees only a generic "publish failed".
        """
        attempts = []

        def connect(**kwargs):
            attempts.append(1)
            raise channel_closed_406()

        monkeypatch.setattr(bot, "_rabbitmq_connect_with_retry", connect)
        monkeypatch.setenv("RABBITMQ_PUBLISH_TRIES", "3")

        run(bot.publish_to_vrc_checker("1", "usr_x", GUILD_ID, None))
        assert len(attempts) == 1

    def test_the_operator_is_told_how_to_fix_it(self, monkeypatch, caplog):
        def connect(**kwargs):
            raise channel_closed_406()

        monkeypatch.setattr(bot, "_rabbitmq_connect_with_retry", connect)
        with caplog.at_level("ERROR"):
            run(bot.publish_to_vrc_checker("1", "usr_x", GUILD_ID, None))

        message = " ".join(r.getMessage() for r in caplog.records).lower()
        assert "delete" in message
        assert "x-max-priority" in message

    def test_ordinary_publish_failures_still_retry(self, monkeypatch):
        # The early return must be specific to the 406, not swallow everything.
        attempts = []

        def connect(**kwargs):
            attempts.append(1)
            raise pika.exceptions.AMQPConnectionError("down")

        monkeypatch.setattr(bot, "_rabbitmq_connect_with_retry", connect)
        monkeypatch.setenv("RABBITMQ_PUBLISH_TRIES", "3")
        monkeypatch.setattr(bot.time, "sleep", lambda s: None)

        run(bot.publish_to_vrc_checker("1", "usr_x", GUILD_ID, None))
        assert len(attempts) == 3


# ---------------------------------------------------------------
# Call-site wiring
# ---------------------------------------------------------------
class TestCallSiteWiring:
    """The gates and the publisher are both correct in isolation.

    What is easy to get wrong is joining them: a call site that forgets to
    pass `priority` leaves every server on the default, and the feature simply
    does not work while every unit test still passes. This exercises the
    no-code re-check path end to end, which is the most testable of the three.
    """

    def interaction(self, entitlements):
        replies = []

        class Response:
            def is_done(self):
                return False

            async def defer(self, ephemeral=False):
                pass

            async def send_message(self, *a, **kw):
                replies.append(a)

            async def send_modal(self, modal):
                replies.append(("modal",))

        class Followup:
            async def send(self, *a, **kw):
                replies.append(a)

        return SimpleNamespace(
            guild_id=int(GUILD_ID),
            user=SimpleNamespace(id=42),
            locale="en-US",
            entitlements=list(entitlements),
            response=Response(),
            followup=Followup(),
        )

    def prepare(self, row_id=NEW_ID):
        make_server(row_id=row_id)
        with bot.session_scope() as session:
            session.add(
                bot.User(
                    discord_id="42", verification_status=False, vrc_user_id="usr_x"
                )
            )

    def published_priority(self, monkeypatch, entitlements):
        captured = {}

        async def fake_publish(*args, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(bot, "publish_to_vrc_checker", fake_publish)
        monkeypatch.setattr(bot, "_verification_cooldowns", {})
        run(bot.process_verification(self.interaction(entitlements)))
        return captured.get("priority")

    def test_premium_guild_publishes_at_high_priority(self, enforced, monkeypatch):
        self.prepare()
        assert (
            self.published_priority(monkeypatch, [FakeEntitlement()])
            == bot.PREMIUM_REQUEST_PRIORITY
        )

    def test_free_guild_publishes_at_default_priority(self, enforced, monkeypatch):
        self.prepare()
        assert (
            self.published_priority(monkeypatch, []) == bot.DEFAULT_REQUEST_PRIORITY
        )
