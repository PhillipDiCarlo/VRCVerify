"""Unit tests for the Subscriptions page (issue #88, step 4).

The page that takes money, so the tests are mostly about the ways it can say
something untrue about somebody's subscription:

- a failed read must never render as "not subscribed", because "not
  subscribed" beside a Buy button is how a paying customer buys a second one
- a server that already pays must be shown no way to pay again, in the page AND
  in the route, because a hand-crafted POST is not a click
- the price is never taken from the form on trust: the browser names a price
  id, and the route accepts it only after finding it among the product's
  ACTIVE prices, fetched from Stripe on that request. "We could not check"
  refuses rather than allowing
- "renews on" and "ends on" are different promises and the page must not
  confuse them
- an unrecognised price is a labelling problem, never a payment problem

`subscription_view` is pure, so every state below is built without Flask, a
clock or a network, which is the whole point of that split.
"""

import dataclasses
import json
import time

import re as _re

import pytest

pytest.importorskip("flask")

from dashboard import subscription_view  # noqa: E402
from dashboard.app import SESSION_COOKIE, create_app  # noqa: E402
from dashboard.botapi import BotAPIError  # noqa: E402
from dashboard.config import DashboardConfig  # noqa: E402
from dashboard.sessions import SessionStore  # noqa: E402
from dashboard.stripe_api import StripeAPIError  # noqa: E402

APP_ID = "1335738139825799188"
SKU_ID = "1533325058573865051"
GUILD = "111111111111"
ACTOR = "424242424242"

SIGNING_KEY = "s" * 48
SECRET_KEY = "k" * 48
PRICE_MONTHLY = "price_monthly"
PRICE_SIX = "price_six"
PRICE_YEARLY = "price_yearly"
PRODUCT_ID = "prod_PREMIUM"
PORTAL_CONFIGURATION = "bpc_PREMIUM"

# Stripe price objects as `list_prices` returns them, and the plans they become.
# Metadata drives label, order and saving; the yearly one deliberately carries a
# trial so the trial path is exercised by the default fixture rather than only
# by the test that is about trials.
PRICES = [
    {
        "id": PRICE_YEARLY,
        "unit_amount": 4799,
        "currency": "usd",
        "recurring": {"interval": "year", "interval_count": 1},
        "metadata": {"label": "12 months", "order": "3", "saving": "Save about 20%",
                     "trial_days": "14"},
    },
    {
        "id": PRICE_MONTHLY,
        "unit_amount": 499,
        "currency": "usd",
        "recurring": {"interval": "month", "interval_count": 1},
        "metadata": {"label": "Monthly", "order": "1"},
    },
    {
        "id": PRICE_SIX,
        "unit_amount": 2699,
        "currency": "usd",
        "recurring": {"interval": "month", "interval_count": 6},
        "metadata": {"label": "6 months", "order": "2", "saving": "Save about 10%",
                     "highlight": "1"},
    },
]
PLANS = subscription_view.plans_from_prices(PRICES)
CUSTOMER = "cus_TEST"

FUTURE = "2026-11-03T00:00:00+00:00"
PAST = "2026-02-03T00:00:00+00:00"


# -------------------------------------------------------------------
# Payloads
# -------------------------------------------------------------------
def payload(
    *,
    enforced=True,
    premium=False,
    discord=False,
    grandfathered=False,
    stripe_enabled=True,
    active=False,
    status=None,
    price_id=PRICE_MONTHLY,
    period_end=FUTURE,
    cancel=False,
    active_count=0,
    customer_id=CUSTOMER,
    trial_eligible=True,
):
    return {
        "guild_id": GUILD,
        "premium": {
            "enforced": enforced,
            "premium": premium,
            "grandfathered": grandfathered,
            "sku_id": SKU_ID,
            "discord": discord,
        },
        "stripe": {
            "enabled": stripe_enabled,
            "active": active,
            "status": status,
            "price_id": price_id,
            "current_period_end": period_end,
            "cancel_at_period_end": cancel,
            "active_count": active_count,
            "customer_id": customer_id,
            "trial_eligible": trial_eligible,
        },
        "fields": {},
    }


def build(settings, **kwargs):
    kwargs.setdefault("application_id", APP_ID)
    kwargs.setdefault("plans", PLANS)
    return subscription_view.build(settings, **kwargs)


# -------------------------------------------------------------------
# The states
# -------------------------------------------------------------------
class TestTheStates:
    def test_a_failed_read_is_its_own_state(self):
        """Never "not subscribed", and this is the most important line here.

        The API turns a failed read into None. Rendering that as "you have no
        plan" -- next to three Buy buttons -- is how somebody who is already
        paying buys a second subscription.
        """
        page = build(None)
        assert page.state == "unavailable"
        assert page.offers_card is False
        assert page.offers_portal is False

    def test_the_tier_being_off_sells_nothing(self):
        """Every gate answers "allowed", so there is nothing to sell."""
        page = build(payload(enforced=False))
        assert page.state == "off"
        assert page.offers_card is False

    def test_not_subscribed_offers_both_paths(self):
        page = build(payload())
        assert page.state == "none"
        assert page.offers_card is True
        assert [plan.label for plan in page.plans] == [
            "Monthly",
            "6 months",
            "12 months",
        ]
        assert page.discord_command is True
        assert page.store_url.endswith(f"/{APP_ID}/store/{SKU_ID}")

    def test_subscribed_by_card(self):
        page = build(
            payload(premium=True, active=True, status="active", price_id=PRICE_SIX),
        )
        assert page.state == "stripe"
        assert page.plan_label == "6 months"
        assert page.renews_on == "3 November 2026"
        assert page.ends_on is None
        assert page.offers_portal is True
        assert page.offers_card is False

    def test_subscribed_by_discord(self):
        """No card buttons at all. Offering one to a server that already pays
        is precisely how it ends up paying twice."""
        page = build(payload(premium=True, discord=True))
        assert page.state == "discord"
        assert page.offers_card is False
        assert page.offers_portal is False
        assert page.on_discord is True

    def test_past_due_is_its_own_state(self):
        """Premium is still on while Stripe retries. Not an error, not silence.

        A boolean could not express this, which is why the bot stores Stripe's
        status verbatim.
        """
        page = build(
            payload(premium=True, active=True, status="past_due"),
        )
        assert page.state == "past_due"
        assert page.premium is True
        assert page.offers_portal is True
        assert page.offers_card is False

    def test_paying_on_both_platforms_is_warned_not_fixed(self):
        page = build(
            payload(premium=True, discord=True, active=True, status="active",
                    active_count=1),
        )
        assert page.state == "both"
        assert page.premium is True
        assert page.on_discord is True
        # Premium stays granted: being double-billed must not also break
        # something.
        assert page.offers_card is False
        assert page.offers_portal is True

    def test_two_card_subscriptions_is_also_double_billing(self):
        """The case the table can count directly, and only because it keys by
        subscription rather than by guild."""
        page = build(
            payload(premium=True, active=True, status="active", active_count=2),
        )
        assert page.state == "both"
        assert page.card_count == 2
        assert page.on_discord is False

    def test_grandfathered_and_unsubscribed(self):
        page = build(payload(grandfathered=True))
        assert page.state == "none"
        assert page.grandfathered is True
        assert page.offers_card is True

    def test_grandfathering_rides_alongside_every_state(self):
        """It is orthogonal, not an eighth state -- the reassurance is the same
        whichever way the server is or is not paying."""
        page = build(payload(premium=True, active=True, status="active",
                             grandfathered=True))
        assert page.state == "stripe"
        assert page.grandfathered is True


