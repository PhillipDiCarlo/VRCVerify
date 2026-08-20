"""Unit tests for card subscriptions alongside Discord ones (issue #88, step 1).

Step 1 is deliberately inert: with STRIPE_ENABLED unset nothing here can be
bought, no route exists, and the premium gate behaves exactly as it did before.
What these tests pin is that the plumbing under that switch is correct *before*
anyone can pay through it, because the first time it runs for real there will
be money on the other end of it.

The themes, in the order they bite:

- the gate is an OR over two independent sources, and neither may see or
  corrupt the other's cached answer
- the two sources fail in opposite directions on purpose, and the Stripe one
  failing open would hand premium to every server in the bot
- a webhook is untrusted *ordering*, not just untrusted content: Stripe retries
  for three days and promises nothing about sequence
- a payment must never cost a server its grandfathered features, which is what
  writing a `servers` row for an unknown guild would silently do
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot

GUILD_ID = "987654321"
OTHER_GUILD_ID = "123123123"
OWNER_ID = "77"
SKU_ID = 555000111

CUSTOMER = "cus_TESTCUSTOMER"
SUBSCRIPTION = "sub_TESTSUBSCRIPTION"
PRICE_MONTHLY = "price_1U3q9eJZiVMQTim6LtcYV4x6"
PRICE_YEARLY = "price_1U3q9eJZiVMQTim6AUOLZ59e"


def run(coro):
    """Run an async bot helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def in_days(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def make_event(
    *,
    event_id="evt_1",
    event_created=None,
    subscription_id=SUBSCRIPTION,
    customer_id=CUSTOMER,
    price_id=PRICE_MONTHLY,
    status="active",
    current_period_end=None,
    cancel_at_period_end=False,
) -> dict:
    """One normalised payload, exactly as the dashboard forwards it."""
    return {
        "event_id": event_id,
        "event_created": (event_created or datetime.now(timezone.utc)).isoformat(),
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "price_id": price_id,
        "status": status,
        "current_period_end": (current_period_end or in_days(30)).isoformat(),
        "cancel_at_period_end": cancel_at_period_end,
    }


def store_subscription(
    *,
    server_id=GUILD_ID,
    status="active",
    price_id=PRICE_MONTHLY,
    subscription_id=SUBSCRIPTION,
    current_period_end=None,
    cancel_at_period_end=False,
    last_event_created=None,
):
    """Write a mirrored row directly, the way step 1 is meant to be exercised."""
    with bot.session_scope() as session:
        session.add(
            bot.StripeSubscription(
                server_id=server_id,
                stripe_customer_id=CUSTOMER,
                stripe_subscription_id=subscription_id,
                price_id=price_id,
                status=status,
                current_period_end=current_period_end or in_days(30),
                cancel_at_period_end=cancel_at_period_end,
                last_event_created=last_event_created or datetime.now(timezone.utc),
            )
        )


def make_server(server_id=GUILD_ID, row_id=100, **overrides):
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


def make_interaction(entitlements=(), guild_id=GUILD_ID):
    return SimpleNamespace(
        guild_id=int(guild_id) if guild_id is not None else None,
        entitlements=list(entitlements),
        locale="en-US",
        guild=SimpleNamespace(id=int(guild_id or 0), name="Test Server"),
        user=SimpleNamespace(id=1),
    )


class FakeEntitlement:
    """Enough of discord.Entitlement for the resolution helpers."""

    def __init__(self, sku_id=SKU_ID, expired=False, deleted=False):
        self.sku_id = sku_id
        self.deleted = deleted
        self._expired = expired

    def is_expired(self) -> bool:
        return self._expired


def fake_entitlements_api(*items):
    def _entitlements(**kwargs):
        async def generate():
            for item in items:
                yield item

        return generate()

    return _entitlements


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.StripeSubscription).delete()
            session.query(bot.StripeEvent).delete()
            session.query(bot.Server).delete()
            session.query(bot.DashboardAudit).delete()
            session.query(bot.PremiumGrandfatherLine).delete()
            # Rows here are never deleted in production -- that is the whole
            # point of the ledger -- so a test that writes one would otherwise
            # make every later test's server look like a returning customer.
            session.query(bot.PremiumEntitlementSeen).delete()

    wipe()
    bot.stripe_status_cache.clear()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.stripe_status_cache.clear()
    bot.premium_status_cache.clear()


@pytest.fixture
def stripe_on(monkeypatch):
    """Switch the feature on, as STRIPE_ENABLED=1 would."""
    monkeypatch.setattr(bot, "STRIPE_ENABLED", True)
    bot.stripe_status_cache.clear()


@pytest.fixture
def enforced(monkeypatch):
    """Turn the premium tier on, as PREMIUM_SKU_ID would."""
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    monkeypatch.setattr(bot.bot, "entitlements", fake_entitlements_api())
    bot.premium_status_cache.clear()


