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

    def test_a_cancellation_of_a_superseded_subscription_is_ignored(self, stripe_on):
        """The ordering guard alone would wave this through, and it is newer.

        A server that bought a second subscription and then cancelled the first
        would otherwise lose the one it is still paying for.
        """
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
        result = run(
            bot.write_dashboard_stripe_subscription(
                GUILD_ID,
                make_event(
                    event_id="evt_cancel_first",
                    subscription_id=SUBSCRIPTION,
                    status="canceled",
                    current_period_end=in_days(-1),
                    event_created=in_days(2),
                ),
            )
        )
        assert result["applied"] is False
        assert result["reason"] == "superseded_subscription"
        assert bot.stripe_active(GUILD_ID) is True

    def test_a_new_paid_subscription_does_take_over(self, stripe_on):
        run(bot.write_dashboard_stripe_subscription(GUILD_ID, make_event()))
        result = run(
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
        assert result["applied"] is True
        with bot.session_scope() as session:
            row = session.query(bot.StripeSubscription).one()
            assert row.stripe_subscription_id == "sub_SECOND"
            assert row.price_id == PRICE_YEARLY


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

    def test_no_customer_id_ever_crosses_the_wire(self, enforced, stripe_on):
        """The website has no use for it, and shipping it is how it ends up in
        a log on the internet-facing box."""
        make_server()
        store_subscription()
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert CUSTOMER not in repr(payload)

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