class TestFoundByProbing:
    """Findings from an adversarial pass over the page, before it merged."""

    def test_just_bought_offers_no_way_to_buy_again(self):
        """The worst moment for a Buy button, and it was there.

        Stripe bounces the browser back the instant checkout completes, but the
        webhook that makes the subscription real may not have landed -- so the
        payload still says "not subscribed" and the page rendered three Buy
        buttons directly under a thank-you message, to somebody who had
        demonstrably just paid.
        """
        page = build(payload(), just_bought=True)
        assert page.state == "pending"
        assert page.offers_card is False
        assert page.plans == ()

    def test_just_bought_never_claims_the_payment_succeeded(self):
        """The redirect is a hint. Only the webhook is evidence."""
        page = build(payload(), just_bought=True)
        assert page.premium is False

    def test_a_confirmed_subscription_ignores_the_hint(self):
        """Once the webhook HAS landed, the real state wins."""
        page = build(
            payload(premium=True, active=True, status="active"),
            just_bought=True,
        )
        assert page.state == "stripe"

    @pytest.mark.parametrize(
        "settings",
        [
            {},
            {"premium": {}},
            {"premium": {"premium": True}},
            {"stripe": {"enabled": True}},
        ],
    )
    def test_a_payload_without_a_premium_block_is_unavailable(self, settings):
        """Not "everything is free".

        A missing `enforced` read as falsy rendered "every feature is available
        at no charge" to a server that may well be paying -- the same class of
        lie as rendering a failed read as "not subscribed".
        """
        assert build(settings).state == "unavailable"


class TestTheKillSwitches:
    """Two switches on two hosts, and both must be on to offer a card."""

    def test_the_bots_switch_off_hides_the_cards(self):
        page = build(payload(stripe_enabled=False))
        assert page.state == "none"
        assert page.plans == ()
        assert page.offers_card is False
        # The Discord path is still offered -- that is the whole page in this
        # configuration, and it is a complete answer rather than a stub.
        assert page.discord_command is True

    def test_the_dashboards_switch_off_hides_the_cards(self):
        page = build(payload(), stripe_configured=False)
        assert page.offers_card is False

    def test_offers_card_needs_plans_and_not_just_the_state(self):
        """Found by a failing template test rather than by reading the code.

        With Stripe off the state is still `none` -- the server genuinely has
        no subscription -- so checking only the state rendered an empty "Pay by
        card" section headed by a promise the page could not keep.
        """
        page = build(payload(stripe_enabled=False))
        assert page.state == "none" and not page.plans
        assert page.offers_card is False


class TestDatesAndLabels:
    def test_cancelled_says_ends_on_not_renews_on(self):
        """Different promises. Saying the wrong one is lying about money."""
        page = build(
            payload(premium=True, active=True, status="active", cancel=True),
        )
        assert page.ends_on == "3 November 2026"
        assert page.renews_on is None

    def test_cancelled_outright_also_says_ends_on(self):
        """The regression, from production, 2026-08-18.

        A subscription cancelled in the Stripe dashboard comes back
        `status=canceled` with `cancel_at_period_end` still FALSE -- there is
        no future period end left to cancel at. Reading only the flag put it on
        the "renews" branch, so the page told a real customer they would be
        billed again on a date nothing was going to bill them.

        Both halves were individually right, which is why no unit test caught
        it and cancelling a real subscription did.
        """
        page = build(
            payload(premium=True, active=True, status="canceled", cancel=False),
        )
        assert page.ends_on == "3 November 2026"
        assert page.renews_on is None

    def test_an_ordinary_active_subscription_still_says_renews(self):
        """The other side of the fix. Widening `cancelling` must not make every
        subscription look cancelled."""
        page = build(payload(premium=True, active=True, status="active"))
        assert page.renews_on == "3 November 2026"
        assert page.ends_on is None

    def test_past_due_still_says_renews_rather_than_ends(self):
        """`past_due` is Stripe still trying, not Stripe giving up. Telling
        someone it ends on a date invites them to stop paying attention to a
        payment that can still succeed."""
        page = build(payload(premium=True, active=True, status="past_due"))
        assert page.renews_on == "3 November 2026"
        assert page.ends_on is None

    def test_a_lapsed_subscription_says_when_it_ended(self):
        """The row survives on purpose, so the page can say this rather than
        pretend the server was never a customer."""
        page = build(
            payload(status="canceled", period_end=PAST, price_id=PRICE_YEARLY),
        )
        assert page.state == "none"
        assert page.ended_on == "3 February 2026"
        assert page.last_plan_label == "12 months"

    def test_an_unknown_price_still_reads_as_premium(self):
        """A price absent from the table is a LABEL problem, never a payment
        problem. Degrading to "not subscribed" would switch off a paying
        customer over a missing environment variable."""
        page = build(
            payload(premium=True, active=True, status="active",
                    price_id="price_WHO_KNOWS"),
        )
        assert page.state == "stripe"
        assert page.plan_label == subscription_view.UNKNOWN_PLAN_LABEL
        assert page.premium is True

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", 12345])
    def test_an_unreadable_date_is_omitted_not_guessed(self, raw):
        page = build(
            payload(premium=True, active=True, status="active", period_end=raw),
        )
        assert page.renews_on is None
        assert page.ends_on is None

    def test_no_amount_is_hardcoded_in_the_module(self):
        """Stripe knows what it charges, and it is the only thing that does.

        This test used to forbid the "$" character too, back when the plans
        were three environment variables and the page had no way to learn a
        price -- so any figure on it was a second copy, maintained by hand, and
        would have gone on rendering the old number long after Stripe charged a
        new one.

        Amounts are now read from Stripe on the render that displays them, so a
        currency symbol is formatting rather than a claim. What must still
        never appear is a FIGURE: that would be the second copy again, and the
        original reasoning would apply to it unchanged.
        """
        source = open(subscription_view.__file__, encoding="utf-8").read()
        for amount in ("4.99", "26.99", "47.99", "499", "2699", "4799"):
            assert amount not in source

    def test_the_rendered_figure_follows_stripe(self, config):
        """The other half, and the one that actually proves there is no second
        copy: change what Stripe returns and the page changes with it."""
        client, _bot, stripe, _session = make_client(config)
        stripe.prices = [dict(PRICES[1], unit_amount=1234)]
        body = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "$12.34" in body
        assert "$4.99" not in body


