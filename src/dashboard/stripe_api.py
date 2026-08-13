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
