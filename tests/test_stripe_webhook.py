"""Unit tests for the Stripe webhook endpoint (issue #88, step 3).

This is **the first public, unauthenticated inbound route this project has ever
had.** Everything else on the VPS is reached by a browser holding a session;
this is reached by a third party holding a signature. So these tests are almost
entirely about the ways that door can be opened by someone who should not be
able to open it, and about the retry contract that decides whether a paid
subscription is ever lost.

The themes:

- the signature is the authentication, and it is checked *before* the body is
  parsed — an endpoint that parses first has already run our JSON parser on an
  unknown party's bytes
- every HMAC here is computed in the test, never mocked. A mocked signature
  check proves the handler calls something, not that the something works
- the response code is a retry instruction. Stripe retries a non-2xx for three
  days, so anything we could not finish must be non-2xx, and anything genuinely
  finished must be 200 — including "not an event we act on", or a checkbox in
  the Stripe dashboard starts a three-day redelivery storm
- with the kill switch off the path does not exist at all
"""

import hmac
import json
import time
from hashlib import sha256
from types import SimpleNamespace

import pytest
import requests

pytest.importorskip("flask")

from dashboard import stripe_events  # noqa: E402
from dashboard.app import create_app  # noqa: E402
from dashboard.botapi import BotAPIError  # noqa: E402
from dashboard.config import DashboardConfig, DashboardConfigError  # noqa: E402
from dashboard.sessions import SessionStore  # noqa: E402
from dashboard.stripe_api import StripeAPIError  # noqa: E402

GUILD_ID = "111111111111"
SIGNING_KEY = "s" * 48
SECRET_KEY = "k" * 48
WEBHOOK_SECRET = "whsec_" + "w" * 32
STRIPE_KEY = "sk_test_" + "x" * 24

SUBSCRIPTION_ID = "sub_TEST"
CUSTOMER_ID = "cus_TEST"
PRICE_MONTHLY = "price_monthly"
PRICE_SIX = "price_six_months"
PRICE_YEARLY = "price_yearly"
PRODUCT_ID = "prod_VRCVERIFYPREMIUM"
PORTAL_CONFIGURATION = "bpc_VRCVERIFYPREMIUM"

WEBHOOK_PATH = "/stripe/webhook"


# -------------------------------------------------------------------
# Fixtures and fakes
# -------------------------------------------------------------------
@pytest.fixture
def certs(tmp_path):
    for name in ("client.pem", "client.key", "ca.pem"):
        (tmp_path / name).write_text("placeholder")
    return tmp_path


def make_config(tmp_path, certs, **overrides):
    base = dict(
        discord_client_id="1335738139825799188",
        discord_client_secret="client-secret",
        oauth_redirect_uri="https://dashboard.vrcverify.com/callback",
        secret_key=SECRET_KEY,
        session_db_path=str(tmp_path / "sessions.db"),
        bot_api_url="https://100.117.6.99:5002",
        bot_api_client_cert=str(certs / "client.pem"),
        bot_api_client_key=str(certs / "client.key"),
        bot_api_ca=str(certs / "ca.pem"),
        bot_api_signing_key=SIGNING_KEY.encode(),
        stripe_enabled=True,
        stripe_secret_key=STRIPE_KEY,
        stripe_webhook_secret=WEBHOOK_SECRET,
        stripe_product_id=PRODUCT_ID,
    )
    base.update(overrides)
    return DashboardConfig(**base)


class FakeBotAPI:
    """Collects what was forwarded, and can refuse on demand."""

    def __init__(self):
        self.forwarded = []
        self.error = None

    def put_stripe_subscription(self, guild_id, subscription):
        self.forwarded.append((str(guild_id), dict(subscription)))
        if self.error is not None:
            raise self.error
        return {"applied": True, "status": subscription.get("status")}

    # The picker calls this on `/`; unused here but the app expects it.
    def admin_guild_ids(self, actor_id, guild_ids):
        return set()


def make_subscription(
    *,
    subscription_id=SUBSCRIPTION_ID,
    guild_id=GUILD_ID,
    status="active",
    price_id=PRICE_MONTHLY,
    period_end=None,
    cancel_at_period_end=False,
    customer=CUSTOMER_ID,
    metadata=None,
):
    if metadata is None:
        metadata = {} if guild_id is None else {"guild_id": guild_id}
    return {
        "id": subscription_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "current_period_end": int(period_end or (time.time() + 30 * 86400)),
        "cancel_at_period_end": cancel_at_period_end,
        "metadata": metadata,
        "items": {"data": [{"price": {"id": price_id}}]},
        # Fields Stripe really sends that we must not mirror.
        "latest_invoice": "in_TEST",
        "default_payment_method": "pm_TEST",
    }


