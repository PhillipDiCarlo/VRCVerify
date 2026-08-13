"""Verifying and reading Stripe webhooks. Pure — no Flask, no network, no clock.

This is the module that decides whether a request claiming to be from Stripe
actually is. It is deliberately the smallest, most boring code in the project,
because it is the first thing a public unauthenticated request touches: until
`verify_signature` returns, the body is a string of bytes an unknown party
chose, and nothing here parses it.

Stdlib only, and the reason is worth stating rather than assuming. Signature
verification is an HMAC-SHA256 over `{timestamp}.{body}` compared with
`hmac.compare_digest` — about fifteen lines. The official SDK would bring a
large dependency onto the internet-facing host to do that and one form-encoded
POST, on the box whose compromise this project's threat model already assumes.
Every line saved there is a line nobody has to review.

**The order of operations is the security property.** Verify, then parse.
Reversed, an attacker gets our JSON parser to run on their bytes before we have
established they are entitled to send us anything at all, which is exactly the
foothold the signature exists to deny.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from hashlib import sha256
from typing import Optional

logger = logging.getLogger(__name__)

# How far a webhook's own timestamp may be from our clock. Stripe's default,
# and the number that makes a captured request worthless five minutes later:
# without it a valid signature is valid forever, and a webhook recorded off the
# wire could be replayed at any point in the future.
DEFAULT_TOLERANCE = 300

# Nothing legitimate is anywhere near this. A subscription event is a few KB;
# the cap is here so a body has a bound before it is read into memory, not
# because any real event approaches it.
MAX_BODY_BYTES = 64 * 1024

# The only events this endpoint acts on.
#
# Deliberately narrow, and not only for least privilege. A checkout completing
# emits several events within the same second — `checkout.session.completed`,
# `customer.subscription.created`, `invoice.paid` — and the bot's ordering guard
# compares Stripe's `created`, which has one-second resolution. Subscribing to
# one family of events keeps same-second pairs for a single subscription rare,
# which is the residual risk noted in `write_dashboard_stripe_subscription`.
#
# Everything else Stripe might send is acknowledged and ignored. An endpoint
# that 400s on an event type somebody enabled in the Stripe dashboard would
# start a three-day retry storm over a checkbox.
SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)


# Stripe object ids are `prefix_` plus base-something alphanumerics. Nothing
# else is ever legitimate, and this is checked before an id is interpolated
# into an API URL.
#
# Yes, the id arrives inside a signature-verified body, so it came from Stripe.
# That is an argument for *why this should never fire*, not for leaving it out:
# a path separator or a query character in this value silently turns a read of
# one subscription into a request for something else entirely, and the whole
# point of the layered design here is that no single check is load-bearing on
# its own.
_OBJECT_ID = re.compile(r"\A[A-Za-z0-9_]{1,255}\Z")


def valid_object_id(value) -> bool:
    """Is this safe to put in a URL path segment?"""
    return isinstance(value, str) and bool(_OBJECT_ID.match(value))


class SignatureError(Exception):
    """The request did not prove it came from Stripe.

    Carries a short machine reason for the log. It is never shown to the
    caller: an endpoint that explains *why* a signature failed is an endpoint
    that helps someone construct one that doesn't.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def verify_signature(
    payload: bytes,
    header: Optional[str],
    secret: str,
    *,
    now: float,
    tolerance: int = DEFAULT_TOLERANCE,
) -> None:
    """Authenticate one webhook. Raises SignatureError, or returns None.

    `now` is passed in rather than read here so the whole module stays pure and
    the expiry cases are testable without sleeping or patching a clock.

    The header looks like `t=1699999999,v1=abc...,v1=def...`. More than one v1
    is normal and expected — it is how Stripe rolls a signing secret without an
    outage — so any matching one is enough.
    """
    if not header:
        raise SignatureError("missing_signature")

    timestamps: list[str] = []
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            signatures.append(value)

    if not timestamps or not signatures:
        raise SignatureError("malformed_signature")
    # Stripe sends exactly one `t`. Several is either a broken sender or
    # someone probing which one this implementation believes — and whichever
    # answer it gave would be arbitrary. Neither ambiguity can forge a
    # signature, since the timestamp is inside the signed string, but a parser
    # with an opinion nobody chose is not one to keep.
    if len(timestamps) > 1:
        raise SignatureError("malformed_signature")
    timestamp = timestamps[0]

    try:
        issued = int(timestamp)
    except ValueError:
        raise SignatureError("malformed_signature")

    # Checked before the HMAC so a flood of stale replays costs a comparison
    # rather than a hash over an attacker-sized body.
    if abs(now - issued) > tolerance:
        raise SignatureError("timestamp_out_of_tolerance")

    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, sha256).hexdigest()

    # compare_digest, never ==. The naive comparison returns early on the first
    # wrong byte, which leaks how much of a guess was right.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("bad_signature")


