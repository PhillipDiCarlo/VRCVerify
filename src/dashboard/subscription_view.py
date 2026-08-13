"""Turns the bot's settings payload into the Subscriptions page. Pure.

No Flask, no network, no clock — `now` arrives as an argument. That is what
makes every state below testable without a request, which matters more here
than anywhere else on the site: this is the page that takes money, and the
difference between two of its states is the difference between showing a
paying customer a Buy button and not.

THE STATES, AND WHY THEY ARE NOT A BOOLEAN
------------------------------------------
`state` is what the template switches on, so adding one is a change here rather
than a new branch scattered through the HTML:

* ``off``          — Stripe is switched off in the bot. Discord path only.
* ``unavailable``  — we could not read the plan. Never rendered as "not
                     subscribed": "not subscribed" next to a Buy button is how
                     a paying customer is sold a second subscription.
* ``none``         — genuinely not subscribed. Plans and both buy paths.
* ``discord``      — subscribed through Discord. No card buttons at all, which
                     is half of what keeps double billing rare.
* ``stripe``       — subscribed by card. Plan, renewal date, portal link.
* ``past_due``     — subscribed by card, last payment failed. Premium is still
                     on while Stripe retries. Not an error and not silence.
* ``both``         — paying on both platforms. Warned, never auto-cancelled.
* ``pending``      — Stripe has just bounced the browser back from checkout and
                     the webhook has not landed yet. Offers no way to buy,
                     because the alternative is three Buy buttons under a
                     thank-you message.

`grandfathered` rides alongside rather than being an eighth state, because it
is orthogonal: a grandfathered server can be in any of the unsubscribed ones
and the reassurance is the same.

WHAT THIS MODULE MAY NOT DO
---------------------------
* **It never decides whether a server is premium.** `premium.premium` from the
  bot is the single answer; the `stripe` block only explains *why*. Re-deriving
  it here would create a second gate that can disagree with the enforcing one.
* **It holds no prices.** Stripe knows what it charges. A second copy of an
  amount on a page about money is a second thing to be wrong, which is the same
  reasoning that keeps the Discord tier's price out of the bot and lets
  Discord render its own label.
* **It maps a price id to a plan, never the reverse.** The reverse direction is
  a checkout concern and belongs where the form is handled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# The three plans, in the order they are offered. Slug, label, and the sentence
# that justifies the longer terms existing at all.
#
# No amounts. The saving is stated as a claim about the plan rather than as
# arithmetic this file could get out of step with Stripe.
PLANS = (
    ("monthly", "Monthly", None),
    ("six_months", "6 months", "Save about 10%"),
    ("yearly", "12 months", "Save about 20%"),
)

# What an unrecognised price id renders as.
#
# This will happen: a plan switched in the billing portal, a price replaced
# during a pricing change, an id rotated between test and live. The
# subscription is real and paid for and only the *label* is unknown, so it
# degrades to this and never to "not subscribed" — which would switch off a
# paying customer over a missing environment variable.
UNKNOWN_PLAN_LABEL = "Premium"

STORE_URL = "https://discord.com/application-directory/{app_id}/store/{sku_id}"


class Plan:
    """One purchasable plan. Attributes, because Jinja reads them cleanly."""

    def __init__(self, slug: str, label: str, saving: Optional[str]):
        self.slug = slug
        self.label = label
        self.saving = saving


class SubscriptionPage:
    def __init__(
        self,
        state: str,
        *,
        grandfathered: bool = False,
        premium: bool = False,
        plan_label: Optional[str] = None,
        renews_on: Optional[str] = None,
        ends_on: Optional[str] = None,
        plans: tuple = (),
        store_url: Optional[str] = None,
        discord_command: bool = False,
        on_discord: bool = False,
        card_count: int = 0,
        ended_on: Optional[str] = None,
        last_plan_label: Optional[str] = None,
    ):
        self.state = state
        self.grandfathered = grandfathered
        self.premium = premium
        self.plan_label = plan_label
        # Exactly one of these is ever set. "Renews on" and "ends on" are
        # different promises and a page that says the wrong one is a page
        # lying about somebody's money.
        self.renews_on = renews_on
        self.ends_on = ends_on
        self.plans = plans
        self.store_url = store_url
        self.discord_command = discord_command
        # Which platforms are involved, for the double-billing notice. It has
        # to name both, or an admin cannot tell which one to go and cancel.
        self.on_discord = on_discord
        self.card_count = card_count
        # A subscription that has lapsed: when it ended and what it was.
        self.ended_on = ended_on
        self.last_plan_label = last_plan_label

    @property
    def offers_card(self) -> bool:
        """Should the plan cards render?

        Both halves are required, and the second is not redundant: with Stripe
        switched off on either host the state is still `none` -- the server
        genuinely has no subscription -- but there are no plans to show, and
        checking only the state rendered an empty "Pay by card" section headed
        by a promise the page could not keep.
        """
        return self.state == "none" and bool(self.plans)

    @property
    def offers_portal(self) -> bool:
        """Is there a Stripe subscription to manage?"""
        return self.state in {"stripe", "past_due", "both"}


def _format_date(raw: Optional[str]) -> Optional[str]:
    """An ISO instant as `3 February 2026`, or None.

    Deliberately a date and not a time. Stripe's period end is a moment, but an
    admin reading "renews on 3 February" and being charged a few hours either
    side of midnight in their own timezone has been told the truth; rendering
    an exact UTC timestamp would be precision the reader cannot use and would
    invite "but it says 00:41" support questions.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # %-d is not portable to Windows, and this runs on Linux in production and
    # under tests on Windows, so the day is trimmed by hand.
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def plan_label_for(price_id: Optional[str], plan_slug: Optional[str]) -> str:
    """The words for a price id, degrading to a generic label.

    `plan_slug` is what the dashboard's own price table made of the id, or None
    when it recognised nothing. None is not an error — see UNKNOWN_PLAN_LABEL.
    """
    if plan_slug:
        for slug, label, _saving in PLANS:
            if slug == plan_slug:
                return label
    return UNKNOWN_PLAN_LABEL