class FakeStripe:
    """Stands in for the Stripe API. Returns whatever `current` is set to."""

    def __init__(self, current=None):
        self.current = current if current is not None else make_subscription()
        self.error = None
        self.reads = []

    def get_subscription(self, subscription_id):
        self.reads.append(subscription_id)
        if self.error is not None:
            raise self.error
        return self.current


@pytest.fixture
def bot_api():
    return FakeBotAPI()


@pytest.fixture
def stripe():
    return FakeStripe()


@pytest.fixture
def app(tmp_path, certs, bot_api, stripe):
    config = make_config(tmp_path, certs)
    application = create_app(
        config,
        store=SessionStore(config.session_db_path, config.session_max_age),
        client=bot_api,
        stripe=stripe,
    )
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def make_event(subscription=None, *, event_id="evt_TEST", event_type=None, created=None):
    return {
        "id": event_id,
        "object": "event",
        "type": event_type or "customer.subscription.updated",
        "created": int(created or time.time()),
        "data": {"object": subscription if subscription is not None else make_subscription()},
    }


def sign(body: bytes, *, secret=WEBHOOK_SECRET, timestamp=None) -> str:
    """Compute a real Stripe-Signature header. Never mocked."""
    stamp = int(timestamp if timestamp is not None else time.time())
    signed = f"{stamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed, sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def post(client, event=None, *, body=None, header=None, **sign_kwargs):
    if body is None:
        body = json.dumps(event if event is not None else make_event()).encode()
    if header is None:
        header = sign(body, **sign_kwargs)
    headers = {"Content-Type": "application/json"}
    if header is not False:
        headers["Stripe-Signature"] = header
    return client.post(WEBHOOK_PATH, data=body, headers=headers)


# -------------------------------------------------------------------
# The kill switch
# -------------------------------------------------------------------
class TestKillSwitch:
    def test_the_route_does_not_exist_when_stripe_is_off(
        self, tmp_path, certs, bot_api
    ):
        """A 404, not a handler that declines.

        The most reliable way to be sure a public endpoint cannot run is for it
        never to have been added to the routing table.
        """
        config = make_config(
            tmp_path,
            certs,
            stripe_enabled=False,
            stripe_secret_key="",
            stripe_webhook_secret="",
            stripe_product_id="",
        )
        application = create_app(
            config,
            store=SessionStore(config.session_db_path, config.session_max_age),
            client=bot_api,
        )
        application.config.update(TESTING=True)
        response = application.test_client().post(WEBHOOK_PATH, data=b"{}")
        assert response.status_code == 404
        assert bot_api.forwarded == []

    def test_no_stripe_client_is_built_when_off(self, tmp_path, certs, bot_api):
        """With the switch off the process holds no Stripe client at all --
        not even an unused one holding a secret key."""
        config = make_config(
            tmp_path,
            certs,
            stripe_enabled=False,
            stripe_secret_key="",
            stripe_webhook_secret="",
            stripe_product_id="",
        )
        application = create_app(
            config,
            store=SessionStore(config.session_db_path, config.session_max_age),
            client=bot_api,
        )
        assert application.config["STRIPE"] is None