# ---------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------
class TestKillSwitch:
    """With STRIPE_ENABLED unset this whole feature must be as if absent.

    That is what lets step 1 ship to production ahead of a Stripe account
    existing, which is the entire point of doing it first.
    """

    def test_a_stored_row_grants_nothing_while_the_switch_is_off(self):
        store_subscription()
        assert bot.stripe_active(GUILD_ID) is False

    def test_the_table_is_not_even_queried(self, monkeypatch):
        """Not merely False -- no query at all.

        This is what keeps resolve_premium_flags_from_interaction free of I/O
        while the feature is dormant, and a slash command is the hottest path
        in the bot.
        """

        def boom():
            raise AssertionError("the table must not be read with Stripe off")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.stripe_active(GUILD_ID) is False

    def test_the_settings_payload_reports_it_off(self, enforced):
        make_server()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["enabled"] is False
        assert payload["stripe"]["active"] is False
        assert payload["stripe"]["status"] is None

    def test_a_stored_row_is_invisible_to_the_page_while_off(self, enforced):
        make_server()
        store_subscription()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["enabled"] is False
        assert payload["stripe"]["active"] is False


# ---------------------------------------------------------------
# What counts as paid
# ---------------------------------------------------------------
class TestWhatCountsAsPaid:
    @pytest.mark.parametrize(
        "status, expected",
        [
            ("active", True),
            ("trialing", True),
            # Stripe is still retrying the card. A late charge is not a
            # cancellation, and cutting a customer off on the first failed
            # retry is the mistake the entitlement fail-open exists to avoid.
            ("past_due", True),
            # Stripe has given up retrying.
            ("unpaid", False),
            ("incomplete", False),
            ("incomplete_expired", False),
        ],
    )
    def test_status_decides(self, stripe_on, status, expected):
        store_subscription(status=status)
        assert bot.stripe_active(GUILD_ID) is expected

    def test_cancelled_keeps_premium_until_the_paid_period_runs_out(self, stripe_on):
        """The Discord side already behaves this way; the two must not disagree.

        A cancellation leaves the subscription live until the period the
        customer paid for actually ends. Only a refund takes it away sooner,
        and Stripe reports that as a status change of its own.
        """
        store_subscription(
            status="canceled", cancel_at_period_end=True, current_period_end=in_days(9)
        )
        assert bot.stripe_active(GUILD_ID) is True

    def test_cancelled_stops_once_the_period_has_run_out(self, stripe_on):
        store_subscription(status="canceled", current_period_end=in_days(-1))
        assert bot.stripe_active(GUILD_ID) is False

    def test_even_an_active_status_expires_with_its_period(self, stripe_on):
        """Both conditions are required, not either.

        A row left `active` because a final webhook never arrived must not
        grant premium forever.
        """
        store_subscription(status="active", current_period_end=in_days(-1))
        assert bot.stripe_active(GUILD_ID) is False

    def test_no_row_at_all_is_not_subscribed(self, stripe_on):
        assert bot.stripe_active(GUILD_ID) is False


# ---------------------------------------------------------------
# The failure rule
# ---------------------------------------------------------------
class TestTheFailureRule:
    """The Stripe read fails CLOSED where the Discord read fails OPEN.

    Read the docstring on stripe_active before changing anything here. The
    asymmetry looks like an inconsistency and is not one: a server that has
    never had a card subscription cannot acquire one during a database blip, so
    failing open would hand premium to every server in the bot the moment
    Postgres hiccups.
    """

    def test_a_failed_read_with_no_history_is_false(self, stripe_on, monkeypatch):
        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.stripe_active(GUILD_ID) is False

    def test_a_failed_read_falls_back_to_last_known(self, stripe_on, monkeypatch):
        """A paying server is protected exactly as the Discord path protects one."""
        store_subscription()
        assert bot.stripe_active(GUILD_ID) is True  # seeds last-known
        bot.stripe_status_cache.invalidate(GUILD_ID)

        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.stripe_active(GUILD_ID) is True

    def test_the_guess_never_becomes_the_fallback_it_came_from(
        self, stripe_on, monkeypatch
    ):
        """A False guessed during an outage must not be recorded as last-known.

        If it were, a server that later pays would still be answered from a
        stale negative after its own webhook landed.
        """

        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.stripe_active(GUILD_ID) is False
        assert bot.stripe_status_cache.get_last_known(GUILD_ID) is None


# ---------------------------------------------------------------
# The gate
# ---------------------------------------------------------------
class TestTheGate:
    def test_premium_via_stripe_only(self, enforced, stripe_on):
        make_server()
        store_subscription()
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.premium is True
        assert flags.allows(bot.FEATURE_BRANDED_PANEL)

    def test_premium_via_discord_only(self, enforced, stripe_on, monkeypatch):
        make_server()
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(FakeEntitlement())
        )
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.premium is True

    def test_premium_via_neither(self, enforced, stripe_on):
        make_server(row_id=5000)
        bot.capture_grandfather_line()
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.premium is False
        assert flags.allows(bot.FEATURE_BRANDED_PANEL) is False

    def test_premium_via_both(self, enforced, stripe_on, monkeypatch):
        """Being double-billed must not also break something.

        Detection and the warning belong to the website; the gate's only job is
        to keep saying yes.
        """
        make_server()
        store_subscription()
        monkeypatch.setattr(
            bot.bot, "entitlements", fake_entitlements_api(FakeEntitlement())
        )
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.premium is True

    def test_an_interaction_grants_premium_from_a_stripe_row(self, enforced, stripe_on):
        make_server()
        store_subscription()
        flags = bot.resolve_premium_flags_from_interaction(make_interaction())
        assert flags.premium is True

    def test_a_stripe_row_for_another_guild_grants_nothing(self, enforced, stripe_on):
        make_server()
        store_subscription(server_id=OTHER_GUILD_ID, subscription_id="sub_OTHER")
        flags = run(bot.resolve_premium_flags(GUILD_ID))
        assert flags.premium is False

    def test_the_two_caches_are_separate_instances(self):
        """Sharing one would let either source corrupt the other's fallback.

        Their fallbacks point in opposite directions, so a collision would not
        merely be untidy -- it would silently invert one of them.
        """
        assert bot.stripe_status_cache is not bot.premium_status_cache