def parse_event(payload: bytes) -> dict:
    """Read a webhook body. Only ever called on a body that has verified.

    Raises SignatureError rather than a parse error, because from the caller's
    point of view an unreadable body on an authenticated request is the same
    refusal with the same response: something is wrong at the sender and there
    is nothing here to act on.
    """
    try:
        event = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise SignatureError("unreadable_body")
    if not isinstance(event, dict):
        raise SignatureError("unreadable_body")
    return event


def subscription_from(event: dict) -> Optional[dict]:
    """The subscription an event is about, or None if it is not about one."""
    if event.get("type") not in SUBSCRIPTION_EVENTS:
        return None
    obj = (event.get("data") or {}).get("object")
    if not isinstance(obj, dict) or obj.get("object") != "subscription":
        return None
    # The id goes into an API URL a moment later, so it is checked here rather
    # than at the point of use alone.
    if not valid_object_id(obj.get("id")):
        return None
    return obj


def guild_id_from(subscription: dict) -> Optional[str]:
    """Which guild this subscription is for, from its own metadata.

    Read from `subscription.metadata`, not from the checkout session's
    `client_reference_id`. The distinction matters more than it looks: the
    reference id appears only on the session, so it is there for the first
    event and gone by the first renewal, while metadata set on the subscription
    rides along with every `customer.subscription.*` event for the life of the
    subscription. The guild binding has to survive to a renewal a year later.

    Returns None rather than guessing. A subscription with no guild is not
    something to attribute to a plausible one.
    """
    metadata = subscription.get("metadata")
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("guild_id")
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return None
    return raw.strip()


def price_id_from(subscription: dict) -> Optional[str]:
    """The price this subscription is on.

    Takes the first line item. These subscriptions carry exactly one — the
    checkout session is built with a single `line_items[0][price]` — and a
    subscription with several is not something this product knows how to
    describe, so the first is the honest answer rather than an arbitrary one.
    """
    items = ((subscription.get("items") or {}).get("data")) or []
    if not items or not isinstance(items[0], dict):
        return None
    price = items[0].get("price")
    if not isinstance(price, dict):
        return None
    price_id = price.get("id")
    return price_id if isinstance(price_id, str) and price_id else None


def _iso(epoch) -> Optional[str]:
    from datetime import datetime, timezone

    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalise(subscription: dict, *, event_id: str, event_created) -> Optional[dict]:
    """Turn a Stripe subscription into the payload the bot accepts.

    Nothing raw from Stripe crosses the wire to the homelab. The bot receives
    eight named fields it already knows how to validate, and never a nested
    object it would have to go digging through — which is what keeps the bot
    free of any opinion about Stripe's schema, and free to keep working when
    that schema changes.

    Note what is *not* here: no email, no customer name, no payment method, no
    address. Checkout collects an email and Stripe holds it; mirroring it into
    the bot's database would add customer PII to the one system in this project
    that holds Discord-to-VRChat identity links, to power a page that says
    "manage billing in the portal". The customer id is included because the
    billing portal needs it, and nothing else about the customer is.

    Returns None when a required field is missing, which the caller treats as
    an event it cannot act on rather than one to retry.
    """
    subscription_id = subscription.get("id")
    customer = subscription.get("customer")
    # Expanded or not, depending on how the object was fetched.
    if isinstance(customer, dict):
        customer = customer.get("id")
    status = subscription.get("status")
    price_id = price_id_from(subscription)
    period_end = _iso(subscription.get("current_period_end"))
    created = _iso(event_created)

    if not all(
        isinstance(value, str) and value
        for value in (subscription_id, customer, status, price_id, event_id)
    ):
        return None
    if period_end is None or created is None:
        return None

    return {
        "event_id": event_id,
        "event_created": created,
        "customer_id": customer,
        "subscription_id": subscription_id,
        "price_id": price_id,
        "status": status,
        "current_period_end": period_end,
        # A real bool, never a coerced one. The bot refuses anything else, and
        # this is the field that decides whether the page says "renews" or
        # "ends".
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
    }