# -------------------------------------------------------------------
# The signature
# -------------------------------------------------------------------
class TestTheSignature:
    """Every HMAC below is computed in the test. None of this is mocked."""

    def test_a_valid_signature_is_accepted(self, client, bot_api):
        assert post(client).status_code == 200
        assert len(bot_api.forwarded) == 1

    def test_a_tampered_body_is_refused(self, client, bot_api):
        body = json.dumps(make_event()).encode()
        header = sign(body)
        response = post(client, body=body + b" ", header=header)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    def test_a_wrong_secret_is_refused(self, client, bot_api):
        response = post(client, secret="whsec_" + "z" * 32)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    def test_an_expired_timestamp_is_refused(self, client, bot_api):
        response = post(client, timestamp=time.time() - 3600)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    def test_a_future_timestamp_is_refused(self, client, bot_api):
        """Tolerance is absolute, not one-sided.

        A signature dated an hour ahead is as much a sign of something wrong as
        one an hour behind, and accepting it would widen the replay window by
        the whole tolerance in the other direction.
        """
        response = post(client, timestamp=time.time() + 3600)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    def test_a_missing_header_is_refused(self, client, bot_api):
        response = post(client, header=False)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "garbage",
            "t=,v1=",
            "v1=abc",  # no timestamp
            "t=123",  # no signature
            "t=notanumber,v1=abc",
        ],
    )
    def test_a_malformed_header_is_refused(self, client, bot_api, header):
        response = post(client, header=header)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    def test_a_second_signature_is_enough(self, client, bot_api):
        """Stripe sends more than one v1 while a signing secret is rolled.

        Refusing on the first non-matching one would turn a routine secret
        rotation into an outage.
        """
        body = json.dumps(make_event()).encode()
        good = sign(body)
        stamp = good.split(",")[0]
        header = f"{stamp},v1={'0' * 64},{good.split(',', 1)[1]}"
        assert post(client, body=body, header=header).status_code == 200
        assert len(bot_api.forwarded) == 1

    def test_the_response_does_not_say_why_it_failed(self, client):
        """An endpoint that explains a signature failure helps someone build
        one that works."""
        reasons = set()
        for kwargs in (
            {"secret": "whsec_" + "z" * 32},
            {"timestamp": time.time() - 3600},
        ):
            response = post(client, **kwargs)
            reasons.add(response.get_json()["error"])
        assert reasons == {"invalid_signature"}

    def test_the_body_is_not_parsed_before_the_signature_is_checked(
        self, client, bot_api
    ):
        """The ordering property, asserted rather than assumed.

        Send unparseable JSON with a *bad* signature. If the answer names the
        parse failure, the parser ran on unverified bytes -- which is exactly
        the foothold the signature exists to deny.
        """
        response = post(client, body=b"{ this is not json", header="t=1,v1=deadbeef")
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_signature"
        assert bot_api.forwarded == []

    def test_unparseable_json_with_a_good_signature_is_a_400(self, client, bot_api):
        response = post(client, body=b"{ this is not json")
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"
        assert bot_api.forwarded == []


# -------------------------------------------------------------------
# Which events are acted on
# -------------------------------------------------------------------
class TestEventRouting:
    def test_a_subscription_event_is_forwarded(self, client, bot_api):
        assert post(client).status_code == 200
        guild_id, payload = bot_api.forwarded[0]
        assert guild_id == GUILD_ID
        assert payload["subscription_id"] == SUBSCRIPTION_ID
        assert payload["status"] == "active"
        assert payload["price_id"] == PRICE_MONTHLY

    @pytest.mark.parametrize(
        "event_type",
        [
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ],
    )
    def test_every_subscription_lifecycle_event_is_acted_on(
        self, client, bot_api, event_type
    ):
        assert post(client, make_event(event_type=event_type)).status_code == 200
        assert len(bot_api.forwarded) == 1

    @pytest.mark.parametrize(
        "event_type",
        ["invoice.paid", "checkout.session.completed", "payment_intent.succeeded"],
    )
    def test_other_events_are_acknowledged_and_ignored(
        self, client, bot_api, event_type
    ):
        """200, not 400.

        Somebody enabling an extra event type in the Stripe dashboard must not
        start a three-day retry storm over a checkbox.
        """
        response = post(client, make_event(event_type=event_type))
        assert response.status_code == 200
        assert response.get_json()["ignored"] == "event_type"
        assert bot_api.forwarded == []

    def test_an_event_with_no_id_is_ignored(self, client, bot_api):
        assert post(client, make_event(event_id="")).status_code == 200
        assert bot_api.forwarded == []

    def test_a_subscription_with_no_guild_metadata_is_ignored(
        self, client, bot_api, stripe
    ):
        """Nothing to route it to, and guessing is not an option.

        200 because retrying will not add metadata that was never set -- this
        is a Checkout Session built wrong, and the log is where it surfaces.
        """
        stripe.current = make_subscription(guild_id=None)
        response = post(client, make_event(make_subscription(guild_id=None)))
        assert response.status_code == 200
        assert response.get_json()["ignored"] == "no_guild"
        assert bot_api.forwarded == []

    def test_the_guild_comes_from_subscription_metadata(self, client, bot_api, stripe):
        """Not from the checkout session's client_reference_id.

        The reference id exists only on the session, so it is there for the
        first event and gone by the first renewal. Metadata on the subscription
        rides along with every event for its life, which is what the binding
        has to survive to.
        """
        stripe.current = make_subscription(guild_id="222222222222")
        event = make_event(make_subscription(guild_id="222222222222"))
        event["data"]["object"]["client_reference_id"] = "999999999999"
        post(client, event)
        assert bot_api.forwarded[0][0] == "222222222222"

    def test_a_non_numeric_guild_id_is_refused(self, client, bot_api, stripe):
        stripe.current = make_subscription(metadata={"guild_id": "../../etc/passwd"})
        response = post(
            client, make_event(make_subscription(metadata={"guild_id": "nope"}))
        )
        assert response.status_code == 200
        assert bot_api.forwarded == []