class TestTheShortCircuit:
    """premium_from_interaction is free, and this must keep it nearly so."""

    def test_an_entitled_interaction_never_reads_the_table(
        self, enforced, stripe_on, monkeypatch
    ):
        """Assert it, or the short-circuit regresses and nothing notices.

        Losing it puts a database query behind every slash command in the bot,
        which is invisible in tests and expensive in production.
        """
        reads = []
        real_scope = bot.session_scope

        def counting_scope():
            reads.append(1)
            return real_scope()

        monkeypatch.setattr(bot, "session_scope", counting_scope)
        flags = bot.resolve_premium_flags_from_interaction(
            make_interaction([FakeEntitlement()])
        )
        assert flags.premium is True
        assert reads == [], "an entitled interaction must not touch the database"

    def test_an_unentitled_interaction_pays_one_cached_lookup(
        self, enforced, stripe_on, monkeypatch
    ):
        store_subscription()
        reads = []
        real_scope = bot.session_scope

        def counting_scope():
            reads.append(1)
            return real_scope()

        monkeypatch.setattr(bot, "session_scope", counting_scope)
        assert bot.resolve_premium_flags_from_interaction(make_interaction()).premium
        first = len(reads)
        assert bot.resolve_premium_flags_from_interaction(make_interaction()).premium
        assert len(reads) == first, "the second interaction must be served from cache"


# ---------------------------------------------------------------
# The writer
# ---------------------------------------------------------------
class TestTheWriter:
    def test_a_new_subscription_is_stored_and_grants_premium(self, stripe_on):
        result = run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        assert result["applied"] is True
        assert result["premium"] is True
        assert bot.stripe_active(GUILD_ID) is True

    def test_the_write_invalidates_the_cache_immediately(self, stripe_on):
        """A purchase must not wait out a TTL to take effect.

        This is the property that justifies the write landing in the bot rather
        than the dashboard writing the database itself: the row and the cache
        are the same process.
        """
        assert bot.stripe_active(GUILD_ID) is False  # caches the negative
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        assert bot.stripe_active(GUILD_ID) is True

    def test_a_cancellation_is_applied(self, stripe_on):
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        result = run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_2",
                    status="canceled",
                    cancel_at_period_end=True,
                    current_period_end=in_days(-1),
                ),
            )
        )
        assert result["applied"] is True
        assert bot.stripe_active(GUILD_ID) is False

    def test_the_audit_row_names_a_machine_not_a_person(self, stripe_on):
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        with bot.session_scope() as session:
            rows = session.query(bot.DashboardAudit).all()
            actors = [row.actor_id for row in rows]
            fields = [row.field for row in rows]
        assert actors == [bot.STRIPE_AUDIT_ACTOR]
        assert fields == ["stripe_subscription"]
        assert not bot.STRIPE_AUDIT_ACTOR.isdigit(), (
            "the Stripe actor must never be mistakable for a Discord id"
        )

    @pytest.mark.parametrize(
        "field",
        ["event_id", "subscription_id", "customer_id", "price_id", "status"],
    )
    def test_a_missing_required_field_is_refused(self, stripe_on, field):
        payload = make_event()
        payload[field] = ""
        with pytest.raises(bot.SettingRejected):
            run(bot.write_dashboard_stripe_subscription(GUILD_ID, payload))

    @pytest.mark.parametrize("field", ["current_period_end", "event_created"])
    def test_an_unparseable_timestamp_is_refused(self, stripe_on, field):
        payload = make_event()
        payload[field] = "not a date"
        with pytest.raises(bot.SettingRejected):
            run(bot.write_dashboard_stripe_subscription(GUILD_ID, payload))

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_cancel_at_period_end_is_not_coerced(self, stripe_on, value):
        """bool("false") is True.

        This is the field that decides whether the page says "renews on the
        3rd" or "ends on the 3rd", so a normalisation slip on the other side of
        the wire would make a wrong statement about somebody's money. Refuse it
        rather than guess.
        """
        payload = make_event()
        payload["cancel_at_period_end"] = value
        with pytest.raises(bot.SettingRejected):
            run(bot.write_dashboard_stripe_subscription(GUILD_ID, payload))

    def test_a_real_boolean_is_accepted(self, stripe_on):
        payload = make_event(cancel_at_period_end=True)
        assert run(bot.write_dashboard_stripe_subscription(GUILD_ID, payload))[
            "applied"
        ] is True

    def test_a_failed_write_reports_none_so_stripe_retries(self, stripe_on, monkeypatch):
        """Never 200-and-drop. A bot restart must not cost a subscription event."""

        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event())) is None