# -------------------------------------------------------------------
# The routes
# -------------------------------------------------------------------
@pytest.fixture
def certs(tmp_path):
    for name in ("client.pem", "client.key", "ca.pem"):
        (tmp_path / name).write_text("placeholder")
    return tmp_path


@pytest.fixture
def config(tmp_path, certs):
    return DashboardConfig(
        discord_client_id=APP_ID,
        discord_client_secret="secret",
        oauth_redirect_uri="https://d.example.com/callback",
        secret_key=SECRET_KEY,
        session_db_path=str(tmp_path / "sessions.db"),
        bot_api_url="https://10.0.0.1:5002",
        bot_api_client_cert=str(certs / "client.pem"),
        bot_api_client_key=str(certs / "client.key"),
        bot_api_ca=str(certs / "ca.pem"),
        bot_api_signing_key=SIGNING_KEY.encode(),
        stripe_enabled=True,
        stripe_secret_key="sk_test_x",
        stripe_webhook_secret="whsec_" + "w" * 32,
        stripe_product_id=PRODUCT_ID,
    )


class FakeBotAPI:
    def __init__(self, settings=None):
        self._settings = settings if settings is not None else payload()
        self.error = None

    def settings(self, actor_id, guild_id):
        if self.error is not None:
            raise self.error
        return self._settings

    def admin_guild_ids(self, actor_id, guild_ids):
        return {GUILD}


class FakeStripe:
    def __init__(self):
        self.checkouts = []
        self.portals = []
        self.error = None
        # What `list_prices` hands back, and how many times it was asked. The
        # count is asserted on: the plan list is fetched on a page render AND
        # again on checkout to validate the submitted id, and a cache exists
        # precisely so that is not two calls to Stripe per purchase.
        self.prices = list(PRICES)
        self.price_calls = 0
        self.price_error = None

    def list_prices(self, product_id):
        self.price_calls += 1
        if self.price_error is not None:
            raise self.price_error
        return list(self.prices)

    def create_checkout_session(self, **kwargs):
        self.checkouts.append(kwargs)
        if self.error is not None:
            raise self.error
        return "https://checkout.stripe.com/c/pay/session_TEST"

    def create_portal_session(self, **kwargs):
        self.portals.append(kwargs)
        if self.error is not None:
            raise self.error
        return "https://billing.stripe.com/p/session/portal_TEST"


def make_client(config, settings=None):
    store = SessionStore(config.session_db_path, config.session_max_age)
    bot_api = FakeBotAPI(settings)
    stripe = FakeStripe()
    app = create_app(config, store=store, client=bot_api, stripe=stripe)
    app.config.update(TESTING=True)
    client = app.test_client()

    pending = store.begin_login("unused-state")
    session = store.complete_login(
        pending.sid,
        ACTOR,
        [{"id": GUILD, "name": "Alpha", "icon": None, "admin_hint": True}],
    )
    client.set_cookie(SESSION_COOKIE, session.sid, domain="localhost")
    return client, bot_api, stripe, session


class TestCheckout:
    def test_a_submitted_price_is_checked_against_stripe(self, config):
        """THE most likely way to get this endpoint wrong.

        The browser names a price, so the guarantee cannot come from the form
        naming a slug any more -- it comes from the id being found among the
        product's ACTIVE prices, fetched from Stripe on this request.
        """
        client, _bot, stripe, session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_SIX, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 303
        assert response.headers["Location"].startswith("https://checkout.stripe.com/")
        assert stripe.checkouts[0]["price_id"] == PRICE_SIX

    def test_a_price_stripe_does_not_offer_is_refused(self, config):
        """The $0 price made while testing, named directly in a crafted POST.

        It exists on the account; it is not on this product's active list, and
        that list is the only thing the route will accept from.
        """
        client, _bot, stripe, session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": "price_FREE_TEST", "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_an_archived_price_stops_being_sellable(self, config):
        """Retiring a plan is archiving its price, with no deploy.

        `list_prices` asks only for active prices, so a plan pulled in Stripe
        must stop being purchasable immediately -- including by someone whose
        page was rendered before it was pulled.
        """
        client, _bot, stripe, session = make_client(config)
        stripe.prices = [p for p in PRICES if p["id"] != PRICE_SIX]
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_SIX, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_a_failed_price_read_refuses_rather_than_trusting_the_form(self, config):
        """"We could not check" must never resolve to "allow".

        Falling back to the submitted id when Stripe is unreachable would turn
        a third-party outage into the exact hole the check exists to close.
        """
        client, _bot, stripe, session = make_client(config)
        stripe.price_error = StripeAPIError("boom")
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_SIX, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_the_trial_comes_from_the_price_not_the_form(self, config):
        """The buyer gets the trial the card advertised.

        A form-supplied trial length would let anyone grant themselves one.
        """
        client, _bot, stripe, session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={
                "price_id": PRICE_YEARLY,
                "trial_days": "9999",
                "csrf_token": session.csrf_token,
            },
        )
        assert response.status_code == 303
        assert stripe.checkouts[0]["trial_days"] == 14

    def test_a_price_with_no_trial_metadata_sends_none(self, config):
        client, _bot, stripe, session = make_client(config)
        client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert stripe.checkouts[0]["trial_days"] is None

    @pytest.mark.parametrize(
        "price", ["", "free", "PRICE_MONTHLY", "../price_monthly", None]
    )
    def test_an_unknown_price_buys_nothing(self, config, price):
        client, _bot, stripe, session = make_client(config)
        data = {"csrf_token": session.csrf_token}
        if price is not None:
            data["price_id"] = price
        response = client.post(f"/guild/{GUILD}/subscription/checkout", data=data)
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_no_csrf_token_buys_nothing(self, config):
        client, _bot, stripe, _session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout", data={"price_id": PRICE_MONTHLY}
        )
        assert response.status_code == 400
        assert stripe.checkouts == []

    def test_a_server_that_already_pays_cannot_check_out(self, config):
        """Pinned in the ROUTE, not only in the page.

        The page hides the button, but a hand-crafted POST is not a click, and
        this is the request that would charge somebody twice.
        """
        client, _bot, stripe, session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_a_discord_subscriber_cannot_check_out_either(self, config):
        client, _bot, stripe, session = make_client(
            config, settings=payload(premium=True, discord=True)
        )
        client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert stripe.checkouts == []

    def test_the_guild_is_bound_into_subscription_metadata(self, config):
        """It has to survive to a renewal a year from now, which
        client_reference_id does not."""
        client, _bot, stripe, session = make_client(config)
        client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert stripe.checkouts[0]["guild_id"] == GUILD
        assert stripe.checkouts[0]["actor_discord_id"] == ACTOR

    def test_stripe_being_down_apologises_rather_than_pretending(self, config):
        client, _bot, stripe, session = make_client(config)
        stripe.error = StripeAPIError("boom")
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        # Substring without the apostrophe: the notice is a template variable
        # and Jinja escapes it, so "couldn't" arrives as "couldn&#39;t".
        assert "reach Stripe just now" in page
        assert "nothing has been charged" in page.lower()

    def test_the_route_404s_when_stripe_is_switched_off(self, tmp_path, certs):
        off = DashboardConfig(
            discord_client_id=APP_ID,
            discord_client_secret="secret",
            oauth_redirect_uri="https://d.example.com/callback",
            secret_key=SECRET_KEY,
            session_db_path=str(tmp_path / "s2.db"),
            bot_api_url="https://10.0.0.1:5002",
            bot_api_client_cert=str(certs / "client.pem"),
            bot_api_client_key=str(certs / "client.key"),
            bot_api_ca=str(certs / "ca.pem"),
            bot_api_signing_key=SIGNING_KEY.encode(),
        )
        client, _bot, stripe, session = make_client(off)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 404
        assert stripe.checkouts == []


