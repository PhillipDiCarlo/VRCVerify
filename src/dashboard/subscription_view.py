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

# Plans come from Stripe now, not from here.
#
# They used to be this module's business: three slugs, three labels, three
# hardcoded savings claims, paired with three STRIPE_PRICE_* variables. Adding
# a plan meant editing code, editing the environment, and redeploying -- three
# steps to do something that should be a Stripe dashboard change, and three
# chances for the page and the account to disagree about what is for sale.
#
# `plans_from_prices` builds them from Stripe's own price objects instead, so
# creating a price *is* publishing a plan and archiving one *is* retiring it.
# What each price says about itself lives in its metadata; see PLAN_METADATA.

# The metadata keys read off a Stripe price, all optional.
#
# Optional matters: a price with no metadata still renders, labelled from its
# own billing interval. That is what stops a plan created in a hurry from
# rendering as a blank card, and it means the metadata is presentation rather
# than configuration the page cannot work without.
PLAN_METADATA = ("label", "order", "saving", "trial_days")

# Labels for the intervals worth naming specially. Anything else is described
# generically from its own interval, which is correct if unlovely -- and being
# unlovely in the Stripe dashboard is a much cheaper problem than a plan that
# cannot be sold until someone deploys.
_INTERVAL_LABELS = {
    ("month", 1): "Monthly",
    ("month", 3): "3 months",
    ("month", 6): "6 months",
    ("month", 12): "12 months",
    ("year", 1): "12 months",
}

# Where a price with no `order` metadata sorts. Longer terms last, which is the
# order the cards were always offered in, derived rather than declared.
_INTERVAL_MONTHS = {"day": 0, "week": 0, "month": 1, "year": 12}