class TestIdempotency:
    def test_the_same_event_twice_writes_once(self, stripe_on):
        first = run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        second = run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        assert first["applied"] is True
        assert second["applied"] is False
        assert second["reason"] == "duplicate_event"
        with bot.session_scope() as session:
            assert session.query(bot.DashboardAudit).count() == 1
            assert session.query(bot.StripeEvent).count() == 1

    def test_a_replayed_cancellation_does_not_re_apply(self, stripe_on):
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        cancel = make_event(
            event_id="evt_cancel", status="canceled", current_period_end=in_days(-1)
        )
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, cancel))
        # A later renewal, then Stripe retries the cancellation it already sent.
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(event_id="evt_renew", event_created=in_days(1)),
            )
        )
        assert bot.stripe_active(GUILD_ID) is True
        assert (
            run(bot.write_dashboard_stripe_subscription(GUILD_ID, cancel))["applied"]
            is False
        )
        assert bot.stripe_active(GUILD_ID) is True

    def test_a_failed_apply_does_not_mark_the_event_processed(
        self, stripe_on, monkeypatch
    ):
        """The ledger entry and the change are one transaction, or neither.

        Recording an event that was never applied would lose it permanently:
        Stripe's retry would be answered "already processed" and the change
        would never land.
        """
        real_scope = bot.session_scope

        class Sabotage(RuntimeError):
            pass

        def fail_after_the_ledger(*args, **kwargs):
            raise Sabotage("apply failed")

        monkeypatch.setattr(bot, "_record_dashboard_audit", fail_after_the_ledger)
        assert run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event())) is None
        monkeypatch.undo()

        with real_scope() as session:
            assert session.query(bot.StripeEvent).count() == 0
            assert session.query(bot.StripeSubscription).count() == 0
        # And the retry now lands.
        assert run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))[
            "applied"
        ] is True


class TestOrdering:
    """Stripe does not promise events arrive in order."""

    def test_an_older_event_is_recorded_but_not_applied(self, stripe_on):
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_cancel",
                    event_created=in_days(0),
                    status="canceled",
                    current_period_end=in_days(-1),
                ),
            )
        )
        stale = make_event(
            event_id="evt_stale", event_created=in_days(-1), status="active"
        )
        result = run(bot.write_dashboard_stripe_subscription(GUILD_ID, stale))

        assert result["applied"] is False
        assert result["reason"] == "out_of_order"
        # The event is still recorded, so a retry of it is cheap.
        with bot.session_scope() as session:
            assert session.query(bot.StripeEvent).count() == 2
        # And the cancellation stands: a delayed `updated` must never resurrect
        # premium for a server that cancelled.
        assert bot.stripe_active(GUILD_ID) is False

    def test_an_event_of_identical_age_is_not_applied(self, stripe_on):
        moment = datetime.now(timezone.utc)
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID, make_event(event_id="evt_a", event_created=moment)
            )
        )
        result = run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_b", event_created=moment, status="canceled"
                ),
            )
        )
        assert result["applied"] is False

    def test_the_ordering_guard_is_per_subscription(self, stripe_on):
        """A stale event for one subscription must not stall another.

        Scoping the guard per guild would mean the newest event from *any*
        subscription set the bar for all of them, so a guild with two would
        start dropping legitimate events for whichever renewed second.
        """
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID, make_event(event_created=in_days(5))
            )
        )
        result = run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_other",
                    subscription_id="sub_SECOND",
                    event_created=in_days(1),
                ),
            )
        )
        assert result["applied"] is True


