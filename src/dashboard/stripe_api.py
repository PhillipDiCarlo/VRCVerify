"""The little of Stripe's API this project actually calls.

Two form-encoded requests over `requests`, which is already a dependency, in
place of the official SDK. That is a deliberate trade recorded here rather than
left as an omission: the SDK is a large dependency, and this is the
internet-facing host whose compromise the threat model already assumes. Adding
it would mean more code running next to a Stripe secret key for the sake of
what fits on one screen.

The secret key never leaves this process. The bot holds no Stripe credential at
all, and nothing in `src/bot*.py` imports this module.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from dashboard import stripe_events

logger = logging.getLogger(__name__)

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Stripe pins behaviour to the version the account is on unless asked
# otherwise. Naming it means a Stripe-side upgrade cannot silently change the
# shape of what `stripe_events.normalise` reads.
STRIPE_API_VERSION = "2024-06-20"


class StripeAPIError(Exception):
    """Stripe could not be reached, or refused the call."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class StripeClient:
    def __init__(self, secret_key: str, *, timeout: int = 10):
        self._secret_key = secret_key
        self.timeout = timeout
        self._session = requests.Session()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._secret_key}",
            "Stripe-Version": STRIPE_API_VERSION,
        }

    def get_subscription(self, subscription_id: str) -> dict:
        """Read a subscription's *current* state.

        Called on every webhook rather than trusting the event's own snapshot,
        and the reason is ordering. Stripe promises nothing about the order
        events arrive in, and `event.created` has one-second resolution — so two
        events for one subscription in the same second are indistinguishable in
        age, and the bot's ordering guard drops the second one. Fetching means
        whichever delivery does apply carries current truth rather than a
        snapshot that may already be stale, so a dropped sibling costs nothing.

        It also means a redelivery three days later writes today's state, not
        the state at the moment the event fired.

        The cost is one API call per webhook and one more way to fail, and the
        failure is the safe one: an exception here becomes a non-2xx to Stripe,
        which retries for three days.
        """
        # Checked at the point the URL is built, as well as by the caller. This
        # value arrives inside a signature-verified body, so it came from
        # Stripe — which is why this should never fire, and not a reason to
        # skip it. A `/` or a `?` here silently turns a read of one
        # subscription into a request for something else entirely.
        if not stripe_events.valid_object_id(subscription_id):
            raise StripeAPIError("refusing to request a malformed subscription id")

        try:
            response = self._session.get(
                f"{STRIPE_API_BASE}/subscriptions/{subscription_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise StripeAPIError(f"could not reach Stripe: {error}") from error

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as error:
                raise StripeAPIError("Stripe returned an unreadable body") from error
            if not isinstance(payload, dict):
                raise StripeAPIError("Stripe returned an unexpected body")
            return payload

        # Deliberately does not log the response body. Stripe error payloads can
        # echo request parameters, and this runs on the public host.
        logger.warning(
            "Stripe refused a subscription read: %s", response.status_code
        )
        raise StripeAPIError("Stripe refused the request", response.status_code)

    def _post(self, path: str, form: list) -> dict:
        """One form-encoded POST. Stripe's API is form-encoded, not JSON."""
        try:
            response = self._session.post(
                f"{STRIPE_API_BASE}{path}",
                headers=self._headers(),
                data=form,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise StripeAPIError(f"could not reach Stripe: {error}") from error

        if response.status_code in (200, 201):
            try:
                payload = response.json()
            except ValueError as error:
                raise StripeAPIError("Stripe returned an unreadable body") from error
            if not isinstance(payload, dict):
                raise StripeAPIError("Stripe returned an unexpected body")
            return payload

        logger.warning("Stripe refused %s: %s", path, response.status_code)
        raise StripeAPIError("Stripe refused the request", response.status_code)

    def create_checkout_session(
        self,
        *,
        price_id: str,
        guild_id: str,
        actor_discord_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Start a hosted Checkout, and return the URL to send the browser to.

        Hosted Checkout by redirect, never embedded Stripe.js. The dashboard's
        CSP is `default-src 'none'` with `script-src 'self'`, and embedding
        would mean permanently allowing js.stripe.com plus a frame-src -- a
        relaxation of the tightest directive set in the app, on the one page
        that handles money. A 303 needs no CSP change at all, because the
        browser simply leaves. It also keeps card data off this infrastructure
        entirely, which is what makes PCI scope SAQ-A.

        `price_id` is looked up by the caller from a plan slug and is never
        taken from the form. See the route.

        The guild id goes into `subscription_data[metadata]`, not only
        `client_reference_id`, and the difference matters more than it looks:
        the reference id exists on the session and is gone by the first
        renewal, while subscription metadata rides along with every
        `customer.subscription.*` event for the life of the subscription. The
        binding has to survive to a renewal a year from now.
        """
        form = [
            ("mode", "subscription"),
            ("line_items[0][price]", price_id),
            ("line_items[0][quantity]", "1"),
            ("client_reference_id", str(guild_id)),
            ("subscription_data[metadata][guild_id]", str(guild_id)),
            ("subscription_data[metadata][actor_discord_id]", str(actor_discord_id)),
            ("success_url", success_url),
            ("cancel_url", cancel_url),
            # Tax is collected where it is due and is NOT included in the
            # advertised price -- the prices are created tax-exclusive, and the
            # plan cards say "+ tax" for that reason. Automatic tax refuses a
            # price whose tax_behavior is unspecified, so a plan card that
            # 500s here means a price was created wrong rather than a bug.
            ("automatic_tax[enabled]", "true"),
            # Required once automatic tax is on: without an address Stripe
            # cannot decide a rate, and the session errors rather than guessing.
            ("billing_address_collection", "required"),
            ("customer_update[address]", "auto"),
        ]
        session = self._post("/checkout/sessions", form)
        url = session.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise StripeAPIError("Stripe returned no checkout URL")
        return url

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        """Open Stripe's own billing portal for this customer.

        Cancelling, switching plan and updating a card all happen on Stripe's
        domain. That is deliberate and not laziness: every one of those is an
        action on somebody's money, and the alternative is reimplementing them
        against an API on the box this project assumes will eventually be
        compromised.
        """
        if not stripe_events.valid_object_id(customer_id):
            raise StripeAPIError("refusing to request a malformed customer id")
        session = self._post(
            "/billing_portal/sessions",
            [("customer", customer_id), ("return_url", return_url)],
        )
        url = session.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise StripeAPIError("Stripe returned no portal URL")
        return url