# Amounts ARE shown now, and the reason that changed is worth recording.
#
# They were deliberately absent while the plans were three environment
# variables: the page had no way to know what Stripe charged, so any figure on
# it was a second copy of a price, maintained by hand, on a page about money.
# The rule was "state a claim about the plan, never arithmetic".
#
# The prices are now read from Stripe on the render that displays them, so the
# figure and the charge have one source. Showing it is no longer a second copy;
# omitting it would just be a pricing page that will not say the price.
#
# Currencies whose smallest unit IS the unit -- ¥500 is 500, not 5.00.
_ZERO_DECIMAL = frozenset(
    {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
     "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
)

_CURRENCY_SYMBOLS = {
    "usd": "$", "eur": "€", "gbp": "£", "jpy": "¥",
    "cad": "CA$", "aud": "A$", "nzd": "NZ$",
}


def _format_amount(unit_amount, currency) -> Optional[str]:
    """A Stripe amount as text, or None if it cannot be rendered honestly.

    None rather than a guess, and never "0" or "—": a plan card whose price is
    wrong is worse than one that shows no price and makes the reader click
    through to Stripe, where the real figure is.
    """
    if isinstance(unit_amount, bool) or not isinstance(unit_amount, int):
        return None
    if unit_amount < 0 or not isinstance(currency, str) or not currency.strip():
        return None
    code = currency.strip().lower()
    if code in _ZERO_DECIMAL:
        figure = str(unit_amount)
    else:
        figure = f"{unit_amount / 100:.2f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    if symbol:
        return f"{symbol}{figure}"
    return f"{figure} {code.upper()}"


def _billing_period(price: dict) -> Optional[str]:
    """"per month", "per 6 months" -- the denominator under the amount."""
    recurring = price.get("recurring")
    if not isinstance(recurring, dict):
        return None
    interval = recurring.get("interval")
    if not isinstance(interval, str) or not interval:
        return None
    count = recurring.get("interval_count") or 1
    if not isinstance(count, int) or count < 1:
        count = 1
    if count == 1:
        return f"per {interval}"
    return f"per {count} {interval}s"

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
    """One purchasable plan. Attributes, because Jinja reads them cleanly.

    `price_id` is Stripe's, and it is what the form submits -- but see the
    checkout route: the submitted id is only ever accepted after being found
    in a freshly fetched list of this product's active prices. The browser
    naming a price is safe precisely because the server never trusts the name.
    """

    def __init__(
        self,
        price_id: str,
        label: str,
        saving: Optional[str] = None,
        trial_days: Optional[int] = None,
        order: int = 0,
        amount: Optional[str] = None,
        period: Optional[str] = None,
        highlight: bool = False,
    ):
        self.price_id = price_id
        self.label = label
        self.saving = saving
        self.trial_days = trial_days
        self.order = order
        # Both from Stripe's own price object, on the render that shows them.
        # `amount` is None when it could not be read; the card then omits the
        # figure rather than inventing one.
        self.amount = amount
        self.period = period
        # `highlight: 1` in a price's metadata. Presentational only -- it
        # changes no price, no order and nothing the server will accept.
        self.highlight = highlight

    @property
    def trial_note(self) -> Optional[str]:
        """The trial, in words, or None. Rendered under the label."""
        if not self.trial_days:
            return None
        # "7-day free trial", not "7-days" -- a hyphenated compound adjective
        # takes the singular however many days it names.
        return f"{self.trial_days}-day free trial"


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
        plans_unavailable: bool = False,
        trial_eligible: bool = False,
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
        # The plan list could not be read from Stripe. Distinct from an empty
        # `plans`; see build().
        self.plans_unavailable = plans_unavailable
        # Whether this server may be offered a free trial. The bot decides it
        # -- it is the only process that can see whether this guild has ever
        # held a paid plan -- and this page never derives it, only obeys it.
        # Defaults to False so a payload without the key, from a bot that
        # predates this, offers nobody a trial.
        self.trial_eligible = trial_eligible

    # THE STATUS CHIP AND THE FACT LIST (#141 phase 1)
    # ------------------------------------------------
    # Eight of this page's states used to be eight paragraphs of grey prose
    # under one heading, so "you are being charged twice" and "verification is
    # free" arrived in the same voice at the same weight. Every billing page
    # gathered as reference does the same two things instead -- a chip on the
    # plan name saying which state this is, and a labelled list of the facts
    # underneath. Superhuman, Base44, Rise and Grammarly all land on it.
    #
    # Built here rather than branched in the template for the reason the
    # module docstring already gives about `state`: this is the page that
    # takes money, and a condition grown inside a template branch is how two
    # of these end up disagreeing about whether somebody has paid.

    #: The chip's tone. Maps to a class, never to a colour -- the stylesheet
    #: owns which token each tone resolves to, and `test_contrast.py` owns
    #: whether that token is legible where it is drawn.
    _CHIP = {
        "stripe": ("Active", "ok"),
        "discord": ("Active", "ok"),
        "past_due": ("Payment failed", "warn"),
        "both": ("Charged twice", "warn"),
        "pending": ("Confirming", "muted"),
        "unavailable": ("Unknown", "muted"),
    }

    @property
    def chip(self) -> Optional[dict]:
        """The state, as a word and a tone, or None where there is no status.

        `off` and the free/lapsed default get no chip: "not subscribed" is not
        a status worth stamping, and a grey pill saying "Free" next to a Buy
        button reads as a downgrade rather than as a fact.
        """
        # Cancelled-but-still-running is not a state of its own -- `build()`
        # keeps it inside `stripe` and marks it by setting `ends_on` instead
        # of `renews_on`. It is worth its own word here, because "Active" on a
        # subscription that stops next month is true and unhelpful, and the
        # fact list underneath says "Premium until" rather than "Renews" for
        # exactly the same reason.
        if self.state == "stripe" and self.ends_on:
            return {"label": "Cancelled", "tone": "muted"}

        found = self._CHIP.get(self.state)
        if found is None:
            return None
        label, tone = found
        return {"label": label, "tone": tone}

    @property
    def facts(self) -> tuple:
        """`(label, value)` rows for the fact list, in reading order.

        Only what this page actually knows. A row whose value is missing is
        omitted rather than rendered as a dash: an empty row on a billing page
        invites the reader to wonder what should be in it, and the honest
        answer is that we were never told.

        "Renews" and "Ends" are never both present -- they are different
        promises about somebody's money, and `build()` sets exactly one.
        """
        if self.state in ("off", "unavailable", "pending"):
            return ()

        rows = []
        if self.state == "discord":
            # `build()` passes no `plan_label` for this branch -- Discord does
            # not tell us a price id, so there is no cadence to name. But the
            # server plainly HAS Premium, and saying so is the whole job of
            # this list: without this row the page states which payment route
            # is in use and never states what was bought.
            #
            # Caught by an adversarial pass over all three phases, and it was
            # a regression this issue introduced: the sentence "This server
            # has VRCVerify Premium, bought through Discord" was deleted on
            # the assumption the fact list carried it, and it did not.
            rows.append(("Plan", self.plan_label or UNKNOWN_PLAN_LABEL))
        elif self.plan_label:
            rows.append(("Plan", self.plan_label))

        if self.state == "discord":
            rows.append(("Billed through", "Discord"))
        elif self.state == "both":
            # Named rather than implied. An admin cannot go and cancel the
            # right one without being told there are two.
            rows.append(("Billed through", "Card and Discord"))
        elif self.state in ("stripe", "past_due"):
            rows.append(("Billed through", "Card"))

        if self.renews_on:
            # "Due to renew" while a payment is failing, not "Renews". Stripe
            # is retrying the card and may yet give up, so the flat assertion
            # is a promise this page cannot make -- directly under a notice
            # saying the last payment did not go through. The prose this
            # replaced hedged with the same three words; dropping the hedge
            # was an accident of compressing it into a label.
            label = "Due to renew" if self.state == "past_due" else "Renews"
            rows.append((label, self.renews_on))
        elif self.ends_on:
            rows.append(("Premium until", self.ends_on))
        return tuple(rows)

    @property
    def winback(self) -> Optional[dict]:
        """The lapsed state, as something to act on rather than a status line.

        `ended_on` exists so this page can say "your subscription ended on the
        3rd" instead of pretending the server was never a customer. It was
        rendering as one more muted sentence at the foot of a card, which
        wastes the one moment on this page where the reader has already
        decided to pay once before.
        """
        if not self.ended_on:
            return None
        return {
            "when": self.ended_on,
            # The plan may not be known -- the bot sends the price id and a
            # price that has since been archived resolves to nothing. The
            # sentence has to work either way rather than saying "your None
            # subscription".
            "plan": self.last_plan_label,
        }

    def trial_days_for(self, plan) -> Optional[int]:
        """The trial this plan may actually grant, or None.

        The single place the two halves meet: a plan carries a trial length
        from its Stripe metadata, and this server may or may not be allowed
        one. Both the card and the checkout route ask this rather than reading
        `plan.trial_days` directly, so there is one answer and not two that can
        disagree about whether somebody's first month is free.
        """
        if not self.trial_eligible:
            return None
        return plan.trial_days

    def trial_note_for(self, plan) -> Optional[str]:
        """The words under the plan label, or None if no trial is on offer."""
        return plan.trial_note if self.trial_days_for(plan) else None

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


def _positive_int(raw) -> Optional[int]:
    """A metadata value as a positive int, or None for anything else.

    Metadata is free text typed into a web form, so every value here is
    attacker-adjacent in the mildest possible sense and typo-adjacent in a very
    real one. Nothing raises: a price whose `trial_days` says "seven" renders
    without a trial rather than 500ing the page for every server.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return None
    value = int(raw.strip())
    return value if value > 0 else None


def _interval_label(price: dict) -> str:
    """What to call a price that did not name itself."""
    recurring = price.get("recurring")
    if not isinstance(recurring, dict):
        return UNKNOWN_PLAN_LABEL
    interval = recurring.get("interval")
    count = recurring.get("interval_count") or 1
    if not isinstance(count, int) or count < 1:
        count = 1
    named = _INTERVAL_LABELS.get((interval, count))
    if named:
        return named
    if not isinstance(interval, str) or not interval:
        return UNKNOWN_PLAN_LABEL
    if count == 1:
        return f"Every {interval}"
    return f"Every {count} {interval}s"


def _interval_rank(price: dict) -> int:
    """Roughly how long a price's term is, in months, for default ordering."""
    recurring = price.get("recurring")
    if not isinstance(recurring, dict):
        return 0
    count = recurring.get("interval_count") or 1
    if not isinstance(count, int) or count < 1:
        count = 1
    return _INTERVAL_MONTHS.get(recurring.get("interval"), 0) * count


def plan_from_price(price: dict) -> Optional[Plan]:
    """One Stripe price as a plan card, or None if it cannot be sold.

    None for a price with no id: everything else about a price degrades to a
    default, but an id is what the form submits and what checkout looks up, so
    a price without one is not a plan with a cosmetic problem.
    """
    price_id = price.get("id")
    if not isinstance(price_id, str) or not price_id:
        return None
    metadata = price.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    label = metadata.get("label")
    if not isinstance(label, str) or not label.strip():
        label = _interval_label(price)

    saving = metadata.get("saving")
    if not isinstance(saving, str) or not saving.strip():
        saving = None
    else:
        saving = saving.strip()

    order = _positive_int(metadata.get("order"))
    highlight = str(metadata.get("highlight") or "").strip().lower()
    return Plan(
        price_id=price_id,
        label=label.strip(),
        saving=saving,
        trial_days=_positive_int(metadata.get("trial_days")),
        order=order if order is not None else _interval_rank(price),
        amount=_format_amount(price.get("unit_amount"), price.get("currency")),
        period=_billing_period(price),
        highlight=highlight in {"1", "true", "yes"},
    )


def plans_from_prices(prices) -> tuple:
    """The plan cards for a product's active prices, in the order to show them.

    Sorted by `order` metadata where set and by term length otherwise, with the
    price id as the final tiebreak so the page does not reshuffle itself
    between renders when two plans sort equal. A stable order matters more than
    it sounds on a page of buttons that charge money: cards that move between
    two loads are cards somebody clicks by muscle memory and gets wrong.
    """
    if not isinstance(prices, (list, tuple)):
        return ()
    plans = []
    for price in prices:
        if not isinstance(price, dict):
            continue
        plan = plan_from_price(price)
        if plan is not None:
            plans.append(plan)
    plans.sort(key=lambda plan: (plan.order, plan.price_id))
    return tuple(plans)


def plan_label_for(price_id: Optional[str], plans=()) -> str:
    """The words for a price id, degrading to a generic label.

    Looked up against the plans currently on offer. A miss is ordinary rather
    than exceptional and must never read as "not subscribed" -- see
    UNKNOWN_PLAN_LABEL. It is in fact *more* ordinary now than it was with a
    static table: retiring a plan means archiving its price, and `list_prices`
    asks only for active ones, so everyone still paying for a retired plan
    lands here. Their subscription is real; only the label is unknown.
    """
    if not price_id:
        return UNKNOWN_PLAN_LABEL
    for plan in plans:
        if plan.price_id == price_id:
            return plan.label
    return UNKNOWN_PLAN_LABEL


def build(
    settings: Optional[dict],
    *,
    application_id: Optional[str],
    plans=(),
    plans_unavailable: bool = False,
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

    `plans` is what Stripe currently sells, already converted by
    `plans_from_prices`. It is passed in rather than fetched here so this
    module stays pure -- no network, no clock, no configuration -- which is
    what lets every state below be built in a test without any of them.

    `plans_unavailable` says the fetch FAILED, which is a different thing from
    it returning nothing, and the difference is the whole reason for the flag.
    Empty means "there is nothing to sell"; unavailable means "we cannot tell
    what there is to sell". Collapsing the second into the first renders a page
    that quietly states Stripe is not an option during a blip, and the admin
    goes and buys through Discord instead -- which is not a wrong statement
    about their subscription, but is a wrong statement about their choices.
    Buttons are withheld either way; only the sentence differs.

    `stripe_configured` is the dashboard's own kill switch; the bot's is read
    from the payload. Both must be on before a card is offered, and they are
    separate switches on separate hosts on purpose.
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
    # Two different ways a subscription stops renewing, and only one of them
    # sets `cancel_at_period_end`.
    #
    # "Cancel at period end" is the customer's own choice in the portal: the
    # subscription stays `active` and the flag goes up. Cancelling outright --
    # in the Stripe dashboard, or by Stripe giving up on an unpaid one -- sets
    # the STATUS to `canceled` and leaves that flag FALSE, because there is no
    # future period end to cancel at any more.
    #
    # Reading only the flag therefore put a live cancellation on the "renews"
    # branch, and the page told a customer they would be billed again on a date
    # nothing was going to bill them. Found 2026-08-18 by cancelling a real
    # subscription and reading the page it produced, which is the only way this
    # was ever going to surface -- both halves were individually correct.
    #
    # `current_period_end` does not move when a subscription is cancelled, so
    # the date is right in both cases; it is the promise attached to it that
    # differs. The bot grants premium until that date either way
    # (`_stripe_row_is_paid` admits `canceled` while the period is unexpired),
    # so this changes the words and not the entitlement.
    cancelling = bool(stripe_block.get("cancel_at_period_end")) or status == "canceled"
    label = plan_label_for(stripe_block.get("price_id"), plans)

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
        plans=tuple(plans) if stripe_on else (),
        # Only worth saying while Stripe is otherwise on: with either kill
        # switch off there are no card plans to be unable to load, and
        # apologising for the absence of something deliberately switched off
        # would be the page inventing a fault.
        plans_unavailable=plans_unavailable and stripe_on,
        store_url=store_url,
        discord_command=True,
        # A lapsed subscription leaves its row behind on purpose, so the page
        # can say when it ended rather than pretending the server was never a
        # customer. Resubscribing is then a status change, not a re-onboarding.
        ended_on=period_end if status else None,
        last_plan_label=label if status else None,
        # The bot's answer, obeyed rather than recomputed. Only meaningful in
        # this state anyway: every other one either already has a subscription
        # or cannot take a purchase, and none of them renders a plan card.
        #
        # `stripe_on` is required as well, so a payload from a bot with Stripe
        # switched off cannot advertise a trial that the checkout route --
        # which re-reads this same field -- would then refuse to honour.
        trial_eligible=bool(stripe_block.get("trial_eligible")) and stripe_on,
    )


class PublicPricingPage:
    """What a signed-out stranger is shown about what Premium costs (#188).

    A deliberately thinner thing than `SubscriptionPage`. That page answers
    "what is THIS server paying and what may it do next", and every one of its
    interesting properties needs a guild. This one answers "what does the
    product cost", which needs only the product -- so it takes prices and
    nothing else, and there is no guild, no session and no bot call anywhere
    behind it.

    THE TRIAL IS THE ONE PLACE THIS PAGE MUST BE VAGUER THAN THE PRIVATE ONE,
    and it is worth understanding why before making it more specific.

    `trial_days_for()` is the single place a plan's trial length meets a
    server's eligibility, precisely so the card and the checkout route cannot
    disagree about whether somebody's first month is free. A public page has no
    server, so it cannot evaluate the second half -- and a card promising "14
    days free" to a reader whose server already used its trial is a promise
    broken at the moment they hand over a card number, which is the failure
    that comment exists to prevent.

    So the public page states only that a trial EXISTS on some plan, and sends
    the reader to the dashboard to find out whether theirs qualifies. It never
    prints a number of days next to a price.
    """

    def __init__(self, plans=(), *, plans_unavailable: bool = False,
                 stripe_configured: bool = True):
        self.plans = tuple(plans)
        # A failed read is an apology, never "nothing is for sale". The rule is
        # the subscription route's, and it bites harder here: an admin seeing
        # an empty list has a working bot in front of them, while a stranger
        # concludes the product is dead and closes the tab.
        self.plans_unavailable = bool(plans_unavailable) and stripe_configured
        self.stripe_configured = bool(stripe_configured)

    @property
    def offers_plans(self) -> bool:
        return bool(self.plans)

    @property
    def unavailable(self) -> bool:
        """Stripe was asked and did not answer. Distinct from having no plans."""
        return self.plans_unavailable and not self.plans

    @property
    def mentions_a_trial(self) -> bool:
        """Does ANY offered plan carry a trial?

        Deliberately a boolean rather than a length. See the class docstring:
        the number of days is only true for a server this page cannot see.
        """
        return any(plan.trial_days for plan in self.plans)


def build_public_pricing(plans=(), *, plans_unavailable: bool = False,
                         stripe_configured: bool = True) -> PublicPricingPage:
    """Pure, like everything else here: prices in, a page object out."""
    return PublicPricingPage(
        plans,
        plans_unavailable=plans_unavailable,
        stripe_configured=stripe_configured,
    )