class TestTwoLiveSubscriptions:
    """A guild really can be paying twice, and the table has to hold it.

    Found by probing rather than by reasoning: with one row per guild, an
    ordinary renewal of the older subscription overwrote the newer one, and the
    older one's later cancellation then switched premium off for a server still
    being billed for the other. No guard on a one-row table closes that, because
    the state it needs to hold has two subscriptions in it.

    Note this is exactly the case the issue's Double billing section says to
    expect — so the bug fired precisely where it was most likely to be hit.
    """

    def both(self):
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_second",
                    subscription_id="sub_SECOND",
                    price_id=PRICE_YEARLY,
                    event_created=in_days(1),
                ),
            )
        )

    def test_both_are_stored(self, stripe_on):
        self.both()
        with bot.session_scope() as session:
            ids = {
                row.stripe_subscription_id
                for row in session.query(bot.StripeSubscription).all()
            }
        assert ids == {SUBSCRIPTION, "sub_SECOND"}

    def test_a_renewal_of_the_older_one_does_not_overwrite_the_newer(self, stripe_on):
        self.both()
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(event_id="evt_renew_first", event_created=in_days(2)),
            )
        )
        with bot.session_scope() as session:
            row = (
                session.query(bot.StripeSubscription)
                .filter_by(stripe_subscription_id="sub_SECOND")
                .one()
            )
            assert row.price_id == PRICE_YEARLY

    def test_cancelling_one_leaves_premium_on_for_the_other(self, stripe_on):
        """The bug, stated as the behaviour it should have had.

        Cancel the first subscription; the second is still being billed, so
        premium stays on.
        """
        self.both()
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_cancel_first",
                    subscription_id=SUBSCRIPTION,
                    status="canceled",
                    current_period_end=in_days(-1),
                    event_created=in_days(3),
                ),
            )
        )
        bot.stripe_status_cache.clear()
        assert bot.stripe_active(GUILD_ID) is True

    def test_cancelling_both_does_end_premium(self, stripe_on):
        self.both()
        for event_id, subscription_id in (
            ("evt_c1", SUBSCRIPTION),
            ("evt_c2", "sub_SECOND"),
        ):
            run(
                bot.write_dashboard_stripe_subscription(
                    GUILD_ID,
                    make_event(
                        event_id=event_id,
                        subscription_id=subscription_id,
                        status="canceled",
                        current_period_end=in_days(-1),
                        event_created=in_days(3),
                    ),
                )
            )
        bot.stripe_status_cache.clear()
        assert bot.stripe_active(GUILD_ID) is False

    def test_double_billing_is_visible_to_the_website(self, enforced, stripe_on):
        """A row count, not a special case -- which is only possible because
        the table keys by subscription rather than by guild."""
        make_server()
        self.both()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["active_count"] == 2
        assert payload["stripe"]["active"] is True

    def test_one_subscription_is_not_double_billing(self, enforced, stripe_on):
        make_server()
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["active_count"] == 1

    def test_the_page_describes_the_longest_running_paid_subscription(
        self, enforced, stripe_on
    ):
        make_server()
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_second",
                    subscription_id="sub_SECOND",
                    price_id=PRICE_YEARLY,
                    current_period_end=in_days(300),
                ),
            )
        )
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["price_id"] == PRICE_YEARLY

    def test_a_subscription_cannot_be_moved_between_guilds(self, stripe_on):
        """One Stripe subscription belongs to one guild. Anything else is an
        anomaly needing a human, and is refused rather than applied."""
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        result = run(
            bot.write_dashboard_stripe_subscription(
                OTHER_GUILD_ID,
                make_event(event_id="evt_steal", event_created=in_days(1)),
            )
        )
        assert result["applied"] is False
        assert result["reason"] == "guild_mismatch"
        assert bot.stripe_active(OTHER_GUILD_ID) is False


class TestTheEventLedgerIsPruned:
    """A-24 again: a table that only ever grows is a table nobody pruned.

    The sweep rides on the insert, so the only thing that can add a row is also
    the thing that removes them. There is no scheduler to forget to run.
    """

    def test_events_past_the_retention_window_are_forgotten(self, stripe_on):
        with bot.session_scope() as session:
            session.add(
                bot.StripeEvent(
                    event_id="evt_ancient",
                    received_at=datetime.now(timezone.utc) - timedelta(days=400),
                )
            )
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        with bot.session_scope() as session:
            remaining = {row.event_id for row in session.query(bot.StripeEvent).all()}
        assert remaining == {"evt_1"}

    def test_events_inside_the_retry_window_are_kept(self, stripe_on):
        """Stripe retries for three days. Forgetting an id inside that window
        would make a redelivery look like a new event and apply it twice."""
        with bot.session_scope() as session:
            session.add(
                bot.StripeEvent(
                    event_id="evt_yesterday",
                    received_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
            )
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        with bot.session_scope() as session:
            remaining = {row.event_id for row in session.query(bot.StripeEvent).all()}
        assert remaining == {"evt_yesterday", "evt_1"}

    def test_a_failed_prune_swallows_its_own_error(self):
        """Losing a subscription event because a housekeeping DELETE went wrong
        would be a far worse trade than a table that stays large for a day.

        Exercises the real failure — the DELETE itself raising — rather than
        the whole function being replaced, which would step over the guard
        being tested.
        """

        class SabotagedSession:
            def query(self, *args, **kwargs):
                raise RuntimeError("delete failed")

        # Must not raise.
        bot._prune_stripe_events(SabotagedSession(), datetime.now(timezone.utc))


class TestTheUnknownGuildTrap:
    """A payment must never cost a server its grandfathered features.

    Inserting a `servers` row for a guild that has none would mint a fresh
    `servers.id` above the captured grandfather line -- silently and
    permanently withdrawing features the server was promised for free, as a
    side effect of paying us. This is the subtlest failure in the whole issue
    and it gets its own class.
    """

    def test_a_webhook_for_an_unknown_guild_creates_no_server_row(self, stripe_on):
        result = run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        assert result["applied"] is True
        with bot.session_scope() as session:
            assert session.query(bot.Server).count() == 0
            assert session.query(bot.StripeSubscription).count() == 1

    def test_it_does_not_push_a_grandfathered_server_over_the_line(self, stripe_on):
        """The full scenario, end to end, because the unit above is not enough.

        A grandfathered server pays by card. Its grandfathered status must be
        exactly what it was before the payment.
        """
        make_server(row_id=100)
        with bot.session_scope() as session:
            session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=820))
        assert bot.is_grandfathered(GUILD_ID) is True

        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))

        assert bot.is_grandfathered(GUILD_ID) is True
        with bot.session_scope() as session:
            assert session.query(bot.Server).count() == 1
            assert session.query(bot.Server).one().id == 100

    def test_grandfathering_never_consults_a_stripe_row(self, stripe_on):
        """is_grandfathered is about when a server arrived, not what it pays."""
        make_server(row_id=5000)
        with bot.session_scope() as session:
            session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=820))
        store_subscription()
        assert bot.is_grandfathered(GUILD_ID) is False