# -------------------------------------------------------------------
# Current state, not the event's snapshot
# -------------------------------------------------------------------
class TestCurrentStateIsFetched:
    def test_the_subscription_is_read_back_from_stripe(self, client, stripe):
        post(client)
        assert stripe.reads == [SUBSCRIPTION_ID]

    def test_the_fetched_state_wins_over_the_event_body(self, client, bot_api, stripe):
        """The whole reason for the extra call.

        Stripe promises nothing about event ordering, and `event.created` has
        one-second resolution -- so whichever delivery does apply must carry
        current truth rather than a snapshot that may already be stale.
        """
        stale = make_subscription(status="active")
        stripe.current = make_subscription(status="canceled")
        post(client, make_event(stale))
        assert bot_api.forwarded[0][1]["status"] == "canceled"

    def test_stripe_being_down_is_a_503_so_it_retries(self, client, bot_api, stripe):
        stripe.error = StripeAPIError("boom")
        response = post(client)
        assert response.status_code == 503
        assert bot_api.forwarded == []


# -------------------------------------------------------------------
# The retry contract
# -------------------------------------------------------------------
class TestTheRetryContract:
    def test_a_bot_that_cannot_be_reached_is_a_503(self, client, bot_api):
        """Never 200-and-drop.

        Stripe retries a non-2xx for up to three days, which covers a bot
        restart, a Tailscale blip or a homelab power cut. Answering 200 on a
        failed forward is the one outcome that loses a paid subscription
        permanently.
        """
        bot_api.error = BotAPIError("unreachable")
        assert post(client).status_code == 503

    def test_a_bot_refusal_is_also_a_503(self, client, bot_api):
        bot_api.error = BotAPIError("unavailable", 503)
        assert post(client).status_code == 503

    def test_a_duplicate_is_reported_as_handled(self, client, bot_api):
        """The bot answering "already processed" is success, not failure.

        Retrying a delivery the bot has already applied must not loop for
        three days.
        """

        def already_done(guild_id, subscription):
            return {"applied": False, "reason": "duplicate_event"}

        bot_api.put_stripe_subscription = already_done
        response = post(client)
        assert response.status_code == 200
        assert response.get_json()["applied"] is False


# -------------------------------------------------------------------
# What crosses the wire
# -------------------------------------------------------------------
class TestTheForwardedPayload:
    def test_it_carries_exactly_the_agreed_fields(self, client, bot_api):
        post(client)
        assert set(bot_api.forwarded[0][1]) == {
            "event_id",
            "event_created",
            "customer_id",
            "subscription_id",
            "price_id",
            "status",
            "current_period_end",
            "cancel_at_period_end",
        }

    def test_nothing_raw_from_stripe_is_forwarded(self, client, bot_api):
        """The bot receives eight named fields it already validates, never a
        nested object it would have to go digging through."""
        post(client)
        payload = bot_api.forwarded[0][1]
        assert "in_TEST" not in repr(payload)
        assert "pm_TEST" not in repr(payload)
        assert "items" not in payload
        assert "metadata" not in payload

    def test_no_customer_pii_is_mirrored(self, client, bot_api, stripe):
        """Checkout collects an email and Stripe keeps it.

        Mirroring it would add customer PII to the one database in this project
        holding Discord-to-VRChat identity links, to power a page that says
        "manage billing in the portal".
        """
        stripe.current = make_subscription()
        stripe.current["customer_email"] = "someone@example.com"
        stripe.current["billing_details"] = {"name": "A Person"}
        post(client)
        rendered = repr(bot_api.forwarded[0][1])
        assert "someone@example.com" not in rendered
        assert "A Person" not in rendered

    def test_cancel_at_period_end_is_a_real_boolean(self, client, bot_api, stripe):
        stripe.current = make_subscription(cancel_at_period_end=True)
        post(client)
        assert bot_api.forwarded[0][1]["cancel_at_period_end"] is True

    def test_timestamps_are_iso_utc(self, client, bot_api):
        post(client)
        payload = bot_api.forwarded[0][1]
        assert payload["current_period_end"].endswith("+00:00")
        assert payload["event_created"].endswith("+00:00")

    def test_an_incomplete_subscription_is_not_forwarded(self, client, bot_api, stripe):
        stripe.current = make_subscription()
        del stripe.current["current_period_end"]
        response = post(client)
        assert response.status_code == 200
        assert response.get_json()["ignored"] == "incomplete"
        assert bot_api.forwarded == []

    def test_an_unknown_price_is_still_forwarded(self, client, bot_api, stripe):
        """A price absent from our table is a label problem, never a payment
        problem. The subscription is real and paid for."""
        stripe.current = make_subscription(price_id="price_SOMETHING_ELSE")
        assert post(client).status_code == 200
        assert bot_api.forwarded[0][1]["price_id"] == "price_SOMETHING_ELSE"