class TestPortal:
    def test_it_opens_for_a_subscriber(self, config):
        client, _bot, stripe, session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        response = client.post(
            f"/guild/{GUILD}/subscription/portal",
            data={"csrf_token": session.csrf_token},
        )
        assert response.status_code == 303
        assert response.headers["Location"].startswith("https://billing.stripe.com/")
        assert stripe.portals[0]["customer_id"] == CUSTOMER

    def test_the_customer_id_never_comes_from_the_form(self, config):
        """A customer id a browser could choose would be a portal into somebody
        else's billing."""
        client, _bot, stripe, session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        client.post(
            f"/guild/{GUILD}/subscription/portal",
            data={"csrf_token": session.csrf_token, "customer_id": "cus_SOMEONE_ELSE"},
        )
        assert stripe.portals[0]["customer_id"] == CUSTOMER

    def test_no_csrf_token_opens_nothing(self, config):
        client, _bot, stripe, _session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        response = client.post(f"/guild/{GUILD}/subscription/portal", data={})
        assert response.status_code == 400
        assert stripe.portals == []

    def test_a_server_with_no_card_subscription_gets_nothing(self, config):
        client, _bot, stripe, session = make_client(
            config, settings=payload(customer_id=None)
        )
        response = client.post(
            f"/guild/{GUILD}/subscription/portal",
            data={"csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.portals == []

    def test_the_configured_portal_is_the_one_that_opens(self, config):
        """The configuration decides which plans a customer may switch to.

        Stripe's account default is shared with every other product on the
        account, so leaving the session to pick it means this product's
        customers are offered whatever that default lists -- on an account
        with other products, prices this one never published. The gate cannot
        be moved to the bot instead: an unrecognised price id still grants
        premium, on purpose, so nothing downstream would notice.
        """
        scoped = dataclasses.replace(
            config, stripe_portal_configuration_id=PORTAL_CONFIGURATION
        )
        client, _bot, stripe, session = make_client(
            scoped, settings=payload(premium=True, active=True, status="active")
        )
        client.post(
            f"/guild/{GUILD}/subscription/portal",
            data={"csrf_token": session.csrf_token},
        )
        assert stripe.portals[0]["configuration"] == PORTAL_CONFIGURATION

    def test_an_unset_configuration_is_passed_as_none_not_empty(self, config):
        """Stripe rejects `configuration=`, so absent must not become blank.

        This is the default path -- the variable is optional and unset means
        the account default -- so getting it wrong would break every portal
        session rather than a rare one.
        """
        assert config.stripe_portal_configuration_id == ""
        client, _bot, stripe, session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        response = client.post(
            f"/guild/{GUILD}/subscription/portal",
            data={"csrf_token": session.csrf_token},
        )
        assert response.status_code == 303
        assert stripe.portals[0]["configuration"] is None


class TestTheStatusChipAndFactList:
    """#141 phase 1. The chip and the fact list, built by the view.

    WHY THESE ARE HERE AT ALL: this module is 1,400 lines and almost every
    assertion in it is against `build()`'s attributes rather than against what
    the page says. Deleting four rendered sentences during this phase changed
    nothing in the suite -- the prose was genuinely untested. These cover the
    new structure at both ends, the object and the render, so the same gap
    does not simply move.
    """

    def test_a_card_subscriber_is_marked_active(self):
        page = build(payload(premium=True, active=True, status="active"))
        assert page.chip == {"label": "Active", "tone": "ok"}

    def test_a_cancelled_one_says_cancelled_not_active(self):
        """"Active" on a subscription that stops next month is true and
        unhelpful -- and it is the same reason the fact list below says
        "Premium until" rather than "Renews"."""
        page = build(payload(premium=True, active=True, status="active", cancel=True))
        assert page.chip["label"] == "Cancelled"
        assert ("Premium until", page.ends_on) in page.facts
        assert not any(label == "Renews" for label, _ in page.facts)

    def test_a_failed_payment_is_marked(self):
        page = build(payload(premium=True, active=True, status="past_due"))
        assert page.chip == {"label": "Payment failed", "tone": "warn"}

    def test_double_billing_is_marked_and_names_both_routes(self):
        """An admin cannot go and cancel the right one without being told
        there are two."""
        page = build(payload(premium=True, discord=True, active=True,
                             status="active", active_count=1))
        assert page.chip == {"label": "Charged twice", "tone": "warn"}
        assert ("Billed through", "Card and Discord") in page.facts

    def test_a_free_server_gets_no_chip(self):
        """"Not subscribed" is not a status worth stamping, and a grey pill
        saying "Free" beside a Buy button reads as a downgrade."""
        assert build(payload()).chip is None
        assert build(payload()).facts == ()

    def test_a_failed_read_is_unknown_and_states_no_facts(self):
        page = build(None)
        assert page.chip == {"label": "Unknown", "tone": "muted"}
        assert page.facts == ()

    def test_renews_and_premium_until_are_never_both_present(self):
        """They are different promises about somebody's money."""
        for settings in (
            payload(premium=True, active=True, status="active"),
            payload(premium=True, active=True, status="active", cancel=True),
        ):
            labels = [label for label, _ in build(settings).facts]
            assert not ("Renews" in labels and "Premium until" in labels)

    def test_an_archived_price_still_names_a_plan(self):
        """Not the omission case, and worth pinning because it looks like one.

        Retiring a plan means archiving its price, and `list_prices` asks only
        for active ones -- so everyone still paying for a retired plan misses
        the lookup. `plan_label_for` degrades to "Premium" rather than to
        nothing, which is the right answer: their subscription is real and only
        the label is unknown. The fact list must show that row, not drop it.
        """
        page = build(payload(premium=True, active=True, status="active",
                             price_id="price_archived_and_gone"))
        assert ("Plan", subscription_view.UNKNOWN_PLAN_LABEL) in page.facts

    def test_a_row_with_nothing_behind_it_is_omitted_rather_than_dashed(self):
        """An empty row on a billing page invites the reader to wonder what
        should be in it, and the honest answer is that we were never told.

        Built directly rather than through `build()`, because `build()` is
        careful enough that it never produces this -- which is the point. The
        helper must not depend on that carefulness holding forever.
        """
        page = subscription_view.SubscriptionPage("stripe", plan_label=None)
        assert not any(label == "Plan" for label, _ in page.facts)
        assert ("Billed through", "Card") in page.facts


class TestTheLapsedWinback:
    """The one state on this page where the reader has already paid once."""

    @staticmethod
    def lapsed(**overrides):
        fields = dict(premium=False, active=False, status="canceled", period_end=PAST)
        fields.update(overrides)
        return build(payload(**fields))

    def test_it_carries_when_it_ended(self):
        page = self.lapsed()
        assert page.winback is not None
        assert page.winback["when"] == page.ended_on

    def test_a_server_that_never_paid_has_none(self):
        assert build(payload()).winback is None

    def test_an_archived_price_still_names_the_plan_that_ended(self):
        page = self.lapsed(price_id="price_archived_and_gone")
        assert page.winback["plan"] == subscription_view.UNKNOWN_PLAN_LABEL

    def test_the_sentence_works_with_no_plan_at_all(self):
        """`build()` sets `last_plan_label` only when the bot sent a status,
        so None is reachable. The copy has to work rather than say "your None
        subscription" -- the template branches on it."""
        page = subscription_view.SubscriptionPage(
            "off", ended_on="3 August 2026", last_plan_label=None
        )
        assert page.winback == {"when": "3 August 2026", "plan": None}


class TestThePageRenders:
    def test_a_failed_read_apologises_and_offers_nothing(self, config):
        client, bot_api, _stripe, _session = make_client(config)
        bot_api.error = BotAPIError("unavailable", 503)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "Subscribe" not in page
        assert "/subscription/checkout" not in page

    def test_the_plan_cards_say_tax_is_extra(self, config):
        """$4.99 on the card and more than $4.99 on the statement is the most
        likely complaint this page will ever produce."""
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "exclude tax" in page

    def test_the_longer_plans_are_declared_card_only(self, config):
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "card-only" in page

    def test_a_double_billed_server_is_told_where_to_cancel_both(self, config):
        client, _bot, _stripe, _session = make_client(
            config,
            settings=payload(premium=True, discord=True, active=True,
                             status="active"),
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "Manage billing" in page
        assert "/vrcverify_subscription" in page
        assert "won't cancel either one for you" in page

    def test_a_discord_subscriber_is_shown_no_card_form(self, config):
        client, _bot, _stripe, _session = make_client(
            config, settings=payload(premium=True, discord=True)
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "/subscription/checkout" not in page

    def test_the_status_chip_and_facts_reach_the_page(self, config):
        """The render half. Deleting four rendered sentences in #141 phase 1
        changed nothing in this suite, so the structure that replaced them is
        asserted at both ends rather than only on the object."""
        client, _bot, _stripe, _session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "sub-chip-ok" in page
        assert "Active" in page
        assert "sub-facts" in page
        assert "Billed through" in page

    def test_the_chip_is_not_the_settings_page_lock_badge(self, config):
        """`.badge.premium` means "your plan cannot use this" two clicks away
        (#136). One component with two opposite meanings is worse than two
        components."""
        client, _bot, _stripe, _session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        main = page.split("<main>", 1)[1].split("</main>", 1)[0]
        assert "badge premium" not in main

    def test_the_facts_are_not_also_repeated_in_prose(self, config):
        """A page that states the same thing twice invites the reader to check
        whether the two agree. The renewal date belongs to the fact list."""
        client, _bot, _stripe, _session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        main = page.split("<main>", 1)[1].split("</main>", 1)[0]
        assert main.count("3 November 2026") == 1

    def test_a_lapsed_server_gets_a_winback_not_a_muted_line(self, config):
        client, _bot, _stripe, _session = make_client(
            config,
            settings=payload(premium=False, active=False, status="canceled",
                             period_end=PAST),
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "sub-winback" in page
        assert "Your Premium ended on" in page
        # And it is still offered something to buy -- a win-back with no way
        # to act on it is just a status line with a border.
        assert "/subscription/checkout" in page

    def test_a_free_server_that_never_paid_sees_no_winback(self, config):
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "sub-winback" not in page
        assert "sub-chip" not in page

    def test_the_purchase_card_is_not_the_settings_footnote(self, config):
        """#158. `settings.html` uses `<p class="muted plan">` for an italic
        grey footnote, and the purchase card declared no colour, size, style or
        margin -- so it inherited all four. The PRICE rendered italic and
        --muted on the page that takes money, and the Subscribe label was
        italic too.

        Asserting the class name is what makes this a regression test rather
        than a restatement: the two rules cannot collide again while the card
        is not called `.plan`.
        """
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert 'class="plan-card' in page
        assert 'class="plan ' not in page and 'class="plan"' not in page

    def test_the_three_plans_are_declared_one_product(self, config):
        """Three cards at rising prices is the shape of a tier comparison, and
        a reader who scans will take the dearest to unlock more. It does not.

        Layout cannot fix that here: the reference that solves it best
        normalises every card to one unit, which needs a figure derived from
        the charge -- and this repo computes no amounts, because a second copy
        of a price on a page about money is a second thing to be wrong. So the
        claim is stated in words, above the cards.
        """
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "One Premium, three ways to pay for it" in page
        assert "exactly the same features" in page

    def test_still_no_amount_is_computed_anywhere(self, config):
        """The rule that ruled out the better layout. Every figure on this page
        comes from the Stripe read that rendered it."""
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        shown = {"$4.99", "$26.99", "$47.99"}
        found = set(_re.findall(r"\$\d+\.\d\d", page))
        assert found <= shown, f"a price on the page that Stripe did not send: {found - shown}"

    def test_the_trial_note_is_its_own_element(self, config):
        """It can start appearing from a `trial_days` edit on a Stripe price
        with no code and no deploy -- so it has to look deliberate the first
        time it shows up, on a card nobody was watching when it did."""
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert 'class="plan-trial"' in page
        assert "free trial" in page

    def test_an_ineligible_server_is_shown_no_trial(self, config):
        """The card asks `page.trial_note_for(plan)`, never `plan.trial_note`:
        the plan knows how long a trial would be, only the page knows whether
        THIS server may have one. A card advertising a trial the checkout then
        declines is a promise broken while somebody is typing a card number."""
        client, _bot, _stripe, _session = make_client(
            config, settings=payload(trial_eligible=False)
        )
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "plan-trial" not in page
        assert "free trial" not in page

    def test_no_inline_style_reaches_the_page(self, config):
        """`style-src 'self'` drops inline styles SILENTLY, so a colour written
        as style="" would simply not apply and nothing would say so."""
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "style=" not in page.lower()

    def test_ko_fi_stays_off_this_page(self, config):
        """Donations and subscriptions in the same place make both read as
        optional -- the reasoning already in bot.py, confirmed for the site."""
        client, _bot, _stripe, _session = make_client(config)
        page = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "ko-fi" not in page.lower()


class TestCheckoutFormShape:
    """What `StripeClient` actually puts on the wire.

    Every other test in this file uses `FakeStripe`, which accepts any kwargs
    and hands back a URL. That is the right shape for testing the route's
    decisions -- who may check out, which slug maps to which price -- and it is
    completely blind to whether the request Stripe receives is one Stripe will
    accept.

    It was blind to exactly that on 2026-08-15. The form carried
    `customer_update[address]=auto` without a `customer`, which Stripe rejects
    outright:

        400 invalid_request_error -- `customer_update` can only be used with
        `customer`.

    So every live checkout failed, the page said "We couldn't reach Stripe just
    now", and the suite stayed green because nothing had ever looked at the
    form. These tests look at the form.
    """

    def _post_form(self):
        """Capture the form for one checkout, without a network."""
        from dashboard.stripe_api import StripeClient

        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"url": "https://checkout.stripe.com/c/pay/cs_test_X"}

        def fake_post(url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["form"] = list(data)
            return FakeResponse()

        client = StripeClient("sk_test_x")
        client._session.post = fake_post
        returned = client.create_checkout_session(
            price_id="price_TEST",
            guild_id="987654321",
            actor_discord_id="77",
            success_url="https://dash.example/guild/987654321/subscription?bought=1",
            cancel_url="https://dash.example/guild/987654321/subscription",
        )
        captured["returned"] = returned
        return captured

    def test_customer_update_is_never_sent(self):
        """The regression. Stripe 400s on `customer_update` without `customer`.

        This session deliberately passes no `customer`: in subscription mode
        Stripe creates one and saves the collected billing address to it by
        itself, so there is nothing for `customer_update` to do. Re-adding it
        breaks every checkout while reading perfectly sensibly in review.
        """
        form = self._post_form()["form"]
        keys = [key for key, _ in form]
        assert not any(key.startswith("customer_update") for key in keys), (
            "customer_update without customer is a 400 from Stripe -- "
            f"got {[k for k in keys if k.startswith('customer_update')]}"
        )

    def test_no_customer_is_sent_either(self):
        """The other half of the pair, pinned so the two stay consistent.

        If a `customer` is ever added here, `customer_update` becomes legal --
        and the test above becomes the wrong assertion rather than a broken
        one. Failing here is the signal to revisit it deliberately.
        """
        keys = [key for key, _ in self._post_form()["form"]]
        assert "customer" not in keys

    def test_the_fields_automatic_tax_depends_on_are_present(self):
        """`automatic_tax` without an address is a different 400.

        Stripe cannot decide a rate without one, so dropping
        `billing_address_collection` trades this bug for its twin.
        """
        form = dict(self._post_form()["form"])
        assert form["automatic_tax[enabled]"] == "true"
        assert form["billing_address_collection"] == "required"

    def test_the_guild_rides_on_subscription_metadata(self):
        """`client_reference_id` is gone by the first renewal; the metadata is
        not. The binding has to survive to a renewal a year from now."""
        form = dict(self._post_form()["form"])
        assert form["subscription_data[metadata][guild_id]"] == "987654321"
        assert form["subscription_data[metadata][actor_discord_id]"] == "77"
        assert form["client_reference_id"] == "987654321"

    def test_it_is_a_subscription_for_the_price_it_was_given(self):
        form = dict(self._post_form()["form"])
        assert form["mode"] == "subscription"
        assert form["line_items[0][price]"] == "price_TEST"
        assert form["line_items[0][quantity]"] == "1"

    def test_it_posts_to_checkout_sessions_and_returns_the_url(self):
        captured = self._post_form()
        assert captured["url"].endswith("/checkout/sessions")
        assert captured["returned"] == "https://checkout.stripe.com/c/pay/cs_test_X"


class TestThePortalSessionForm:
    """The same discipline as the checkout form above, for the same reason.

    The portal session is where "which plans may this customer switch to" is
    decided, and it is decided by a field that is easy to leave off -- the
    session succeeds without it, opens a working portal, and offers whatever
    the shared account default lists.
    """

    def _post_form(self, **kwargs):
        from dashboard.stripe_api import StripeClient

        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"url": "https://billing.stripe.com/p/session/bps_X"}

        def fake_post(url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["form"] = list(data)
            return FakeResponse()

        client = StripeClient("sk_test_x")
        client._session.post = fake_post
        captured["returned"] = client.create_portal_session(
            customer_id="cus_TEST",
            return_url="https://dash.example/guild/1/subscription",
            **kwargs,
        )
        return captured

    def test_the_configuration_is_sent_when_given(self):
        form = dict(self._post_form(configuration="bpc_TEST")["form"])
        assert form["configuration"] == "bpc_TEST"

    def test_the_field_is_absent_rather_than_empty_when_not_given(self):
        """Stripe rejects `configuration=`, so this cannot be sent blank."""
        keys = [key for key, _ in self._post_form()["form"]]
        assert "configuration" not in keys

    def test_none_is_the_same_as_not_given(self):
        """The route passes `... or None`, so this is the live default path."""
        keys = [key for key, _ in self._post_form(configuration=None)["form"]]
        assert "configuration" not in keys

    def test_a_malformed_configuration_never_reaches_stripe(self):
        with pytest.raises(StripeAPIError):
            self._post_form(configuration="bpc_x/../../v1/customers")

    def test_it_posts_to_portal_sessions_and_returns_the_url(self):
        captured = self._post_form()
        assert captured["url"].endswith("/billing_portal/sessions")
        assert captured["returned"] == "https://billing.stripe.com/p/session/bps_X"


class TestPlansFromPrices:
    """Stripe's price objects becoming plan cards.

    Pure, so none of this needs Flask or a network. Every case here is a price
    somebody can create in the Stripe dashboard in about four seconds, which is
    the point: the plan list is now user input from a web form, and the page
    that renders it takes money.
    """

    def one(self, **price):
        plans = subscription_view.plans_from_prices([price])
        return plans[0] if plans else None

    def test_metadata_drives_the_card(self):
        plan = self.one(
            id="price_x",
            recurring={"interval": "month", "interval_count": 1},
            metadata={"label": "Founder", "saving": "Half price", "trial_days": "7",
                      "order": "1"},
        )
        assert (plan.label, plan.saving, plan.trial_days) == ("Founder", "Half price", 7)
        assert plan.trial_note == "7-day free trial"

    def test_a_price_with_no_metadata_still_renders(self):
        """A plan created in a hurry must be sellable, not a blank card.

        This is what keeps the metadata presentational rather than
        configuration the page cannot work without.
        """
        plan = self.one(id="price_x", recurring={"interval": "month", "interval_count": 6})
        assert plan.label == "6 months"
        assert plan.saving is None
        assert plan.trial_days is None
        assert plan.trial_note is None

    @pytest.mark.parametrize(
        "interval,count,expected",
        [
            ("month", 1, "Monthly"),
            ("month", 3, "3 months"),
            ("year", 1, "12 months"),
            ("week", 2, "Every 2 weeks"),
            ("day", 1, "Every day"),
        ],
    )
    def test_labels_derive_from_the_interval(self, interval, count, expected):
        plan = self.one(
            id="price_x", recurring={"interval": interval, "interval_count": count}
        )
        assert plan.label == expected

    @pytest.mark.parametrize("raw", ["seven", "", "-3", "0", "3.5", None, True, {}])
    def test_a_junk_trial_renders_without_one(self, raw):
        """Metadata is free text typed into a web form.

        A typo must cost the trial, never the page -- 500ing here would take
        the subscription page down for every server over one bad character on
        one price.
        """
        plan = self.one(
            id="price_x",
            recurring={"interval": "month", "interval_count": 1},
            metadata={"trial_days": raw},
        )
        assert plan.trial_days is None

    def test_a_price_with_no_id_is_not_a_plan(self):
        """Everything else degrades to a default; an id cannot, because it is
        what the form submits and what checkout looks up."""
        assert subscription_view.plans_from_prices([{"recurring": {}}]) == ()

    def test_order_metadata_wins_over_the_interval(self):
        plans = subscription_view.plans_from_prices([
            {"id": "price_a", "recurring": {"interval": "year", "interval_count": 1},
             "metadata": {"order": "1"}},
            {"id": "price_b", "recurring": {"interval": "month", "interval_count": 1},
             "metadata": {"order": "2"}},
        ])
        assert [p.price_id for p in plans] == ["price_a", "price_b"]

    def test_without_order_the_shorter_term_comes_first(self):
        plans = subscription_view.plans_from_prices([
            {"id": "price_year", "recurring": {"interval": "year", "interval_count": 1}},
            {"id": "price_month", "recurring": {"interval": "month", "interval_count": 1}},
            {"id": "price_six", "recurring": {"interval": "month", "interval_count": 6}},
        ])
        assert [p.price_id for p in plans] == ["price_month", "price_six", "price_year"]

    def test_equal_plans_keep_a_stable_order(self):
        """Cards that move between two renders are cards somebody clicks by
        muscle memory and gets wrong."""
        prices = [
            {"id": "price_b", "recurring": {"interval": "month", "interval_count": 1}},
            {"id": "price_a", "recurring": {"interval": "month", "interval_count": 1}},
        ]
        first = [p.price_id for p in subscription_view.plans_from_prices(prices)]
        second = [p.price_id for p in subscription_view.plans_from_prices(prices[::-1])]
        assert first == second == ["price_a", "price_b"]

    @pytest.mark.parametrize("junk", [None, "prices", 42, [None, 7, "x"]])
    def test_junk_in_place_of_prices_is_no_plans_not_a_crash(self, junk):
        assert subscription_view.plans_from_prices(junk) == ()

    def test_an_archived_price_loses_only_its_label(self):
        """Retiring a plan must not unsubscribe the people still on it.

        `list_prices` asks for active prices only, so everyone paying for a
        retired plan lands on the generic label -- and that is all that may
        happen to them.
        """
        page = build(
            payload(premium=True, active=True, status="active",
                    price_id="price_RETIRED"),
        )
        assert page.state == "stripe"
        assert page.premium is True
        assert page.plan_label == subscription_view.UNKNOWN_PLAN_LABEL


class TestPlansUnavailable:
    """Stripe not answering is not the same as Stripe selling nothing."""

    def test_the_page_says_so_rather_than_implying_nothing_is_for_sale(self, config):
        client, _bot, stripe, _session = make_client(config)
        stripe.price_error = StripeAPIError("boom")
        body = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "can't load the plans" in body
        assert "Subscribe" not in body

    def test_the_rest_of_the_page_is_unaffected(self, config):
        """The subscription facts come from the bot and are still true.

        A Stripe outage may cost the Buy buttons and nothing else.
        """
        client, _bot, stripe, _session = make_client(
            config, settings=payload(premium=True, active=True, status="active")
        )
        stripe.price_error = StripeAPIError("boom")
        response = client.get(f"/guild/{GUILD}/subscription")
        assert response.status_code == 200
        assert "Manage billing" in response.data.decode()

    def test_an_empty_price_list_is_not_an_apology(self, config):
        """Stripe answering "nothing" is a true answer the page may render."""
        client, _bot, stripe, _session = make_client(config)
        stripe.prices = []
        body = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "can't load the plans" not in body

    def test_a_failed_fetch_is_not_cached(self, config):
        """A blip must not switch the plans off for the whole TTL."""
        client, _bot, stripe, _session = make_client(config)
        stripe.price_error = StripeAPIError("boom")
        client.get(f"/guild/{GUILD}/subscription")
        stripe.price_error = None
        body = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "can't load the plans" not in body
        assert "Subscribe" in body

    def test_the_list_is_cached_across_renders(self, config):
        """The cache is why rendering and checking out is not two calls to a
        third party on every purchase."""
        client, _bot, stripe, _session = make_client(config)
        for _ in range(3):
            client.get(f"/guild/{GUILD}/subscription")
        assert stripe.price_calls == 1


class TestCheckoutIsReachableFromTheBrowser:
    """The CSP must permit the hop the checkout route actually performs.

    The route answers a POST with a 303 to Stripe. `form-action` governs where
    a form submission may end up INCLUDING AFTER A REDIRECT, so if Stripe's
    hosted pages are not named there the browser sends the request, receives
    the redirect, and silently declines to follow it.

    That failure has no server-side symptom at all: the route returns 303, the
    log records a successful checkout session, and Stripe shows a session that
    nobody ever arrived at. The only evidence is a console violation in one
    person's browser. It shipped, and it presented as "the Subscribe button
    does nothing".
    """

    def form_action(self):
        from dashboard.app import CSP

        for directive in CSP.split(";"):
            if directive.strip().startswith("form-action"):
                return directive.split()
        raise AssertionError(f"no form-action directive in CSP: {CSP!r}")

    def test_checkout_is_a_permitted_form_target(self):
        assert "https://checkout.stripe.com" in self.form_action()

    def test_the_billing_portal_is_too(self):
        """The Manage billing button is the same 303-to-Stripe shape, so it
        fails the same silent way."""
        assert "https://billing.stripe.com" in self.form_action()

    def test_the_redirect_target_is_actually_allowed_by_form_action(self, config):
        """Ties the two halves together.

        Asserting the CSP names an origin proves nothing on its own if the
        route later redirects somewhere else; this checks the URL the route
        really issues against the directive that really ships.
        """
        client, _bot, _stripe, session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_SIX, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 303
        target = response.headers["Location"]
        origin = "/".join(target.split("/")[:3])
        assert origin in self.form_action(), (
            f"the route redirects to {origin}, which form-action forbids -- "
            "the browser will refuse the hop and the button will do nothing"
        )

    def test_nothing_else_was_loosened(self):
        """The allowance is a navigation target, not a script origin.

        Stripe must not become able to run script, be framed, or style this
        page: that is the difference between a redirect and an embed, and it is
        what keeps card data off this infrastructure.
        """
        from dashboard.app import CSP

        assert "script-src 'self';" in CSP
        assert "default-src 'none';" in CSP
        assert "frame-src" not in CSP
        assert "stripe.com" not in CSP.split("form-action")[0]


class TestAmounts:
    """The figures on the cards, and the ways a price can fail to state one.

    Amounts are read from Stripe on the render that shows them, so there is no
    second copy to drift -- but there is now a formatting layer between a
    Stripe integer and a price on a page about money, and it is worth pinning.
    """

    def amount_for(self, **price):
        price.setdefault("id", "price_x")
        price.setdefault("recurring", {"interval": "month", "interval_count": 1})
        return subscription_view.plans_from_prices([price])[0].amount

    def test_minor_units_become_a_decimal(self):
        assert self.amount_for(unit_amount=499, currency="usd") == "$4.99"

    def test_a_round_amount_keeps_its_cents(self):
        """"$5" for a $5.00 plan reads as an approximation on a page that is
        about to charge an exact number."""
        assert self.amount_for(unit_amount=500, currency="usd") == "$5.00"

    @pytest.mark.parametrize(
        "currency,expected",
        [("usd", "$4.99"), ("eur", "€4.99"), ("gbp", "£4.99"), ("cad", "CA$4.99")],
    )
    def test_known_currencies_get_their_symbol(self, currency, expected):
        assert self.amount_for(unit_amount=499, currency=currency) == expected

    def test_an_unknown_currency_falls_back_to_its_code(self):
        """Wrong symbol is worse than no symbol on a page about money."""
        assert self.amount_for(unit_amount=499, currency="sek") == "4.99 SEK"

    def test_a_zero_decimal_currency_is_not_divided(self):
        """JPY 500 is five hundred yen, not five. Dividing it by a hundred
        would advertise a plan at one percent of its price."""
        assert self.amount_for(unit_amount=500, currency="jpy") == "¥500"

    @pytest.mark.parametrize(
        "price",
        [
            {"currency": "usd"},                       # no amount at all
            {"unit_amount": None, "currency": "usd"},
            {"unit_amount": "499", "currency": "usd"},  # a string, not an int
            {"unit_amount": True, "currency": "usd"},   # bool is an int subclass
            {"unit_amount": -100, "currency": "usd"},
            {"unit_amount": 499},                       # no currency
            {"unit_amount": 499, "currency": ""},
        ],
    )
    def test_an_unreadable_amount_is_omitted_never_guessed(self, price):
        """None, so the card shows no figure and the reader clicks through to
        Stripe -- where the real one is. A wrong price is worse than none."""
        assert self.amount_for(**price) is None

    def test_a_priced_plan_still_renders_without_its_amount(self, config):
        """A tiered or metered price has no flat unit_amount. It must still be
        purchasable, because Checkout knows what to charge even when this page
        cannot summarise it in one number."""
        client, _bot, stripe, session = make_client(config)
        stripe.prices = [{"id": PRICE_MONTHLY,
                          "recurring": {"interval": "month", "interval_count": 1},
                          "metadata": {"label": "Monthly"}}]
        body = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert "Subscribe" in body
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 303

    @pytest.mark.parametrize(
        "interval,count,expected",
        [("month", 1, "per month"), ("month", 6, "per 6 months"),
         ("year", 1, "per year")],
    )
    def test_the_period_names_the_real_billing_interval(self, interval, count, expected):
        plans = subscription_view.plans_from_prices([
            {"id": "price_x", "unit_amount": 499, "currency": "usd",
             "recurring": {"interval": interval, "interval_count": count}}
        ])
        assert plans[0].period == expected

    def test_highlight_metadata_features_one_card(self, config):
        client, _bot, _stripe, _session = make_client(config)
        body = client.get(f"/guild/{GUILD}/subscription").data.decode()
        assert body.count("plan-featured") == 1
        assert "Most popular" in body

    @pytest.mark.parametrize("raw", ["0", "no", "", "maybe", None])
    def test_anything_but_a_yes_is_not_highlighted(self, raw):
        plans = subscription_view.plans_from_prices([
            {"id": "price_x", "recurring": {"interval": "month", "interval_count": 1},
             "metadata": {"highlight": raw}}
        ])
        assert plans[0].highlight is False


# -------------------------------------------------------------------
# Trial eligibility (#88 phase 8)
# -------------------------------------------------------------------
class TestTheTrialIsOfferedOnlyToServersThatMayHaveOne:
    """Two enforcement points, and the second is the one that matters.

    The plan card not showing a trial is presentation. The checkout route
    refusing to send one is the gate: a POST is not a click, anyone who has
    ever bought has seen this form, and a returning server replaying it would
    otherwise take a second free month once per cancellation, forever.
    """

    def page_for(self, **kwargs):
        return subscription_view.build(
            payload(**kwargs), application_id=APP_ID, plans=PLANS
        )

    def test_an_eligible_server_sees_the_trial_on_the_card(self):
        page = self.page_for(trial_eligible=True)
        trialled = [p for p in page.plans if page.trial_note_for(p)]
        assert [p.price_id for p in trialled] == [PRICE_YEARLY]

    def test_an_ineligible_server_sees_no_trial_anywhere(self):
        page = self.page_for(trial_eligible=False)
        assert all(page.trial_note_for(p) is None for p in page.plans)
        assert all(page.trial_days_for(p) is None for p in page.plans)

    def test_the_plan_still_knows_its_own_trial_length(self):
        """Only the *offer* is withheld, not the price's metadata.

        The plan object is what Stripe says the plan is; eligibility is what
        this server may have. Conflating them would mean a page could not tell
        "no trial configured" from "no trial for you", and the difference is
        the one a support question turns on.
        """
        page = self.page_for(trial_eligible=False)
        yearly = next(p for p in page.plans if p.price_id == PRICE_YEARLY)
        assert yearly.trial_days == 14
        assert page.trial_days_for(yearly) is None

    def test_a_payload_from_an_older_bot_offers_no_trial(self):
        """Absent key means no, never yes.

        A bot that predates this cannot answer the question, and defaulting to
        eligible would hand a free month to every server on the platform for as
        long as the two halves were out of step.
        """
        settings = payload()
        del settings["stripe"]["trial_eligible"]
        page = subscription_view.build(
            settings, application_id=APP_ID, plans=PLANS
        )
        assert page.trial_eligible is False

    def test_the_bots_kill_switch_also_withdraws_the_trial(self):
        page = self.page_for(stripe_enabled=False, trial_eligible=True)
        assert page.trial_eligible is False


class TestCheckoutHonoursEligibility:
    def test_an_eligible_server_gets_the_trial(self, config):
        client, _bot, stripe, session = make_client(config)
        client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_YEARLY, "csrf_token": session.csrf_token},
        )
        assert stripe.checkouts[0]["trial_days"] == 14

    def test_a_returning_server_is_refused_the_trial_but_not_the_purchase(
        self, config
    ):
        """It still buys. It just pays for the first month like everyone else.

        Refusing the whole checkout would be the wrong correction: they are
        entitled to subscribe, they are simply not entitled to a second free
        trial.
        """
        client, bot_api, stripe, session = make_client(config)
        bot_api._settings = payload(trial_eligible=False)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"price_id": PRICE_YEARLY, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 303
        assert stripe.checkouts[0]["price_id"] == PRICE_YEARLY
        assert stripe.checkouts[0]["trial_days"] is None