# ---------------------------------------------------------------
# What the website is told
# ---------------------------------------------------------------
class TestTheSettingsPayload:
    def test_it_describes_an_active_subscription(self, enforced, stripe_on):
        make_server()
        store_subscription(price_id=PRICE_YEARLY)
        payload = run(bot.read_dashboard_settings(GUILD_ID))

        assert payload["stripe"]["enabled"] is True
        assert payload["stripe"]["active"] is True
        assert payload["stripe"]["status"] == "active"
        assert payload["stripe"]["price_id"] == PRICE_YEARLY
        assert payload["stripe"]["current_period_end"] is not None
        assert payload["stripe"]["cancel_at_period_end"] is False

    def test_premium_premium_stays_the_single_answer(self, enforced, stripe_on):
        """The stripe block explains why; it is not a second gate to OR."""
        make_server()
        store_subscription()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["premium"]["premium"] is True

    def test_past_due_is_reported_honestly_and_still_premium(self, enforced, stripe_on):
        """The page needs both halves to write "premium is still on while
        Stripe retries" -- a boolean alone could not say it."""
        make_server()
        store_subscription(status="past_due")
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["status"] == "past_due"
        assert payload["stripe"]["active"] is True
        assert payload["premium"]["premium"] is True

    def test_a_lapsed_subscription_still_reports_its_status(self, enforced, stripe_on):
        """The row survives so the page can say "ended on the 3rd"."""
        make_server()
        store_subscription(status="canceled", current_period_end=in_days(-5))
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["status"] == "canceled"
        assert payload["stripe"]["active"] is False

    def test_the_customer_id_is_shipped_because_the_portal_needs_it(
        self, enforced, stripe_on
    ):
        """An earlier version of this test asserted the opposite, and was wrong.

        Opening Stripe's billing portal requires a customer id, so the website
        genuinely needs it to offer the one action a subscriber wants -- and
        withholding it bought nothing anyway, since the dashboard holds a
        Stripe secret key and can enumerate customers with or without our help.
        """
        make_server()
        store_subscription()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["customer_id"] == CUSTOMER

    def test_no_email_ever_crosses_the_wire(self, enforced, stripe_on):
        """This is the part that did not change.

        Checkout collects an email and Stripe keeps it. Mirroring it would put
        customer PII in the one database linking Discord accounts to VRChat
        identities, to power a page whose answer is "manage billing in the
        portal".
        """
        make_server()
        store_subscription()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        rendered = repr(payload).lower()
        assert "email" not in rendered
        assert "@" not in rendered

    def test_an_unreadable_table_refuses_the_whole_read(
        self, enforced, stripe_on, monkeypatch
    ):
        """Never "not subscribed" on a failed read.

        "Not subscribed" next to a Buy button is how a paying customer is sold
        a second subscription. The API turns None into a 503 and the page
        apologises instead.
        """
        make_server()
        real_scope = bot.session_scope
        calls = []

        def failing_scope():
            calls.append(1)
            if len(calls) > 2:
                raise RuntimeError("database is down")
            return real_scope()

        monkeypatch.setattr(bot, "session_scope", failing_scope)
        assert run(bot.read_dashboard_settings(GUILD_ID)) is None