def build(
    settings: Optional[dict],
    *,
    application_id: Optional[str],
    plan_slug: Optional[str] = None,
    stripe_configured: bool = True,
    just_bought: bool = False,
    now: Optional[datetime] = None,
) -> SubscriptionPage:
    """The whole page, from the bot's settings payload.

    `settings` is None when the bot could not answer. That is its own state and
    is never collapsed into "not subscribed" — the rule the settings page
    already follows for a failed read, applied here with more force, because
    the wrong answer on this page costs somebody money rather than a confusing
    toggle.

    `just_bought` is set when Stripe has bounced the browser back from a
    completed checkout. It matters more than it sounds: the webhook that makes
    the subscription real may not have landed yet, so the payload still says
    "not subscribed" -- and rendering that literally would put three Buy
    buttons directly under a thank-you message, at the one moment somebody has
    demonstrably just paid. Found by probing the page rather than by reading
    it.

    `plan_slug` is the caller's lookup of the stored price id against its own
    price table, passed in rather than looked up here so this module needs no
    configuration. `stripe_configured` is the dashboard's own kill switch; the
    bot's is read from the payload. Both must be on before a card is offered,
    and they are separate switches on separate hosts on purpose.
    """
    if settings is None:
        return SubscriptionPage("unavailable")

    premium_block = settings.get("premium")
    if not isinstance(premium_block, dict) or "enforced" not in premium_block:
        # A payload without a premium block is not a payload saying "free" --
        # it is one this page cannot read. Treating a missing `enforced` as
        # falsy would render "every feature is available at no charge" to a
        # server that may well be paying, which is the same class of lie as
        # rendering a failed read as "not subscribed".
        return SubscriptionPage("unavailable")
    stripe_block = settings.get("stripe") or {}

    enforced = bool(premium_block.get("enforced"))
    premium = bool(premium_block.get("premium"))
    grandfathered = bool(premium_block.get("grandfathered"))
    sku_id = premium_block.get("sku_id")

    store_url = (
        STORE_URL.format(app_id=application_id, sku_id=sku_id)
        if sku_id and application_id
        else None
    )

    # The tier is switched off entirely: every gate answers "allowed", so there
    # is nothing to sell and a page offering to sell it would be selling
    # something already free.
    if not enforced:
        return SubscriptionPage("off", premium=True, grandfathered=grandfathered)

    # Two switches on two hosts, and both must be on. The bot's says whether it
    # would record a purchase; the dashboard's says whether it can take one.
    # Offering a card when either is off sells something that cannot complete.
    stripe_on = bool(stripe_block.get("enabled")) and stripe_configured
    stripe_active = bool(stripe_block.get("active"))
    status = stripe_block.get("status")
    # More than one *card* subscription is its own double-billing case, and the
    # only one the table can count directly.
    card_count = int(stripe_block.get("active_count") or 0)
    # Whether Discord alone grants premium. Sent explicitly by the bot rather
    # than deduced here -- `premium` is an OR and deducing the halves from it
    # is exactly the kind of second opinion that drifts.
    discord = bool(premium_block.get("discord"))

    period_end = _format_date(stripe_block.get("current_period_end"))
    cancelling = bool(stripe_block.get("cancel_at_period_end"))
    label = plan_label_for(stripe_block.get("price_id"), plan_slug)

    # Exactly one of these is ever set, and they are different promises.
    renews_on = None if cancelling else period_end
    ends_on = period_end if cancelling else None

    # Paying twice: on both platforms, or on two cards. Premium stays granted
    # either way -- being double-billed must not also break something -- and
    # the page warns rather than acting. Nothing anywhere cancels or refunds on
    # a customer's behalf; that is a category of bug that costs real money in
    # the wrong direction.
    if (stripe_active and discord) or card_count > 1:
        return SubscriptionPage(
            "both",
            premium=True,
            grandfathered=grandfathered,
            plan_label=label if stripe_active else None,
            renews_on=renews_on,
            ends_on=ends_on,
            store_url=store_url,
            discord_command=True,
            on_discord=discord,
            card_count=card_count,
        )

    if stripe_active:
        return SubscriptionPage(
            "past_due" if status == "past_due" else "stripe",
            premium=True,
            grandfathered=grandfathered,
            plan_label=label,
            renews_on=renews_on,
            ends_on=ends_on,
        )

    if premium:
        # Premium with no active card subscription means Discord. Deliberately
        # no card buttons: offering one here is precisely how a server that
        # already pays ends up paying twice.
        return SubscriptionPage(
            "discord",
            premium=True,
            grandfathered=grandfathered,
            store_url=store_url,
            discord_command=True,
            on_discord=True,
        )

    if just_bought:
        # Deliberately says nothing certain about payment. The checkout
        # redirect is a hint, never evidence -- only the webhook is that -- so
        # the copy promises a wait rather than a subscription, and offers no
        # way to buy again while we cannot tell.
        return SubscriptionPage(
            "pending",
            grandfathered=grandfathered,
            store_url=store_url,
        )

    return SubscriptionPage(
        "none",
        grandfathered=grandfathered,
        plans=tuple(Plan(*plan) for plan in PLANS) if stripe_on else (),
        store_url=store_url,
        discord_command=True,
        # A lapsed subscription leaves its row behind on purpose, so the page
        # can say when it ended rather than pretending the server was never a
        # customer. Resubscribing is then a status change, not a re-onboarding.
        ended_on=period_end if status else None,
        last_plan_label=label if status else None,
    )
