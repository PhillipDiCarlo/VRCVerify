"""Unit tests for the Subscriptions page (issue #88, step 4).

The page that takes money, so the tests are mostly about the ways it can say
something untrue about somebody's subscription:

- a failed read must never render as "not subscribed", because "not
  subscribed" beside a Buy button is how a paying customer buys a second one
- a server that already pays must be shown no way to pay again, in the page AND
  in the route, because a hand-crafted POST is not a click
- the price id is never taken from the form; the browser may name a plan slug
  and nothing else
- "renews on" and "ends on" are different promises and the page must not
  confuse them
- an unrecognised price is a labelling problem, never a payment problem

`subscription_view` is pure, so every state below is built without Flask, a
clock or a network, which is the whole point of that split.
"""

import json
import time

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
        },
        "fields": {},
    }


def build(settings, **kwargs):
    kwargs.setdefault("application_id", APP_ID)
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
        assert [plan.slug for plan in page.plans] == [
            "monthly",
            "six_months",
            "yearly",
        ]
        assert page.discord_command is True
        assert page.store_url.endswith(f"/{APP_ID}/store/{SKU_ID}")

    def test_subscribed_by_card(self):
        page = build(
            payload(premium=True, active=True, status="active", price_id=PRICE_SIX),
            plan_slug="six_months",
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
            plan_slug="monthly",
        )
        assert page.state == "past_due"
        assert page.premium is True
        assert page.offers_portal is True
        assert page.offers_card is False

    def test_paying_on_both_platforms_is_warned_not_fixed(self):
        page = build(
            payload(premium=True, discord=True, active=True, status="active",
                    active_count=1),
            plan_slug="monthly",
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
            plan_slug="monthly",
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
            plan_slug="monthly",
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
            plan_slug="monthly",
        )
        assert page.ends_on == "3 November 2026"
        assert page.renews_on is None

    def test_a_lapsed_subscription_says_when_it_ended(self):
        """The row survives on purpose, so the page can say this rather than
        pretend the server was never a customer."""
        page = build(
            payload(status="canceled", period_end=PAST, price_id=PRICE_YEARLY),
            plan_slug="yearly",
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
            plan_slug=None,
        )
        assert page.state == "stripe"
        assert page.plan_label == subscription_view.UNKNOWN_PLAN_LABEL
        assert page.premium is True

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", 12345])
    def test_an_unreadable_date_is_omitted_not_guessed(self, raw):
        page = build(
            payload(premium=True, active=True, status="active", period_end=raw),
            plan_slug="monthly",
        )
        assert page.renews_on is None
        assert page.ends_on is None

    def test_no_amount_appears_anywhere_in_the_module(self):
        """Stripe knows what it charges.

        A second copy of a price on a page about money is a second thing to be
        wrong, and it would be wrong silently -- the page would keep rendering
        the old number long after Stripe charged a new one.
        """
        source = open(subscription_view.__file__, encoding="utf-8").read()
        for amount in ("4.99", "26.99", "47.99", "$"):
            assert amount not in source


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
        stripe_prices={
            "monthly": PRICE_MONTHLY,
            "six_months": PRICE_SIX,
            "yearly": PRICE_YEARLY,
        },
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
    def test_a_plan_slug_becomes_a_price_server_side(self, config):
        """THE most likely way to get this endpoint wrong.

        A form-supplied price id would let anyone check out against any price
        on the account, including a $0 one created while testing.
        """
        client, _bot, stripe, session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"plan": "six_months", "csrf_token": session.csrf_token},
        )
        assert response.status_code == 303
        assert response.headers["Location"].startswith("https://checkout.stripe.com/")
        assert stripe.checkouts[0]["price_id"] == PRICE_SIX

    def test_a_raw_price_id_in_the_form_is_refused(self, config):
        client, _bot, stripe, session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"plan": PRICE_MONTHLY, "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.checkouts == []

    @pytest.mark.parametrize("plan", ["", "free", "MONTHLY", "../monthly", None])
    def test_an_unknown_plan_buys_nothing(self, config, plan):
        client, _bot, stripe, session = make_client(config)
        data = {"csrf_token": session.csrf_token}
        if plan is not None:
            data["plan"] = plan
        response = client.post(f"/guild/{GUILD}/subscription/checkout", data=data)
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_no_csrf_token_buys_nothing(self, config):
        client, _bot, stripe, _session = make_client(config)
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout", data={"plan": "monthly"}
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
            data={"plan": "monthly", "csrf_token": session.csrf_token},
        )
        assert response.status_code == 302
        assert stripe.checkouts == []

    def test_a_discord_subscriber_cannot_check_out_either(self, config):
        client, _bot, stripe, session = make_client(
            config, settings=payload(premium=True, discord=True)
        )
        client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"plan": "monthly", "csrf_token": session.csrf_token},
        )
        assert stripe.checkouts == []

    def test_the_guild_is_bound_into_subscription_metadata(self, config):
        """It has to survive to a renewal a year from now, which
        client_reference_id does not."""
        client, _bot, stripe, session = make_client(config)
        client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"plan": "monthly", "csrf_token": session.csrf_token},
        )
        assert stripe.checkouts[0]["guild_id"] == GUILD
        assert stripe.checkouts[0]["actor_discord_id"] == ACTOR

    def test_stripe_being_down_apologises_rather_than_pretending(self, config):
        client, _bot, stripe, session = make_client(config)
        stripe.error = StripeAPIError("boom")
        response = client.post(
            f"/guild/{GUILD}/subscription/checkout",
            data={"plan": "monthly", "csrf_token": session.csrf_token},
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
            data={"plan": "monthly", "csrf_token": session.csrf_token},
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