# ---------------------------------------------------------------
# /vrcverify_subscription, once there are two ways to have paid
# ---------------------------------------------------------------
class TestTheSubscriptionCommandNamesTheRightPlatform:
    """Which message an admin gets, and which buttons come with it.

    Before Stripe there was one answer to "are you subscribed" and one place
    to manage it, so the command needed neither branch. Now "yes" has three
    shapes -- Discord, card, or both -- and each sends the admin somewhere
    different. Getting this wrong is not a cosmetic bug: telling a card
    subscriber to cancel in Discord's User Settings sends them looking for a
    subscription that is not there, and showing a Buy button to a server that
    already pays is how the second subscription gets sold.
    """

    DASHBOARD = "https://dashboard.example.test"

    def reply(self, interaction, monkeypatch, dashboard_url=DASHBOARD):
        """Run the command and hand back (message, view)."""
        monkeypatch.setattr(bot, "DASHBOARD_URL", dashboard_url)
        captured = {}

        async def send_message(msg, ephemeral=False, **extra):
            captured["message"] = msg
            captured["view"] = extra.get("view")

        interaction.response = SimpleNamespace(send_message=send_message)
        run(bot.vrcverify_subscription.callback(interaction))
        return captured["message"], captured["view"]

    def link_urls(self, view):
        return [] if view is None else [
            item.url for item in view.children if getattr(item, "url", None)
        ]

    def not_grandfathered(self):
        """Put the grandfather line below this server's row.

        Without it every test server is inside the line and gets the
        grandfathered pitch, which is a different message with a different
        lead-in -- so an assertion about the unsubscribed copy would be
        checking text the admin never saw.
        """
        with bot.session_scope() as session:
            session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=1))

    def test_a_discord_subscriber_is_sent_to_discord(self, enforced, stripe_on,
                                                     monkeypatch):
        make_server()
        message, view = self.reply(
            make_interaction([FakeEntitlement()]), monkeypatch
        )
        assert "User Settings" in message
        # No buttons at all: they have bought it, and there is nothing on the
        # website they need that Discord's own settings does not cover.
        assert view is None

    def test_a_card_subscriber_is_sent_to_the_website(self, enforced, stripe_on,
                                                      monkeypatch):
        make_server()
        store_subscription()
        message, view = self.reply(make_interaction(), monkeypatch)
        assert "VRCVerify website" in message
        assert "User Settings" not in message
        assert self.link_urls(view) == [
            f"{self.DASHBOARD}/guild/{GUILD_ID}/subscription"
        ]

    def test_a_card_subscriber_still_reads_the_full_feature_list(
        self, enforced, stripe_on, monkeypatch
    ):
        """Same bundle, same money, same list.

        The card message is derived from the Discord one so the two cannot
        drift (pinned per locale in test_premium). What this adds is that the
        *command* reaches that full message rather than some shorter variant:
        every gated feature, same as anyone paying the other way reads.

        Counted off the FEATURE_ constants rather than typed. It used to say 7,
        which was true until it was not -- the same way the copy itself goes
        stale, and for the same reason.
        """
        make_server()
        store_subscription()
        card, _ = self.reply(make_interaction(), monkeypatch)
        bullets = [line for line in card.split("\n") if line.startswith("\u2022")]
        gated = {
            value
            for name, value in vars(bot).items()
            if name.startswith("FEATURE_") and isinstance(value, str)
        } - bot.UNANNOUNCED_FEATURES
        assert len(bullets) == len(gated)

    def test_paying_on_both_platforms_is_named_as_such(self, enforced, stripe_on,
                                                       monkeypatch, caplog):
        make_server()
        store_subscription()
        with caplog.at_level("WARNING"):
            message, view = self.reply(
                make_interaction([FakeEntitlement()]), monkeypatch
            )
        assert "twice" in message
        # Both places to cancel, because they have to choose one.
        assert "User Settings" in message
        assert self.link_urls(view) == [
            f"{self.DASHBOARD}/guild/{GUILD_ID}/subscription"
        ]
        # Logged as well as shown: if this turns out to be common, the copy is
        # unclear, and there has to be something to notice that in.
        assert any("billed twice" in record.message for record in caplog.records)

    def test_nobody_is_auto_cancelled_or_refunded(self, enforced, stripe_on,
                                                  monkeypatch):
        """The command warns. It does not act.

        Code that cancels a subscription and moves money without a person
        deciding is a category of bug that costs real money in the wrong
        direction. The failure mode of a warning is a delayed refund request,
        which is recoverable.
        """
        make_server()
        store_subscription()
        self.reply(make_interaction([FakeEntitlement()]), monkeypatch)
        with bot.session_scope() as session:
            row = session.query(bot.StripeSubscription).one()
            assert row.status == "active"
            assert row.cancel_at_period_end is False

    def test_an_unsubscribed_server_is_offered_both_paths(self, enforced,
                                                          stripe_on, monkeypatch):
        make_server()
        self.not_grandfathered()
        message, view = self.reply(make_interaction(), monkeypatch)
        assert "two ways to buy it" in message
        assert self.link_urls(view) == [
            f"{self.DASHBOARD}/guild/{GUILD_ID}/subscription"
        ]

    def test_without_a_dashboard_url_the_website_button_is_absent(
        self, enforced, stripe_on, monkeypatch
    ):
        """The copy still mentions the website; only the shortcut goes.

        A link button with no scheme is a 400 that fails the whole
        interaction, so degrading to no button is the safe direction.
        """
        make_server()
        message, view = self.reply(make_interaction(), monkeypatch, dashboard_url=None)
        assert "website" in message
        assert self.link_urls(view) == []

    def test_with_stripe_off_a_card_row_changes_nothing(self, enforced,
                                                        monkeypatch):
        """The kill switch, from the admin's side of the screen.

        `stripe_on` is deliberately absent here. A row can exist in the table
        -- written by a test, or left behind by an earlier deploy -- and with
        the switch off it must not reach a single word of the reply.
        """
        make_server()
        self.not_grandfathered()
        store_subscription()
        message, view = self.reply(make_interaction(), monkeypatch)
        assert "two ways to buy it" in message  # the unsubscribed message
        assert "VRCVerify website" not in message


# ---------------------------------------------------------------
# Trial eligibility (#88 phase 8)
# ---------------------------------------------------------------
class TestTheEverPaidLedger:
    """What makes a free trial a once-per-server thing.

    A live subscription is easy to see from either platform. The hard case is
    the one this table exists for: a server that paid, cancelled, and came
    back. Discord leaves nothing behind when an entitlement ends, so without a
    ledger that server is indistinguishable from a brand new one -- and a free
    month is available again, once per cancellation, forever.
    """

    def test_a_row_is_written_once_and_is_idempotent(self):
        assert bot.record_entitlement_seen(GUILD_ID) is True
        assert bot.record_entitlement_seen(GUILD_ID) is False
        with bot.session_scope() as session:
            assert session.query(bot.PremiumEntitlementSeen).count() == 1

    def test_a_failed_write_is_swallowed(self, monkeypatch):
        """Bookkeeping must not take down the event handler it hangs off."""
        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "session_scope", boom)
        assert bot.record_entitlement_seen(GUILD_ID) is False

    def test_a_lapsed_card_subscription_still_counts_as_having_paid(self):
        """No second ledger for Stripe: the rows already outlive the plan."""
        store_subscription(status="canceled", current_period_end=in_days(-40))
        assert bot.has_ever_paid(GUILD_ID) is True

    def test_a_server_with_no_history_has_never_paid(self):
        assert bot.has_ever_paid(GUILD_ID) is False