# -------------------------------------------------------------------
# Budgets and bounds
# -------------------------------------------------------------------
class TestBoundsOnAPublicRoute:
    def test_an_oversized_body_is_refused(self, client, bot_api):
        response = post(client, body=b"x" * (stripe_events.MAX_BODY_BYTES + 1))
        assert response.status_code == 413
        assert bot_api.forwarded == []

    def test_a_burst_is_rate_limited(self, app, client, bot_api):
        """429, which Stripe treats as a retry rather than a failure -- so the
        cost of a burst is delay and never loss."""
        app.config["STRIPE_RATE"].limit = 3
        codes = [post(client).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]
        assert len(bot_api.forwarded) == 3

    def test_the_limiter_is_not_shared_with_anything_else(self, app):
        """The webhook is the one route that must keep working when the
        session-authenticated ones are under load. A subscription must not be
        lost because somebody is hammering /login."""
        assert app.config["STRIPE_RATE"] is not app.config.get("STORE")
        app.config["STRIPE_RATE"].limit = 0
        assert app.test_client().get("/healthz").status_code == 200


class TestFoundByProbing:
    """Four findings from an adversarial pass over this endpoint.

    None of them was reachable without a valid Stripe signature, which is the
    argument for why they should never fire — not an argument for leaving them
    open. The first three chain: a malformed id changes which object comes
    back, and the object that comes back decides which guild gets written.
    """

    def test_every_body_is_capped_before_a_handler_sees_it(self, app):
        """`request.content_length` is None on a chunked request.

        A handler that checks it first sees None and then reads the whole body
        into memory to measure it -- so the size check inside the webhook is
        not the half that holds. Werkzeug's global cap is.
        """
        assert app.config["MAX_CONTENT_LENGTH"] is not None
        assert app.config["MAX_CONTENT_LENGTH"] <= 1024 * 1024

    @pytest.mark.parametrize(
        "subscription_id",
        [
            "sub_x/../../v1/customers",
            "sub_x?expand[]=customer",
            "sub_x#fragment",
            "sub_x/../charges",
            "../v1/account",
        ],
    )
    def test_a_malformed_subscription_id_never_reaches_the_api(
        self, client, bot_api, stripe, subscription_id
    ):
        """That value is interpolated into an API URL a moment later.

        A `/` or a `?` turns a read of one subscription into a request for
        something else entirely.
        """
        response = post(client, make_event(make_subscription(subscription_id=subscription_id)))
        assert response.status_code == 200
        assert stripe.reads == []
        assert bot_api.forwarded == []

    def test_the_api_client_refuses_a_bad_id_on_its_own(self):
        """Checked where the URL is built, as well as by the caller.

        Neither check is load-bearing alone, which is the point.
        """
        from dashboard.stripe_api import StripeClient

        with pytest.raises(StripeAPIError):
            StripeClient("sk_test_x").get_subscription("sub_x/../../v1/customers")

    def test_a_mismatched_subscription_is_refused(self, client, bot_api, stripe):
        """We asked about one subscription; anything else means the request did
        not go where this code believes it went.

        Without this, a read that landed on a different object would be written
        to whichever guild *that* object names.
        """
        stripe.current = make_subscription(
            subscription_id="sub_SOMEONE_ELSE", guild_id="222222222222"
        )
        response = post(client, make_event(make_subscription(subscription_id="sub_TEST")))
        assert response.status_code == 503
        assert bot_api.forwarded == []

    @pytest.mark.parametrize("position", ["prepend", "append"])
    def test_a_second_timestamp_is_refused(self, client, bot_api, position):
        """Stripe sends exactly one `t`.

        Several is a broken sender or someone probing which one this believes,
        and whichever answer it gave would be arbitrary. Neither ordering can
        forge a signature -- the timestamp is inside the signed string -- but a
        parser with an opinion nobody chose is not one to keep.
        """
        body = json.dumps(make_event()).encode()
        good = sign(body)
        stale = f"t={int(time.time()) - 100000}"
        header = (
            f"{stale},{good}" if position == "prepend" else f"{good},{stale}"
        )
        response = post(client, body=body, header=header)
        assert response.status_code == 400
        assert bot_api.forwarded == []

    def test_a_replay_inside_the_window_is_safe(self, client, bot_api):
        """A captured webhook replayed within the 5-minute tolerance verifies
        again -- and must be harmless.

        Idempotency is the bot's job, keyed on the event id, and this pins that
        the dashboard forwards the identical event id rather than minting
        something new that would defeat it.
        """
        body = json.dumps(make_event()).encode()
        header = sign(body)
        assert post(client, body=body, header=header).status_code == 200
        assert post(client, body=body, header=header).status_code == 200
        assert len(bot_api.forwarded) == 2
        assert bot_api.forwarded[0][1]["event_id"] == bot_api.forwarded[1][1]["event_id"]


# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
class TestConfiguration:
    """Refuse to boot rather than come up half-configured.

    A dashboard missing its webhook secret rejects every event Stripe sends and
    looks healthy doing it. One missing a price id renders a plan card that
    500s when somebody clicks Buy.
    """

    def base_env(self, tmp_path, certs):
        return {
            "DISCORD_CLIENT_ID": "1",
            "DISCORD_CLIENT_SECRET": "s",
            "OAUTH_REDIRECT_URI": "https://d.example.com/callback",
            "DASHBOARD_SECRET_KEY": SECRET_KEY,
            "BOT_API_TOKEN_SIGNING_KEY": SIGNING_KEY,
            "BOT_API_URL": "https://10.0.0.1:5002",
            "BOT_API_CLIENT_CERT": str(certs / "client.pem"),
            "BOT_API_CLIENT_KEY": str(certs / "client.key"),
            "BOT_API_CA": str(certs / "ca.pem"),
            "SESSION_DB_PATH": str(tmp_path / "s.db"),
        }

    def stripe_env(self):
        return {
            "STRIPE_ENABLED": "1",
            "STRIPE_SECRET_KEY": STRIPE_KEY,
            "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "STRIPE_PRODUCT_ID": PRODUCT_ID,
        }

    def build(self, monkeypatch, tmp_path, certs, **overrides):
        env = self.base_env(tmp_path, certs)
        env.update(overrides)
        for key in (
            "STRIPE_ENABLED",
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
            "STRIPE_PRODUCT_ID",
            "STRIPE_PORTAL_CONFIGURATION_ID",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return DashboardConfig.from_env()

    def test_stripe_off_needs_no_stripe_config(self, monkeypatch, tmp_path, certs):
        """The whole feature ships dormant. Nothing may be required to boot."""
        config = self.build(monkeypatch, tmp_path, certs)
        assert config.stripe_enabled is False
        assert config.stripe_secret_key == ""

    def test_stripe_on_needs_everything(self, monkeypatch, tmp_path, certs):
        config = self.build(monkeypatch, tmp_path, certs, **self.stripe_env())
        assert config.stripe_enabled is True
        assert config.stripe_product_id == PRODUCT_ID

    @pytest.mark.parametrize(
        "missing",
        [
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
            "STRIPE_PRODUCT_ID",
        ],
    )
    def test_a_missing_value_refuses_to_boot(
        self, monkeypatch, tmp_path, certs, missing
    ):
        env = self.stripe_env()
        env.pop(missing)
        with pytest.raises(DashboardConfigError):
            self.build(monkeypatch, tmp_path, certs, **env)

    def test_a_publishable_key_is_refused(self, monkeypatch, tmp_path, certs):
        """pk_ here would fail every call, at the worst possible moment."""
        env = self.stripe_env()
        env["STRIPE_SECRET_KEY"] = "pk_test_" + "x" * 24
        with pytest.raises(DashboardConfigError):
            self.build(monkeypatch, tmp_path, certs, **env)

    def test_an_api_key_as_the_webhook_secret_is_refused(
        self, monkeypatch, tmp_path, certs
    ):
        """The signing secret and the API key are different things that both
        look like opaque strings. Confusing them rejects every event."""
        env = self.stripe_env()
        env["STRIPE_WEBHOOK_SECRET"] = STRIPE_KEY
        with pytest.raises(DashboardConfigError):
            self.build(monkeypatch, tmp_path, certs, **env)

    def test_a_price_id_in_place_of_the_product_is_refused(
        self, monkeypatch, tmp_path, certs
    ):
        """The plausible mistake, now that there is only one id to get wrong.

        Product and price ids sit next to each other in the Stripe dashboard
        and both are "the thing I copied for the plan". A price id here matches
        no prices, so the page would apologise forever with nothing in the log
        saying why -- the failure is silent, which is what makes a boot-time
        check worth its line.
        """
        env = self.stripe_env()
        env["STRIPE_PRODUCT_ID"] = PRICE_MONTHLY
        with pytest.raises(DashboardConfigError):
            self.build(monkeypatch, tmp_path, certs, **env)

    def test_the_portal_configuration_is_optional(
        self, monkeypatch, tmp_path, certs
    ):
        """The one Stripe variable that may be absent while Stripe is on.

        Unset means Stripe's account default, which is what shipped and is
        still running. Requiring it would mean a deploy that reaches the host
        before the variable does takes the whole site down -- the failure of
        2026-08-15, repeated.
        """
        config = self.build(monkeypatch, tmp_path, certs, **self.stripe_env())
        assert config.stripe_enabled is True
        assert config.stripe_portal_configuration_id == ""

    def test_the_portal_configuration_is_kept_when_set(
        self, monkeypatch, tmp_path, certs
    ):
        env = self.stripe_env()
        env["STRIPE_PORTAL_CONFIGURATION_ID"] = PORTAL_CONFIGURATION
        config = self.build(monkeypatch, tmp_path, certs, **env)
        assert config.stripe_portal_configuration_id == PORTAL_CONFIGURATION

    @pytest.mark.parametrize("wrong", [PRODUCT_ID, PRICE_MONTHLY, "not-an-id"])
    def test_something_that_is_not_a_portal_configuration_is_refused(
        self, monkeypatch, tmp_path, certs, wrong
    ):
        """Every plausible mistake here fails late and says nothing.

        Stripe rejects the portal session, the customer is bounced to
        error:portal, and the log names neither which id was wrong nor that an
        id was the problem. Checked at boot instead, where it is one line.
        """
        env = self.stripe_env()
        env["STRIPE_PORTAL_CONFIGURATION_ID"] = wrong
        with pytest.raises(DashboardConfigError):
            self.build(monkeypatch, tmp_path, certs, **env)

    def test_a_blank_portal_configuration_is_not_an_error(
        self, monkeypatch, tmp_path, certs
    ):
        """An empty value is how the example file documents "use the default",
        so whitespace must read as absent rather than as a malformed id."""
        env = self.stripe_env()
        env["STRIPE_PORTAL_CONFIGURATION_ID"] = "   "
        config = self.build(monkeypatch, tmp_path, certs, **env)
        assert config.stripe_portal_configuration_id == ""


# -------------------------------------------------------------------
# The pure module, directly
# -------------------------------------------------------------------
class TestPureHelpers:
    def test_verify_accepts_its_own_signature(self):
        body = b'{"hello":"world"}'
        stripe_events.verify_signature(
            body, sign(body), WEBHOOK_SECRET, now=time.time()
        )

    def test_verify_is_tolerant_within_the_window_and_not_outside_it(self):
        body = b"{}"
        stamp = time.time() - 299
        stripe_events.verify_signature(
            body, sign(body, timestamp=stamp), WEBHOOK_SECRET, now=time.time()
        )
        with pytest.raises(stripe_events.SignatureError):
            stripe_events.verify_signature(
                body,
                sign(body, timestamp=time.time() - 301),
                WEBHOOK_SECRET,
                now=time.time(),
            )

    def test_price_id_comes_from_the_first_line_item(self):
        assert (
            stripe_events.price_id_from(make_subscription(price_id="price_x"))
            == "price_x"
        )

    def test_a_subscription_with_no_items_has_no_price(self):
        subscription = make_subscription()
        subscription["items"] = {"data": []}
        assert stripe_events.price_id_from(subscription) is None

    def test_an_expanded_customer_object_still_yields_an_id(self):
        subscription = make_subscription(customer={"id": "cus_EXPANDED"})
        payload = stripe_events.normalise(
            subscription, event_id="evt_1", event_created=int(time.time())
        )
        assert payload["customer_id"] == "cus_EXPANDED"


class TestListPrices:
    """The one outbound call that decides what the site sells.

    Wrong here is not a broken page: it is the wrong plans, at the wrong
    prices, or a plan that was retired still being purchasable.
    """

    def client(self, *, status=200, body=None, boom=None):
        from dashboard.stripe_api import StripeClient

        captured = {}

        class FakeResponse:
            status_code = status

            @staticmethod
            def json():
                if body is None:
                    raise ValueError("no body")
                return body

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            if boom is not None:
                raise boom
            return FakeResponse()

        client = StripeClient("sk_test_x")
        client._session.get = fake_get
        return client, captured

    def test_it_asks_only_for_active_recurring_prices(self):
        """Both filters are load-bearing.

        Archiving a price is how a plan is retired, so an inactive one must
        stop being offered with no deploy; and a one-off price rendered on a
        page whose checkout runs in mode=subscription is a 400 at the moment
        somebody clicks Buy.
        """
        client, captured = self.client(body={"data": []})
        client.list_prices("prod_x")
        assert captured["url"].endswith("/prices")
        assert captured["params"]["product"] == "prod_x"
        assert captured["params"]["active"] == "true"
        assert captured["params"]["type"] == "recurring"

    def test_it_returns_the_price_objects(self):
        client, _ = self.client(body={"data": [{"id": "price_a"}, {"id": "price_b"}]})
        assert [p["id"] for p in client.list_prices("prod_x")] == ["price_a", "price_b"]

    def test_junk_entries_are_dropped_not_returned(self):
        client, _ = self.client(body={"data": [{"id": "price_a"}, None, "x", 7]})
        assert client.list_prices("prod_x") == [{"id": "price_a"}]

    @pytest.mark.parametrize(
        "product_id",
        ["prod_x/../../v1/customers", "prod_x?expand[]=data", "prod_x#f", "../v1/account"],
    )
    def test_a_malformed_product_id_never_reaches_the_api(self, product_id):
        """A query parameter today; the same check as everywhere else, because
        "it is only a query parameter" stops being true after a refactor."""
        client, captured = self.client(body={"data": []})
        with pytest.raises(StripeAPIError):
            client.list_prices(product_id)
        assert captured == {}

    def test_a_refusal_is_an_error_not_an_empty_list(self):
        """An empty list would read as "this product sells nothing", which is a
        statement, and we do not have one to make."""
        client, _ = self.client(status=403, body={"error": {}})
        with pytest.raises(StripeAPIError) as caught:
            client.list_prices("prod_x")
        assert caught.value.status == 403

    def test_an_unreachable_stripe_is_an_error(self):
        client, _ = self.client(boom=requests.RequestException("dns"))
        with pytest.raises(StripeAPIError):
            client.list_prices("prod_x")

    def test_an_unreadable_body_is_an_error(self):
        client, _ = self.client(body=None)
        with pytest.raises(StripeAPIError):
            client.list_prices("prod_x")

    @pytest.mark.parametrize("payload", [{}, {"data": "prices"}, {"data": None}])
    def test_a_body_with_no_price_list_is_an_error(self, payload):
        client, _ = self.client(body=payload)
        with pytest.raises(StripeAPIError):
            client.list_prices("prod_x")

    def test_it_does_not_follow_has_more(self):
        """One page, deliberately. Following it would turn a page render into
        an unbounded number of API calls on the public host."""
        client, captured = self.client(
            body={"data": [{"id": "price_a"}], "has_more": True}
        )
        assert client.list_prices("prod_x") == [{"id": "price_a"}]
        assert captured["params"]["limit"] == 100