class TestTrialEligibility:
    def test_a_brand_new_server_is_eligible(self, stripe_on):
        make_server()
        assert bot.trial_eligible(GUILD_ID) is True

    def test_a_grandfathered_server_is_eligible(self, stripe_on):
        """Settled 2026-08-17: never paid means eligible, full stop.

        Grandfathering is a row-id comparison that knows nothing about money,
        and reading it into a payment decision would couple the two in the
        direction the grandfathering rule exists to prevent. A grandfathered
        server has never paid us, so it gets the same offer as anyone else who
        never has.
        """
        make_server(row_id=1)
        with bot.session_scope() as session:
            session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=5000))
        assert bot.is_grandfathered(GUILD_ID) is True
        assert bot.trial_eligible(GUILD_ID) is True

    def test_a_past_discord_subscriber_is_not_eligible(self, stripe_on):
        make_server()
        bot.record_entitlement_seen(GUILD_ID)
        assert bot.trial_eligible(GUILD_ID) is False

    def test_a_past_card_subscriber_is_not_eligible(self, stripe_on):
        """The whole point: cancelling must not restore the offer."""
        make_server()
        store_subscription(status="canceled", current_period_end=in_days(-9))
        assert bot.trial_eligible(GUILD_ID) is False

    def test_eligibility_is_per_guild(self, stripe_on):
        make_server()
        bot.record_entitlement_seen(GUILD_ID)
        assert bot.trial_eligible(OTHER_GUILD_ID) is True

    def test_a_failed_read_refuses_the_trial(self, stripe_on, monkeypatch):
        """Fails CLOSED, and the opposite way from guild_has_premium.

        That one fails open because an outage must not switch off somebody who
        paid. Here the cost of being wrong runs the other way: a free month
        handed to a server that has already had one, repeatably, for as long as
        the database is unhappy. Do not "fix" this to match the other one.
        """
        def boom(guild_id):
            raise RuntimeError("database is down")

        monkeypatch.setattr(bot, "has_ever_paid", boom)
        assert bot.trial_eligible(GUILD_ID) is False

    def test_with_stripe_off_nobody_is_eligible(self):
        """No switch, no trial -- and no query either."""
        def boom():
            raise AssertionError("the table must not be read with Stripe off")

        make_server()
        assert bot.trial_eligible(GUILD_ID) is False

    def test_the_settings_payload_carries_the_answer(self, enforced, stripe_on):
        """Decided by the bot, never derived by the website."""
        make_server()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["trial_eligible"] is True

        bot.record_entitlement_seen(GUILD_ID)
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["stripe"]["trial_eligible"] is False


class TestTheEntitlementSweep:
    """The backfill, without which everything that ended before this shipped
    looks like a server that has never paid."""

    def sweep_over(self, monkeypatch, *guild_ids):
        entitlements = [
            SimpleNamespace(guild_id=gid, sku_id=SKU_ID) for gid in guild_ids
        ]

        def _entitlements(**kwargs):
            async def generate():
                for item in entitlements:
                    yield item

            return generate()

        monkeypatch.setattr(bot.bot, "entitlements", _entitlements)
        return run(bot.sweep_entitlement_history())

    def test_it_records_guilds_whose_entitlement_has_already_ended(
        self, enforced, stripe_on, monkeypatch
    ):
        """`exclude_ended` defaults to False, which is the entire point.

        A live entitlement is visible everywhere already. An ended one is the
        case the trial gate needs and the only one nothing else remembers.
        """
        make_server()
        assert self.sweep_over(monkeypatch, GUILD_ID) == 1
        assert bot.trial_eligible(GUILD_ID) is False

    def test_running_it_again_records_nothing_new(
        self, enforced, stripe_on, monkeypatch
    ):
        """Every boot, not once -- so a gap opened by downtime self-heals."""
        self.sweep_over(monkeypatch, GUILD_ID)
        assert self.sweep_over(monkeypatch, GUILD_ID) == 0

    def test_it_does_nothing_while_the_feature_is_off(self, monkeypatch):
        def boom(**kwargs):
            raise AssertionError("the sweep must not call Discord while off")

        monkeypatch.setattr(bot.bot, "entitlements", boom)
        assert run(bot.sweep_entitlement_history()) == 0

    def test_a_failed_sweep_is_not_a_failed_boot(
        self, enforced, stripe_on, monkeypatch
    ):
        def boom(**kwargs):
            raise RuntimeError("Discord is unhappy")

        monkeypatch.setattr(bot.bot, "entitlements", boom)
        assert run(bot.sweep_entitlement_history()) == 0

    def test_it_stops_at_the_cap_and_says_so(
        self, enforced, stripe_on, monkeypatch, caplog
    ):
        """Overflow means the ledger is incomplete, which is worth shouting
        about: some server past the cap gets a second free trial."""
        monkeypatch.setattr(bot, "ENTITLEMENT_SWEEP_MAX", 2)
        with caplog.at_level("ERROR"):
            self.sweep_over(monkeypatch, "1", "2", "3", "4")
        assert any("INCOMPLETE" in record.message for record in caplog.records)
