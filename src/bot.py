import os
import json
import asyncio
import logging
import secrets
import hashlib
import random
import string
import re
import time
from typing import Optional
from dataclasses import dataclass
from html import escape
from urllib.parse import urlparse
from types import SimpleNamespace

import discord
from discord import app_commands, Embed
from discord.ext import commands
from discord.ui import View, Button, Select

import pika
from pika.exceptions import AMQPError
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    text,
    inspect,
    func,
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from dotenv import load_dotenv
from locales import localizations, LANGUAGE_CODES
import bot_api
import heartbeat
from log_safety import install_log_scrubbing


# --- Localization Helpers ---
def get_locale(interaction: discord.Interaction) -> str:
    """Return best matching locale code or fallback to English."""
    loc = getattr(interaction, "locale", None)
    return loc if loc in LANGUAGE_CODES else "en-US"


def get_message(key: str, interaction: discord.Interaction, **kwargs) -> str:
    """Fetch localized template and format with kwargs."""
    locale = get_locale(interaction)
    template = localizations.get(locale, localizations["en-US"]).get(key)
    if template is None:
        template = localizations["en-US"].get(key, key)
    return template.format(**kwargs)

# -------------------------------------------------------------------
# Load environment variables
# -------------------------------------------------------------------
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")


RABBITMQ_REQUEST_QUEUE = os.getenv("RABBITMQ_QUEUE_NAME") # The queue to which we send verification requests (the "inbound" queue for vrc_online_checker).
RABBITMQ_RESULT_QUEUE = os.getenv("RABBITMQ_RESULT_QUEUE") # The queue from which we *receive* the verification results back from vrc_online_checker.
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")

# The group-invite worker's own pair of queues (issue #49). Separate from the
# verification queues above because two consumers on one queue split its
# messages round-robin, so the age checker would swallow half the invite jobs.
# Defaults match vrc_group_inviter.py's; tests pin that the two agree.
RABBITMQ_GROUP_INVITE_QUEUE = os.getenv(
    "RABBITMQ_GROUP_INVITE_QUEUE", "vrcverify_group_invites"
)
RABBITMQ_GROUP_INVITE_RESULT_QUEUE = os.getenv(
    "RABBITMQ_GROUP_INVITE_RESULT_QUEUE", "vrcverify_group_invite_results"
)

# The VRChat account an admin has to invite to their group. Read here, not just
# in the worker, because the *bot* is what tells the dashboard which account to
# name -- and naming the wrong one produces a "not a member" verify failure
# with nothing on screen explaining why. Display names are not unique, so the
# usr_ id is the part that matters.
#
# Unset means the operator has not provisioned the account, and the feature
# refuses to start a verification rather than sending an admin to invite
# nobody.
INVITE_VRCHAT_USER_ID = (os.getenv("INVITE_VRCHAT_USER_ID") or "").strip() or None


# Priority levels on the request queue, so premium servers are served ahead of
# free ones when a backlog forms. RabbitMQ only reorders messages already
# waiting, so this changes nothing while the queue is empty — which is the
# normal case — and everything when the single shared VRChat account falls
# behind, which is the case worth paying for.
#
# NOT an env var, deliberately. vrc_online_checker.py declares the same queue
# and the arguments must match exactly or every declare fails with 406
# PRECONDITION_FAILED, taking down both services at once. Changing this value
# is itself a migration — the queue has to be deleted and recreated — so it
# must not look like something that can be tuned from config.
# tests/test_priority_queue.py pins that both services agree.
QUEUE_MAX_PRIORITY = 5
PREMIUM_REQUEST_PRIORITY = QUEUE_MAX_PRIORITY
DEFAULT_REQUEST_PRIORITY = 0

# RabbitMQ's reply code for "this queue already exists with other arguments".
QUEUE_ARGUMENT_MISMATCH_CODE = 406


def request_queue_arguments() -> dict:
    """Declaration arguments for the verification request queue.

    Both services build them here-shaped so a mismatch is a test failure rather
    than a production 406.
    """
    return {"x-max-priority": QUEUE_MAX_PRIORITY}


def is_queue_argument_mismatch(error: Exception) -> bool:
    """Is this the 406 you get from re-declaring a queue with new arguments?"""
    return getattr(error, "reply_code", None) == QUEUE_ARGUMENT_MISMATCH_CODE


def log_queue_argument_mismatch(queue_name: str) -> None:
    """Say exactly what is wrong and exactly how to fix it.

    Without this the failure is invisible: ChannelClosedByBroker subclasses
    AMQPError, so the publisher swallows it in its generic retry loop and the
    consumer reconnects forever, neither explaining why. That turns a one-line
    operator fix into a mysterious outage across both services.
    """
    logger.error(
        "Queue '%s' already exists with different arguments, so it cannot be "
        "declared with priority support (x-max-priority=%s). Verification is "
        "STOPPED until this is fixed. Stop the bot, let the checker drain the "
        "queue to zero, stop the checker, delete the '%s' queue in RabbitMQ, "
        "then start both again.",
        queue_name,
        QUEUE_MAX_PRIORITY,
        queue_name,
    )


# Donation link surfaced on the instruction panel, admin confirmations,
# and the one-time milestone DM.
KOFI_URL = "https://ko-fi.com/italiandogs"
# Verifications a guild must complete before the one-time owner thank-you DM.
MILESTONE_VERIFICATION_COUNT = 100
# Instruction panels are refreshed concurrently rather than one guild at a time.
# Two independent limits apply:
#
# CONCURRENCY caps how many edits are in flight, so a few thousand panels don't
# serialize behind each other's round trips.
#
# RATE caps how many we *start* per second. A concurrency cap alone does not
# bound the request rate: these edits often fail fast (403/404 come back in tens
# of milliseconds), so even a handful of workers can push well past Discord's
# ~50 req/s global ceiling. Exceeding it throttles the whole bot — verification
# results and command replies included — not just this loop. Default 25 leaves
# half the global budget for real traffic.
def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


# How long a group verification may sit unanswered before the dashboard stops
# calling it "checking". The round trip is a queue hop each way plus a couple of
# VRChat calls, so a few seconds is normal and two minutes means the worker is
# down, wedged, or was never deployed. Without this the page shows a spinner
# for ever and the admin has nothing to act on.
#
# Evaluated when the row is read, not by a sweep: a state that expires on its
# own needs no scheduler to be correct, and there is nothing to go wrong at
# three in the morning.
GROUP_VERIFY_TIMEOUT_SECONDS = _int_env("GROUP_VERIFY_TIMEOUT_SECONDS", 120)

# The same idea for one member's invite: how long a request may sit unanswered
# before the member is told it did not work rather than watching "asking
# VRChat..." forever. Shorter than a setup check because a setup check may sit
# behind a join and two get_group calls, while an invite is two calls at most.
GROUP_INVITE_TIMEOUT_SECONDS = _int_env("GROUP_INVITE_TIMEOUT_SECONDS", 90)

# Minimum gap between two invite ATTEMPTS by the same member in the same
# server. Deliberately NOT "for the same group" (issue #208): an attempt spent
# the invite account's shared budget whichever group it was about, so keying
# the clock on the group let an admin alternate between two of them and their
# members pay no cooldown at all. See group_invite_refusal, which is where the
# distinction between a verdict (group-specific) and this clock (not) is made.
#
# What it bounds is retries. A member whose invite failed transiently (VRChat
# down, the server's permission broken) gets the button back so they can try
# again, and this is what stops that becoming a tight loop against the invite
# account's shared rate budget. It bounds the re-OFFER of a settled outcome
# too, since GROUP_INVITE_FINAL_STATES is empty and every settled state is
# asked again once this has lapsed. Five minutes, because it is measured
# against a human deciding to press a button again rather than against a
# machine: long enough that mashing achieves nothing, short enough that someone
# who hit a blip is not locked out of a feature they are paying for.
GROUP_INVITE_COOLDOWN_SECONDS = _int_env("GROUP_INVITE_COOLDOWN_SECONDS", 300)

INSTRUCTIONS_REFRESH_CONCURRENCY = _int_env("INSTRUCTIONS_REFRESH_CONCURRENCY", 10)
INSTRUCTIONS_REFRESH_RATE = _int_env("INSTRUCTIONS_REFRESH_RATE", 25)

# A server that ran /vrcverify_setup but never posted an instruction panel is
# half-configured: members have no button to click. After a grace period we DM
# the admin who ran setup, once.
#
# The spacing and per-sweep cap exist because a blast of DMs is exactly what
# Discord's anti-spam heuristics look for. A backlog, clock skew, or a long
# outage must never turn into a burst — it trickles out over later sweeps.
PANEL_NUDGE_GRACE_HOURS = _int_env("PANEL_NUDGE_GRACE_HOURS", 48, minimum=0)
PANEL_NUDGE_INTERVAL = _int_env("PANEL_NUDGE_INTERVAL", 3600)
PANEL_NUDGE_MAX_PER_SWEEP = _int_env("PANEL_NUDGE_MAX_PER_SWEEP", 20)
PANEL_NUDGE_DM_SPACING = _float_env("PANEL_NUDGE_DM_SPACING", 2.0)

# Instruction panel buttons carry fixed custom_ids so a single bot.add_view()
# call routes clicks for every panel ever posted, instead of the bot having to
# re-edit all of them on boot just to hand out ids it recognises.
#
# Bump this when the button set changes: the custom_ids change with it, panels
# still carrying an older version stop matching the registered view, and the
# startup pass re-edits exactly those.
INSTRUCTIONS_VIEW_VERSION = 1
BEGIN_VERIFICATION_CUSTOM_ID = f"vrcverify:begin:v{INSTRUCTIONS_VIEW_VERSION}"
UPDATE_NICKNAME_CUSTOM_ID = f"vrcverify:nickname:v{INSTRUCTIONS_VIEW_VERSION}"

# Discord's "thread is archived" rejection. Worth naming because it is by far
# the most common way a panel degrades without anyone noticing.
ARCHIVED_THREAD_ERROR_CODE = 50083

# -------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("pika").setLevel(logging.WARNING)
# discord.py, like pika, is a library whose internals are not this bot's logs.
#
# It used to silence itself by accident: Client.run called its own
# setup_logging with root=True, which set the ROOT logger to INFO and so
# quietly overrode LOG_LEVEL for everything. Opting out of that (log_handler
# =None, needed so it cannot install a handler behind install_log_scrubbing)
# made LOG_LEVEL work as documented -- and LOG_LEVEL=DEBUG then buried every
# line this bot writes under gateway keepalives and raw event payloads.
#
# Pinned here so the two settings are independent: LOG_LEVEL controls OUR
# logs, and turning it up for a real investigation does not cost the ability
# to read them. Raise DISCORD_LOG_LEVEL deliberately when the gateway itself
# is what is under suspicion.
logging.getLogger("discord").setLevel(
    getattr(logging, os.getenv("DISCORD_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
)
# Attacker-controlled text reaches these logs -- Discord ids, OAuth claims,
# guild ids from Stripe metadata. See log_safety.
install_log_scrubbing()
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# The invite-account roster (issue #49, phase 6)
# -------------------------------------------------------------------
# How many groups one VRChat account may hold. 100 is the platform limit; VRC+
# raises it to 200 on the account that has it, which is why the roster below
# lets an entry override this rather than assuming one number fits every
# account.
INVITE_ACCOUNT_SEAT_CAP = _int_env("INVITE_ACCOUNT_SEAT_CAP", 100)


@dataclass(frozen=True)
class InviteAccount:
    """One VRChat account that can hold groups, and the queue that reaches it.

    The queue is per-account and that is the whole point. Two workers
    consuming one queue get its messages round-robin, so an invite for a group
    only account B has joined would be handed to account A -- the same trap
    that made the invite worker take its own queue instead of sharing the age
    checker's. Routing is therefore explicit: the bot publishes each job to the
    queue belonging to the account that holds that guild's seat.
    """

    user_id: str
    queue: str
    seats: int


def parse_invite_accounts(raw, *, default_queue, default_seats, default_user_id=None):
    """The invite-account roster, from INVITE_ACCOUNTS.

    Format is `usr_id|queue|seats`, entries separated by commas, seats
    optional:

        INVITE_ACCOUNTS=usr_aaa|vrcverify_group_invites|200,usr_bbb|vrcverify_group_invites_2

    Unset falls back to the single account the pre-switchboard settings
    describe, so an existing deployment keeps working untouched and only grows
    this variable when it grows a second account.

    A malformed entry is logged and skipped rather than raised. Refusing to
    start over one typo takes down invites for every guild including the ones
    whose accounts parsed fine; dropping the entry costs the guilds on that one
    account, which is strictly less. Both are loud.
    """
    accounts = []
    seen_ids = set()
    seen_queues = set()

    def add(user_id, queue, seats, where):
        if not user_id or not user_id.startswith("usr_"):
            logger.error(
                "INVITE_ACCOUNTS %s: %r is not a VRChat user id; skipping.",
                where,
                user_id,
            )
            return
        if not queue:
            logger.error("INVITE_ACCOUNTS %s: no queue name; skipping.", where)
            return
        if user_id in seen_ids:
            logger.error(
                "INVITE_ACCOUNTS %s: %s appears twice; keeping the first.",
                where,
                user_id,
            )
            return
        if queue in seen_queues:
            # Two accounts on one queue is the round-robin bug this design
            # exists to avoid, arriving by configuration instead of by code.
            logger.error(
                "INVITE_ACCOUNTS %s: queue %r is already used by another "
                "account; skipping. Two accounts sharing a queue would have "
                "each other's jobs.",
                where,
                queue,
            )
            return
        seen_ids.add(user_id)
        seen_queues.add(queue)
        accounts.append(InviteAccount(user_id=user_id, queue=queue, seats=seats))

    text = (raw or "").strip()
    if not text:
        if default_user_id:
            add(default_user_id, default_queue, default_seats, "(from INVITE_VRCHAT_USER_ID)")
        return tuple(accounts)

    for index, entry in enumerate(text.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split("|")]
        where = "entry %s" % index
        if len(parts) not in (2, 3):
            logger.error(
                "INVITE_ACCOUNTS %s: expected usr_id|queue or usr_id|queue|seats, "
                "got %r; skipping.",
                where,
                entry,
            )
            continue
        seats = default_seats
        if len(parts) == 3:
            try:
                seats = int(parts[2])
            except ValueError:
                logger.error(
                    "INVITE_ACCOUNTS %s: %r is not a seat count; using %s.",
                    where,
                    parts[2],
                    default_seats,
                )
            else:
                if seats < 1:
                    logger.error(
                        "INVITE_ACCOUNTS %s: seat count %s is not positive; "
                        "using %s.",
                        where,
                        seats,
                        default_seats,
                    )
                    seats = default_seats
        add(parts[0], parts[1], seats, where)
    return tuple(accounts)


INVITE_ACCOUNTS = parse_invite_accounts(
    os.getenv("INVITE_ACCOUNTS"),
    default_queue=RABBITMQ_GROUP_INVITE_QUEUE,
    default_seats=INVITE_ACCOUNT_SEAT_CAP,
    default_user_id=INVITE_VRCHAT_USER_ID,
)
INVITE_ACCOUNTS_BY_ID = {a.user_id: a for a in INVITE_ACCOUNTS}

# How long a guild may be without a subscription before its seat goes back in
# the pool. The bot leaves the VRChat group; everything the admin configured
# survives, including verified_at -- so coming back is "invite the bot again
# and press check", never a full re-setup with the claim code.
#
# 90 days rather than 60. The two ways of being wrong are not symmetrical:
# holding a seat too long costs one seat, and reclaiming too early takes a
# returning customer's working setup away from them.
SEAT_LAPSE_GRACE_SECONDS = _int_env("SEAT_LAPSE_GRACE_SECONDS", 90 * 24 * 3600)

# A seat reserved for an admin who never finished setting up. Without this the
# feature leaks capacity to everyone who tried it once and gave up.
SEAT_RESERVATION_GRACE_SECONDS = _int_env(
    "SEAT_RESERVATION_GRACE_SECONDS", 30 * 24 * 3600
)

# Warn the operator while there is still time to provision another account.
SEAT_CAPACITY_WARN_FREE = _int_env("SEAT_CAPACITY_WARN_FREE", 10, minimum=0)

# The seat sweep. Everything else time-based in this feature expires ON READ,
# deliberately -- "a state that expires on its own needs no scheduler to be
# correct, and there is nothing to fail at three in the morning". That does not
# work here, and the reason is worth stating: nothing reads a lapsed guild's
# row, which is precisely what makes it lapsed. Leaving a VRChat group is an
# action nobody will otherwise trigger, so this one genuinely needs a sweep.
#
# Six hours, against a ninety-day grace period. The interval is not what makes
# the reclaim timely -- the grace period is -- so this is set by how fresh the
# capacity warning should be, not by how fast a seat comes back.
SEAT_SWEEP_INTERVAL = _int_env("SEAT_SWEEP_INTERVAL", 6 * 3600)
# Capped and spaced for the same reason panel nudges are: a backlog, a clock
# jump or a long outage must trickle out rather than becoming a burst of VRChat
# writes from one account.
SEAT_SWEEP_MAX_PER_PASS = _int_env("SEAT_SWEEP_MAX_PER_PASS", 25)
SEAT_SWEEP_SPACING = _float_env("SEAT_SWEEP_SPACING", 2.0)

# -------------------------------------------------------------------
# SQLAlchemy setup
# -------------------------------------------------------------------
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
Base = declarative_base()


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


# -------------------------------------------------------------------
# Database Models
# -------------------------------------------------------------------
class Server(Base):
    __tablename__ = "servers"
    id = Column(Integer, primary_key=True)
    server_id = Column(String, unique=True, nullable=False)
    owner_id = Column(String, nullable=False)
    role_id = Column(String, nullable=True)
    # Optional role to remove upon successful verification
    unverified_role_id = Column(String, nullable=True)
    subscription_status = Column(Boolean, default=False)
    subscription_start_date = Column(DateTime, nullable=True)
    email = Column(String, nullable=True)
    last_renewal_date = Column(DateTime, nullable=True)
    instructions_channel_id = Column(String, nullable=True)
    instructions_message_id = Column(String, nullable=True)
    auto_nickname_change = Column(Boolean, default=False)
    instructions_locale = Column(String, default="en-US", nullable=False)
    # New setting: auto verify new members on join (default ON)
    auto_verify_new_members = Column(Boolean, default=True, nullable=False)
    # Custom message shown after user clicks Verify (optional)
    custom_verification_requested_message = Column(String, nullable=True)
    # Completed 18+ verifications in this guild (for the one-time milestone DM)
    verification_count = Column(Integer, default=0, nullable=False)
    milestone_dm_sent = Column(Boolean, default=False, nullable=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(30), unique=True, nullable=False)
    verification_status = Column(Boolean, default=False)
    vrc_user_id = Column(String(50), nullable=True)
    last_verification_attempt = Column(DateTime(timezone=True))


class PendingVerification(Base):
    __tablename__ = "pending_verifications"
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(30), nullable=False)
    guild_id = Column(String(30), nullable=False)
    vrc_user_id = Column(String(50), nullable=False)
    verification_code = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)


class InstructionPanelView(Base):
    """Which button custom_id set each guild's posted panel is carrying.

    A separate table rather than a column on `servers` on purpose: create_all()
    below creates missing *tables* automatically but never adds columns to an
    existing one, and a column present in the model but absent in the database
    breaks every Server query — not just the ones reading it. This keeps the
    rollout to zero manual DDL.
    """

    __tablename__ = "instruction_panel_views"
    server_id = Column(String, primary_key=True)
    view_version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GuildOnboarding(Base):
    """Panel-nudge state for guilds configured from this release onward.

    A separate table for the same reason as InstructionPanelView above:
    create_all() adds missing tables but never columns.

    It also gives the "new servers only" rule for free. Guilds set up before
    this shipped have no row here, so the nudge sweep cannot reach them — the
    scope limit is structural rather than a date comparison someone could get
    wrong later.
    """

    __tablename__ = "guild_onboarding"
    server_id = Column(String, primary_key=True)
    setup_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    panel_nudge_dm_sent = Column(Boolean, nullable=False, default=False)


class PremiumCutoverNotice(Base):
    """Which guilds have already had the one-time premium announcement DM.

    A separate table for the same reason as the two above: create_all() adds
    missing tables but never columns. Presence of a row means "already told" —
    there is no boolean to get out of sync, and mark-before-send is a plain
    insert.

    Note this is only the DM ledger. Whether a server is *grandfathered* is
    PremiumGrandfatherLine's job.
    """

    __tablename__ = "premium_cutover_notice"
    server_id = Column(String, primary_key=True)
    sent_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PremiumGrandfatherLine(Base):
    """Where the grandfather line was drawn, captured once and never moved.

    Written on the first startup that finds the tier switched on, holding
    MAX(servers.id) as of that moment. Every server already installed is
    therefore grandfathered, and only servers added afterwards can ever be
    asked to pay for GRANDFATHERED_FEATURES.

    That ordering is the entire point: it makes "a server loses automation it
    already had" impossible by construction rather than something we detect
    afterwards and apologise for (issue #59). It also means the cutover DM can
    go out after the switch without harm, since it is now purely informational.

    Captured rather than configured because a hand-set line is a number someone
    has to remember to update at exactly the right moment, and the cost of
    forgetting is silently charging existing servers for what they already had.

    Stored in the database, not a file or an env var, so a restore carries the
    line with the servers it describes — recomputing it after a restore would
    re-draw it wherever the restore happened to land, retroactively
    grandfathering everyone who signed up since.
    """

    __tablename__ = "premium_grandfather_line"
    # Single row, always id=1. A table rather than a column so create_all()
    # brings it into being on its own, like the three above.
    id = Column(Integer, primary_key=True)
    max_server_id = Column(Integer, nullable=False)
    captured_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class InstructionPanelBranding(Base):
    """A premium server's own colour and thumbnail for its instructions panel.

    A separate table for the same reason as the others: create_all() adds
    missing tables but never columns.

    Both settings default to "off", so the presence of a row does not by itself
    change how a panel looks. That matters because subscribing should not
    silently restyle a panel the admin never asked to restyle.

    Only the styling is customisable. The instruction copy itself is not, and
    deliberately: it is the part that actually gets people through verification
    correctly, and letting servers rewrite it means support requests about
    instructions nobody here wrote.

    The row survives a lapsed subscription, like VerificationLogChannel above:
    styling reverts to the default, but the admin's choices are theirs and come
    back untouched if they resubscribe.
    """

    __tablename__ = "instruction_panel_branding"
    server_id = Column(String, primary_key=True)
    # Discord's native integer form. NULL means "use the default blue" rather
    # than a sentinel colour, so "unset" and "deliberately dark" stay distinct.
    embed_color = Column(Integer, nullable=True)
    show_icon = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class VerificationLogChannel(Base):
    """Where a guild wants its verification activity posted.

    A separate table for the same reason as the three above: create_all() adds
    missing tables but never columns.

    The row survives a lapsed subscription on purpose. Logging stops, but the
    admin's choice of channel is theirs — clearing it would mean re-configuring
    after every billing hiccup, and it matches how the settings view leaves a
    locked setting's stored value alone.
    """

    __tablename__ = "verification_log_channel"
    server_id = Column(String, primary_key=True)
    channel_id = Column(String, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# How far a guild's VRChat group setup has got, as stored in
# group_invite_config.verify_state.
#
# Everything except the first two is a verdict the invite worker reached, and
# these strings must stay identical to the STATE_* constants in
# vrc_group_inviter.py. They are duplicated rather than imported because the
# worker is a separate process in a separate image that has neither discord.py
# nor a database driver -- importing it here would drag both into the bot.
# tests/test_group_invite_config.py imports both modules and asserts the two
# vocabularies agree, which is the part that would otherwise drift silently.
# The bot's own three. The worker only ever reports what it found, so it has
# no way to say "nobody has asked yet", "we are waiting" or "the answer never
# came" -- those are this side's business.
GROUP_SETUP_UNVERIFIED = "unverified"  # never checked
GROUP_SETUP_CHECKING = "checking"  # a verify job is in flight
GROUP_SETUP_TIMED_OUT = "timed_out"  # asked, and nothing came back in time
# The job never left this process. Distinct from the worker's
# vrchat_unavailable, which means the worker ran and VRChat did not answer:
# one is a broken queue here, the other is a broken API there, and a single
# code covering both makes the state useless for working out which.
GROUP_SETUP_WORKER_UNREACHABLE = "worker_unreachable"
# The bot left the group to free its seat after a long lapse (phase 6). Its own
# state rather than reusing "not_invited": that copy is accurate -- the bot
# really is not in the group -- but it reads as though the admin never did the
# setup, when in fact they did it and we undid it. The distinction is worth a
# string because the actions differ too: this one only needs the bot invited
# again, and specifically does NOT need a fresh claim code.
GROUP_SETUP_SEAT_RELEASED = "seat_released"

# The worker's verdicts.
GROUP_SETUP_READY = "ready"  # joined, and holds the invite permission
GROUP_SETUP_JOIN_REQUESTED = "join_requested"
GROUP_SETUP_NOT_INVITED = "not_invited"
GROUP_SETUP_NO_INVITE_PERMISSION = "no_invite_permission"
GROUP_SETUP_GROUP_NOT_FOUND = "group_not_found"
GROUP_SETUP_CODE_MISSING = "code_missing"
GROUP_SETUP_BANNED = "banned"
GROUP_SETUP_BAD_JOB = "bad_job"
GROUP_SETUP_VRCHAT_UNAVAILABLE = "vrchat_unavailable"

GROUP_SETUP_STATES = frozenset(
    {
        GROUP_SETUP_UNVERIFIED,
        GROUP_SETUP_CHECKING,
        GROUP_SETUP_TIMED_OUT,
        GROUP_SETUP_WORKER_UNREACHABLE,
        GROUP_SETUP_SEAT_RELEASED,
        GROUP_SETUP_READY,
        GROUP_SETUP_JOIN_REQUESTED,
        GROUP_SETUP_NOT_INVITED,
        GROUP_SETUP_NO_INVITE_PERMISSION,
        GROUP_SETUP_GROUP_NOT_FOUND,
        GROUP_SETUP_CODE_MISSING,
        GROUP_SETUP_BANNED,
        GROUP_SETUP_BAD_JOB,
        GROUP_SETUP_VRCHAT_UNAVAILABLE,
    }
)

# Verdicts that mean the setup is finished and working. Everything else is
# either in progress or something an admin has to act on.
GROUP_SETUP_SUCCESS_STATES = frozenset({GROUP_SETUP_READY})


# group_invite_request.state -- what happened when ONE member asked for ONE
# invite (issue #49, phase 5).
#
# A separate vocabulary from the setup states above because it answers a
# different question for a different reader: a setup state is a sentence for
# the server's admin on the dashboard, an invite state is a sentence for the
# member in a DM. Sharing one set would have members reading
# "no_invite_permission" as though it were something they could fix.
#
# Everything except the bot's own three below is a verdict the invite worker
# reached, and must stay identical to vrc_group_inviter.INVITE_STATES. Same
# duplication and same reason as the setup states: the worker has neither
# discord.py nor a database driver, so the constants are copied and a test
# asserts the two agree.
GROUP_INVITE_SENT = "sent"
GROUP_INVITE_ALREADY_MEMBER = "already_member"
GROUP_INVITE_ALREADY_INVITED = "already_invited"
GROUP_INVITE_BLOCKED = "blocked"
GROUP_INVITE_BANNED = "banned"
GROUP_INVITE_GROUP_NOT_FOUND = "group_not_found"
# The member's stored VRChat account does not resolve -- they relinked, or the
# id was never real. Theirs to fix by verifying again, and kept apart from the
# group's own failures because the advice is opposite: this one must never tell
# them their server's setup is broken and send them to an admin who can do
# nothing about it.
GROUP_INVITE_USER_NOT_FOUND = "user_not_found"
GROUP_INVITE_NO_PERMISSION = "no_invite_permission"
GROUP_INVITE_VRCHAT_UNAVAILABLE = "vrchat_unavailable"
GROUP_INVITE_BAD_JOB = "bad_job"

# The bot's own three. The worker only reports what it found, so it cannot say
# "we are waiting", "the answer never came", or "the job never left this
# process" -- those are this side's business, exactly as with setup.
GROUP_INVITE_PENDING = "pending"
GROUP_INVITE_TIMED_OUT = "timed_out"
GROUP_INVITE_WORKER_UNREACHABLE = "worker_unreachable"

# What the WORKER is allowed to tell us. The bot's own three are not in here,
# and that is the point: a result carrying "pending" would push a settled row
# back into flight and rewrite the member's DM to "asking VRChat..." for ever.
# Nothing should ever send one -- which is exactly why the wire must not
# accept one.
GROUP_INVITE_WORKER_STATES = frozenset(
    {
        GROUP_INVITE_SENT,
        GROUP_INVITE_ALREADY_MEMBER,
        GROUP_INVITE_ALREADY_INVITED,
        GROUP_INVITE_BLOCKED,
        GROUP_INVITE_BANNED,
        GROUP_INVITE_GROUP_NOT_FOUND,
        GROUP_INVITE_USER_NOT_FOUND,
        GROUP_INVITE_NO_PERMISSION,
        GROUP_INVITE_VRCHAT_UNAVAILABLE,
        GROUP_INVITE_BAD_JOB,
    }
)

GROUP_INVITE_STATES = frozenset(
    {
        GROUP_INVITE_SENT,
        GROUP_INVITE_ALREADY_MEMBER,
        GROUP_INVITE_ALREADY_INVITED,
        GROUP_INVITE_BLOCKED,
        GROUP_INVITE_BANNED,
        GROUP_INVITE_GROUP_NOT_FOUND,
        GROUP_INVITE_USER_NOT_FOUND,
        GROUP_INVITE_NO_PERMISSION,
        GROUP_INVITE_VRCHAT_UNAVAILABLE,
        GROUP_INVITE_BAD_JOB,
        GROUP_INVITE_PENDING,
        GROUP_INVITE_TIMED_OUT,
        GROUP_INVITE_WORKER_UNREACHABLE,
    }
)

# vrc_group_inviter.LEAVE_STATES, mirrored. Same duplication and same reason
# as the two vocabularies above: the worker has neither discord.py nor a
# database driver, so importing it here would drag both in.
JOB_LEAVE_GROUP = "leave_group"

# Why a leave was asked for. Echoed back by the worker, because the two need
# opposite handling and the result alone cannot tell them apart.
#
#   reclaim   -- the guild lapsed; on success the seat goes back in the pool.
#   displaced -- the admin pointed the guild at a DIFFERENT group. The old
#                group must be left, but the seat stays exactly where it is:
#                they are setting up a new group on the same account.
#
# Without this distinction, an admin changing their group had their seat freed
# and their brand-new setup marked as reclaimed, by the success of a leave that
# was only ever about the old group.
RELEASE_REASON_RECLAIM = "reclaim"
RELEASE_REASON_DISPLACED = "displaced"

SEAT_LEAVE_DONE = "left"
SEAT_LEAVE_FAILED = "leave_failed"
SEAT_LEAVE_STATES = frozenset({SEAT_LEAVE_DONE, SEAT_LEAVE_FAILED})

# An answer, as opposed to a failure to get one. Reaching one of these settles
# the request, and settles the BUTTON that produced it for good: the press
# refuses every state in here, which is what stops one member's pile of old
# DMs becoming a pile of invites.
#
# Everything NOT in here is a transient failure -- VRChat down, the server's
# permissions broken, the job lost -- and those deliberately leave the button
# live, because none of them is the member's answer to the question.
GROUP_INVITE_SETTLED_STATES = frozenset(
    {
        GROUP_INVITE_SENT,
        GROUP_INVITE_ALREADY_MEMBER,
        GROUP_INVITE_ALREADY_INVITED,
        GROUP_INVITE_BLOCKED,
        GROUP_INVITE_BANNED,
    }
)

# Outcomes this bot will never ask about again. DELIBERATELY EMPTY.
#
# It used to hold BANNED and BLOCKED, on the argument that a moderator's ban
# and a member's own opt-out are both answers we have no business re-opening.
# That argument was wrong, and the way it was wrong is worth keeping:
#
#   * Neither state is OUR verdict. Both are a cache of what VRChat told us
#     once. A moderator who lifts a ban, and a member who switches group
#     invites back on, have both changed the answer -- and a permanent cache
#     is a promise never to notice. A one-day ban cost the member the feature
#     for ever.
#   * The member-facing copy has always described a re-checkable system.
#     "Only a group moderator can change that" and "Group invites may be
#     switched off in your VRChat settings" both name a fix that, under the old
#     rule, could not possibly help. The strings were right and the state
#     machine was wrong.
#   * Re-checking costs almost nothing and can invite nobody. The worker asks
#     get_group_member first, and a ban that check cannot see is refused by
#     create_group_invite with a 409 (issue #209). VRChat enforces this, not
#     us, so the worst case of asking again is one refused API call -- never an
#     invite reaching somebody a moderator threw out.
#
# The compliance argument that motivated permanence survives intact, because it
# was always about SENDING, not asking. What VRChat's guidelines call abuse is
# pushing an unsolicited invite at somebody who has been thrown out. Nothing
# here sends one: an invite exists only after the member presses a button in
# their own DM, and confirm_override_block is still always False.
#
# Kept as an empty set rather than deleted so the relationship below still
# reads as a relationship, and so a state that genuinely IS permanent has a
# home. Adding one is a real decision: it must be an answer that no action by
# the member or by a moderator could ever change.
GROUP_INVITE_FINAL_STATES: frozenset = frozenset()
GROUP_INVITE_REOFFERABLE_STATES = GROUP_INVITE_SETTLED_STATES - GROUP_INVITE_FINAL_STATES


class GroupInviteConfig(Base):
    """A guild's VRChat group, and how far its setup has got (issue #49).

    A separate table for the same reason as the four above: create_all() adds
    missing tables but never columns. That constraint bites harder here than
    anywhere else in this file, because several columns below exist for phases
    not yet written -- the dashboard's verify round trip, and the invites
    themselves. Declaring them now costs nothing; adding one later costs a
    hand-run ALTER against a live database, or a second table to hold the one
    thing that was forgotten.

    `group_id` is UNIQUE, and that constraint IS first-claim-wins. A group
    belongs to the first guild that claims it and a second guild is refused,
    because otherwise guild B could type the id of a group guild A had already
    set up and start inviting its own members into a stranger's group. NULLs
    do not collide in Postgres or SQLite, so an unconfigured guild claims
    nothing, and clearing the field releases the claim for someone else.

    Everything describing the VRChat side -- the name, the permissions, the
    verify verdict -- is a cache of what the invite worker last saw, never
    something an admin types. All of it is reset when `group_id` changes: a
    verdict about the old group says nothing about the new one, and a stale
    "ready" would let invites go out against a group nobody has checked.

    The row survives a lapsed subscription, like VerificationLogChannel above.
    Invites stop; the admin's configuration is theirs and comes back untouched.
    """

    __tablename__ = "group_invite_config"
    server_id = Column(String, primary_key=True)
    # The `grp_...` id, lower-cased by parse_vrchat_group_id. Normalising there
    # rather than here is deliberate: unnormalised, two guilds could claim one
    # group in different cases and the unique constraint would not notice.
    group_id = Column(String(64), nullable=True, unique=True)
    # What the group calls itself, as the worker last saw it. Display only --
    # nothing keys off it, and it is allowed to go stale.
    #
    # Unbounded, unlike the columns around it. The rule for this table is that
    # a value THIS code produces gets a length (the id is regex-constrained to
    # 40 characters, the claim code to 11, the state to a closed vocabulary),
    # and a value VRChat produces does not. A cap on somebody else's string is
    # a length limit we would find out about when Postgres refuses a write --
    # and the column could not be widened afterwards without the hand-run ALTER
    # this whole table exists to avoid.
    group_name = Column(String, nullable=True)
    # The group's icon, as VRChat serves it -- `icon_url`, never `banner_url`.
    # They are separate fields and the banner is a wide header image, correct
    # in VRChat's own UI and wrong beside a one-line status.
    #
    # ADDED AFTER THIS TABLE WAS ALREADY DEPLOYED, which create_all() cannot
    # do -- it adds missing tables and never columns. Any database that
    # already had group_invite_config needs, once:
    #
    #   ALTER TABLE group_invite_config ADD COLUMN group_icon_url VARCHAR;
    #
    # A fresh database gets it from create_all() like everything else, so this
    # is a one-time step for existing deployments rather than lasting drift.
    # The startup check below names the statement in the log if it is missing,
    # because the symptom otherwise is every query against this table failing
    # with a driver error that says nothing about a migration.
    group_icon_url = Column(String, nullable=True)
    # The admin's switch, independent of whether a group is configured or
    # verified. Off by default, so storing a group id never starts anything.
    #
    # NEVER the gate on its own. The settings field is write_locked, which
    # means a lapsed subscription is refused the save and leaves this exactly
    # as it was -- True, on a server that is no longer paying. Anything acting
    # on this flag has to resolve the plan as well.
    enabled = Column(Boolean, nullable=False, default=False)
    # Which VRChat account serves this guild. There is one today, but a VRChat
    # account can only be in 100 groups (200 with VRC+), so there will be more,
    # and an admin must never be the one working out which one to invite.
    invite_account_id = Column(String(64), nullable=True)
    # Proof the claimer controls the group: shown on the dashboard, pasted into
    # the group description, read back through get_group() before the bot
    # joins. The group-level analogue of the bio code members already use.
    claim_code = Column(String(20), nullable=True)
    claim_code_issued_at = Column(DateTime(timezone=True), nullable=True)
    # One of GROUP_SETUP_STATES: what the last check concluded.
    verify_state = Column(
        String(32), nullable=False, default=GROUP_SETUP_UNVERIFIED
    )
    # The worker's own sentence about why, for an admin to read. Not a code --
    # verify_state is the code, and this is the part that names the group.
    #
    # Unbounded for the reason group_name gives, and more sharply: some of
    # these sentences quote a VRChat API error, so a cap here would mean an
    # unusually verbose failure fails the write that was only trying to record
    # a failure.
    verify_error = Column(String, nullable=True)
    verify_requested_at = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    # The job id the last check went out with. The round trip is asynchronous
    # over RabbitMQ, so a slow answer to an old question can land after a fast
    # answer to a new one; without this to match on, the stale answer wins and
    # the page shows a verdict about a group the admin has already replaced.
    verify_job_id = Column(String(64), nullable=True)
    # What the account can actually do in the group, from get_group's
    # my_member.permissions. `can_invite` is the feature itself -- confirmed
    # empirically to need its own `group-invites-manage` checkbox, which admin
    # does not imply.
    #
    # `can_see_members` is optional and nothing gates on it. It is recorded so
    # the dashboard can suggest granting it: without it the bot cannot tell
    # that a member already has an invite waiting, and may send a second. The
    # invite path itself never reads this -- it asks VRChat and copes with
    # whatever comes back, because what that endpoint requires has never
    # actually been measured.
    can_invite = Column(Boolean, nullable=False, default=False)
    can_see_members = Column(Boolean, nullable=False, default=False)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class GroupInviteRequest(Base):
    """One member's standing with one server's VRChat group (issue #49).

    A separate table for the same reason as the five above: create_all() adds
    missing tables but never columns.

    One row per (server, member), rewritten in place rather than appended to.
    This is not an audit log -- it is the answer to two questions asked on
    every verification: may we offer this member an invite, and what happened
    the last time they asked. A history of attempts would answer neither
    better, and it would accumulate a Discord-to-VRChat mapping row per
    attempt for no purpose. The verification log deliberately refuses to
    publish that link at all (see the comment on queue_verification_log), so
    this table holds the minimum that makes the feature work: who asked, about
    which group, and how it went.

    `group_id` is stored beside the outcome rather than read from the config,
    because the two can disagree. An admin who changes the server's group has
    invalidated everything learned about the old one -- a member who is
    "already_member" of the group this server used to use tells us nothing
    about the one it uses now, and without this column that stale row would
    silently suppress the offer for ever. Every read compares it.

    The row survives a lapsed subscription, like the two config tables above.
    Invites stop; nobody is re-offered something they already declined or
    already has when the subscription comes back.
    """

    __tablename__ = "group_invite_request"
    # Composite natural key, as VerificationDaily does it. There is exactly one
    # standing per member per server, and making that the key is what stops a
    # double press writing two rows that disagree.
    server_id = Column(String, primary_key=True)
    discord_id = Column(String(30), primary_key=True)
    # Which group this outcome is about. See the docstring -- this is the
    # column that keeps a verdict about the old group from suppressing the
    # offer for the new one.
    group_id = Column(String(64), nullable=True)
    # The member's VRChat account, as it was when they asked. Kept so the
    # worker's answer can be matched to the account it was actually about: a
    # member who relinks to a different VRChat account has a standing that
    # describes somebody else's membership.
    vrc_user_id = Column(String(50), nullable=True)
    # One of GROUP_INVITE_STATES.
    state = Column(String(32), nullable=False, default=GROUP_INVITE_PENDING)
    # The job id this request went out with. The round trip is asynchronous
    # over RabbitMQ, so a slow answer to an old question can land after a fast
    # answer to a new one; without this to match on, the stale answer wins.
    # Same hazard, same fix, as group_invite_config.verify_job_id.
    job_id = Column(String(64), nullable=True)
    # When the member pressed the button. Both the cooldown and the "this
    # request is never coming back" timeout are measured from here, on read --
    # no sweep, for the reason GROUP_VERIFY_TIMEOUT_SECONDS gives.
    requested_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    # The DM the button was pressed in, so the outcome can be edited into that
    # same message instead of arriving as a second DM. Stored rather than held
    # in memory because the worker's answer can outlive a bot restart, and a
    # member left looking at "asking VRChat..." for ever is the failure this
    # avoids. Strings, like every other Discord id in this file.
    channel_id = Column(String(30), nullable=True)
    message_id = Column(String(30), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class GroupSeatLease(Base):
    """Which invite account holds a guild's seat, and since when (issue #49).

    A separate table for the same reason as the six above: create_all() adds
    missing tables but never columns.

    A VRChat account can be in 100 groups, 200 with VRC+. That cap is the
    scarcest thing this feature has, and it is spent by servers rather than by
    members -- so it needs accounting that a lapsed server cannot quietly hold
    forever. This table is that accounting.

    It exists as its own table rather than as columns on group_invite_config
    because two of its three dates answer questions that table cannot:

      * `reserved_at` is set when an account is ASSIGNED, which is the moment
        the dashboard first names it to an admin -- before they have invited
        anything. Counting only groups the bot had actually joined would let
        fifty admins setting up at once all be sent to the same nearly-full
        account and push it past its cap.

      * `last_premium_at` is the one piece of state nothing else records. A
        Stripe subscriber's lapse date can be read from
        stripe_subscription.current_period_end, but a guild on a Discord
        entitlement simply stops appearing -- premium_entitlement_seen holds
        `first_seen` and nothing else. Without this column there is no date to
        measure a grace period from.

    Releasing a seat leaves the VRChat group and clears the lease. It
    deliberately does NOT clear the guild's configuration: group_id keeps the
    UNIQUE claim so nobody can squat on a lapsed server's group while it is
    away, and `verified_at` survives so begin_group_verification still computes
    `requireCode` as False. A returning admin re-invites the bot and presses
    check; they never paste the claim code back into their group description.
    """

    __tablename__ = "group_seat_lease"
    server_id = Column(String, primary_key=True)
    # Which account is holding the seat. Indexed because every capacity
    # decision counts rows by this column, and that count happens on the path
    # an admin waits on.
    invite_account_id = Column(String(64), nullable=True, index=True)
    # When the account was assigned -- not when the bot joined. See above.
    reserved_at = Column(DateTime(timezone=True), nullable=True)
    # The last moment this guild was seen paying. The grace period counts from
    # here, so a guild that has never been seen paying since this table shipped
    # is treated as lapsing from the sweep that first looks at it, not from the
    # epoch. NULL is therefore "not yet observed", never "lapsed long ago".
    last_premium_at = Column(DateTime(timezone=True), nullable=True)
    # The reclaim job this lease is waiting on. The round trip is asynchronous,
    # so a slow answer can arrive after the guild has resubscribed and set
    # itself up again -- and freeing the seat then would break a working setup
    # with the success of a question nobody is asking any more. Same hazard and
    # same fix as group_invite_config.verify_job_id.
    release_job_id = Column(String(64), nullable=True)
    # Set when the bot has actually left the group. A released row is kept
    # rather than deleted: it is the record that says this guild HAD a seat and
    # what happened to it, which is the difference between "never set up" and
    # "set up, then reclaimed" on a support ticket.
    released_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class DashboardAudit(Base):
    """Who changed which setting, from the website, and to what.

    The bot's own slash commands are already accountable: Discord records who
    ran one, and an admin has to be in the server holding Administrator to do
    it. The dashboard is different in kind — it is reachable from the public
    internet, and the only thing standing between a stolen session and a
    settings change is a cookie. So the first write path gets a durable record
    in the same change that introduces it.

    Deliberately append-only in use: nothing in this codebase updates or
    deletes a row here. The value of an audit trail is precisely that it
    disagrees with someone's account of events, which it cannot do if the code
    that made the change can also rewrite the record of it.

    Old and new values are stored as text, and are the *settings* only. No
    member data, no VRChat identity, nothing about who is verified — this table
    is about administrators changing configuration, and it should stay boring
    enough that its own disclosure would not matter much.

    **What this trail does not cover.** The actor comes from verified token
    claims, which makes it trustworthy exactly as far as the signing key is. The
    dashboard host holds that key, and the design elsewhere says to assume that
    host is eventually compromised — so an attacker at VPS level can mint a
    token naming any actor, including a guild's owner, and every row they cause
    will name that person. This table detects a stolen *session*; against a
    compromised dashboard it does not merely fail to detect, it actively
    misattributes. Reading a row as proof a particular human did something is
    only sound while the key is sound.
    """

    __tablename__ = "dashboard_audit"
    id = Column(Integer, primary_key=True)
    server_id = Column(String, index=True, nullable=False)
    # The Discord user the request was scoped to, taken from the verified token
    # claims rather than from anything the dashboard put in a body.
    actor_id = Column(String, nullable=False)
    field = Column(String, nullable=False)
    old_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    changed_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class VerificationDaily(Base):
    """How many verifications a guild completed on each UTC day.

    The dashboard's Overview needs "how many this week", and
    `Server.verification_count` cannot answer it — that is a running total with
    no history behind it, so subtracting anything from it tells you nothing
    about when. This is the smallest table that can.

    **One row per guild per day, and nothing else.** No discord id, no VRChat
    id, no per-person timestamp. That is a deliberate ceiling rather than a
    first version: this product exists to tell a server that somebody is over
    18, and a durable record of *which* member verified *when* would be a more
    sensitive dataset than anything else the bot keeps, held for the sake of a
    number on a page. A count cannot be turned back into a person, so the
    honest way to build the feature is to never write the row that could.

    Because it is only a count, an admin reading the Overview learns the shape
    of their own server's activity and nothing about any individual in it —
    which is also what makes it safe to show without a permission model of its
    own beyond the Administrator check every dashboard read already passes.

    A day with no verifications has no row. Absent and zero therefore look the
    same *in the table*, and the reader is what tells them apart: see
    `read_dashboard_overview`, which uses the earliest row to decide whether a
    window is backed by data at all. Writing zero-rows nightly would remove
    that distinction and buy nothing.
    """

    __tablename__ = "verification_daily"
    server_id = Column(String, primary_key=True)
    # UTC. The bot runs in one place and the dashboard renders what it is told,
    # so there is exactly one clock in this feature and no timezone to argue
    # about.
    day = Column(Date, primary_key=True)
    count = Column(Integer, nullable=False, default=0)


class ServerMembershipDaily(Base):
    """Daily snapshot of registered servers and current Discord presence.

    ``Server`` rows survive removal so configuration can be restored when a
    guild re-invites the bot. ``active_count`` counts every guild currently
    exposed by Discord, including installed guilds that have not configured the
    bot. ``inaccessible_count`` counts retained registrations absent from that
    current guild set.
    """

    __tablename__ = "server_membership_daily"
    day = Column(Date, primary_key=True)
    registered_count = Column(Integer, nullable=False, default=0)
    active_count = Column(Integer, nullable=False, default=0)
    inaccessible_count = Column(Integer, nullable=False, default=0)


class StripeSubscription(Base):
    """One Stripe subscription, mirrored (issue #88). A guild may have several.

    Stripe is the source of truth. This is a local copy so the premium gate
    stays a database read rather than an API call on every verification — and
    so the bot never needs a Stripe credential to answer "is this server paid
    up", which is the property that keeps every Stripe secret on the VPS.

    A separate table for the same reason as the five above: create_all() adds
    missing tables but never columns. The dead `subscription_status` / `email`
    / `last_renewal_date` columns on `servers` stay dead — they could not hold
    a customer id, a subscription id or a price id even if they were wanted.

    **Keyed by the Stripe subscription, not by the guild**, which is the one
    place this departs from the issue's schema. A guild really can hold two
    live subscriptions at once — the issue's own Double billing section says to
    expect it and warn about it — and a table that cannot represent that does
    not merely lose a warning, it loses money in the customer's disfavour: with
    one row per guild, an ordinary renewal of the older subscription overwrites
    the newer one, and the older one's eventual cancellation then switches off
    a server that is still being billed for the other. That was found by
    probing this code rather than reasoning about it, and no guard on a
    one-row-per-guild table closes it, because the state it needs to hold has
    two subscriptions in it.

    So: the gate asks whether *any* row for the guild is paid, and the
    double-billing warning is a row count rather than a special case.

    Rows survive a lapsed subscription, like VerificationLogChannel and
    InstructionPanelBranding do. Resubscribing is then a status change rather
    than a re-onboarding, and the website can say "your subscription ended on
    the 3rd" instead of pretending the server was never a customer.
    """

    __tablename__ = "stripe_subscription"
    stripe_subscription_id = Column(String, primary_key=True)
    # Indexed rather than unique: every read starts from a guild, and a guild
    # may legitimately have more than one row.
    server_id = Column(String, index=True, nullable=False)
    stripe_customer_id = Column(String, nullable=False)
    # Which of the three plans. Stored rather than derived, because the price
    # id is the only thing the webhook reports and the labels live in the
    # dashboard's config.
    price_id = Column(String, nullable=False)
    # Stripe's own status string, verbatim and unmapped. A boolean here would
    # throw away the difference between "cancelled" and "we could not charge
    # the card yesterday", which is exactly the difference support questions
    # are about.
    status = Column(String, nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    # Ordering guard, per subscription — which is the scope Stripe's ordering
    # actually concerns. It does not promise events arrive in order, and a
    # delayed `subscription.updated` overwriting a newer `subscription.deleted`
    # would silently restore premium to a server that cancelled.
    last_event_created = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StripeEvent(Base):
    """Every webhook event id this bot has already acted on.

    Insert first and let the primary key settle it: an IntegrityError means
    the event is a replay, which is the same pattern capture_grandfather_line()
    uses to settle its race. Stripe retries a webhook for up to three days, so
    duplicates are expected traffic rather than an anomaly.

    Pruned by the same function that inserts, deliberately rather than by a
    scheduled job — the A-24 pattern, where the only thing that can add a row
    is also the thing that removes them, so nothing here needs a scheduler to
    stay correct. Anything older than STRIPE_EVENT_RETENTION_DAYS is far
    outside Stripe's three-day retry window and can no longer be replayed at
    us, so keeping it buys nothing and grows forever.
    """

    __tablename__ = "stripe_event"
    event_id = Column(String, primary_key=True)
    # Indexed because the sweep filters on it on every insert.
    received_at = Column(
        DateTime(timezone=True),
        index=True,
        default=lambda: datetime.now(timezone.utc),
    )


class PremiumEntitlementSeen(Base):
    """Every guild observed holding a Discord entitlement, ever (issue #88).

    The free trial is offered to servers that have **never had a paid plan by
    either route**, and that question is harder than it sounds in exactly one
    direction. A live card subscription is a `stripe_subscription` row, and
    those rows deliberately survive the subscription ending — so the card half
    of "has this server ever paid" is already answered by a table that exists.

    The Discord half was answered by nothing at all. `on_entitlement_delete`
    invalidates a cache and returns; `premium_from_interaction` reads a payload
    and keeps no record. An entitlement that ended left this process with no
    memory of it having existed, so a server could subscribe through Discord,
    cancel, and come back to a free month — repeatably, and once per cancel.

    So this is the ledger, and three things about it are deliberate:

    * **Rows are never deleted.** A lapsed subscription is precisely the case
      this exists to remember; pruning would reopen the hole it closes.
    * **It is written from gateway events and a startup sweep, never from
      `premium_from_interaction`.** That function is I/O-free by design and a
      test pins it, because a write there would put a database round trip
      behind every slash command in the bot.
    * **`source` records how the guild was first seen paying**, for support
      rather than for the gate — the predicate only asks whether a row exists.
      A guild that later pays the other way keeps its original value, which is
      why this is not called `current_source`.
    """

    __tablename__ = "premium_entitlement_seen"
    server_id = Column(String, primary_key=True)
    source = Column(String, nullable=False, default="discord")
    first_seen = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


# Creates any missing tables. Note this does NOT add columns to tables that
# already exist — those still need a manual ALTER.
Base.metadata.create_all(engine)


def _ddl_type_for(table: str, column: str) -> str:
    """The column's real SQL type, so the statement in the log can be pasted.

    A hardcoded VARCHAR was right for the two string columns that had gone
    missing so far and would be quietly wrong for the first timestamp or
    boolean -- and an ALTER that runs and creates the wrong type is worse than
    one that does not run.
    """
    try:
        model_column = Base.metadata.tables[table].columns[column]
        return model_column.type.compile(dialect=engine.dialect)
    except Exception:
        # Never let formatting the advice swallow the warning it belongs to.
        return "/* see the model for this column's type */"


def _warn_about_missing_columns() -> None:
    """Say so, loudly and with the exact statement, at startup.

    A column in the model that the database lacks does not fail politely: it
    fails on the next SELECT against that table, with a driver error naming a
    column and nothing about a migration. Whoever reads that log is several
    steps from the answer.

    Derived from the models rather than from a hand-kept list. It used to be a
    list, on the reasoning that only tables whose columns arrived after the
    table did are worth checking -- which is true, and useless, because it
    depends on whoever adds a column also remembering to add it here. Nobody
    did for group_seat_lease.release_job_id, and the first anyone heard of it
    was a live ProgrammingError on a member pressing a button.

    Comparing the metadata to the database costs one inspection at startup and
    cannot fall out of date, because there is nothing to keep up to date.
    """
    expected = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
    }
    try:
        insp = inspect(engine)
        existing_tables = set(insp.get_table_names())
    except Exception:
        logger.warning("Could not inspect the database for missing columns.", exc_info=True)
        return

    for table, columns in expected.items():
        if table not in existing_tables:
            continue  # create_all will have built it complete
        try:
            present = {c["name"] for c in insp.get_columns(table)}
        except Exception:
            logger.warning("Could not inspect %s.", table, exc_info=True)
            continue
        for column in sorted(columns - present):
            logger.error(
                "Database column %s.%s is missing. Every query against %s will "
                "fail until it is added. Run:  ALTER TABLE %s ADD COLUMN %s %s;",
                table,
                column,
                table,
                table,
                column,
                _ddl_type_for(table, column),
            )


_warn_about_missing_columns()


# Helper: check if a column exists on the 'servers' table (no auto-migration)
def server_has_column(column_name: str) -> bool:
    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("servers")]
        return column_name in cols
    except Exception:
        logger.warning("⚠️ Could not inspect database for column presence.", exc_info=True)
        return False


# -------------------------------------------------------------------
# RabbitMQ Setup
# -------------------------------------------------------------------
credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)


def _rabbitmq_parameters() -> pika.ConnectionParameters:
    """Build connection parameters with heartbeats/timeouts so stale connections get detected."""
    heartbeat = int(os.getenv("RABBITMQ_HEARTBEAT", "60"))
    blocked_timeout = int(os.getenv("RABBITMQ_BLOCKED_TIMEOUT", "60"))
    connection_attempts = int(os.getenv("RABBITMQ_CONN_ATTEMPTS", "3"))
    retry_delay = float(os.getenv("RABBITMQ_RETRY_DELAY", "2"))
    socket_timeout = float(os.getenv("RABBITMQ_SOCKET_TIMEOUT", "10"))

    return pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=credentials,
        heartbeat=heartbeat,
        blocked_connection_timeout=blocked_timeout,
        connection_attempts=connection_attempts,
        retry_delay=retry_delay,
        socket_timeout=socket_timeout,
    )


def _rabbitmq_connect_with_retry(max_tries: int = 0) -> pika.BlockingConnection:
    """Connect to RabbitMQ with retries.

    max_tries=0 means retry forever (used by long-running consumers).
    """
    params = _rabbitmq_parameters()
    attempt = 0
    while True:
        attempt += 1
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError:
            if max_tries and attempt >= max_tries:
                raise
            delay = min(30.0, 2.0 * attempt)
            logger.warning("RabbitMQ connection failed; retrying in %.1fs (attempt %s)", delay, attempt)
            import time

            time.sleep(delay)

# -------------------------------------------------------------------
# Discord Bot
# -------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True

# Small TTL cache and concurrency control for REST fetches
REST_TTL_SECONDS = int(os.getenv("REST_TTL_SECONDS", "180"))
REST_CACHE_MAX = int(os.getenv("REST_CACHE_MAX", "10000"))
REST_CONCURRENCY = int(os.getenv("REST_CONCURRENCY", "8"))


class _TTLCache:
    def __init__(self, maxsize: int, ttl: int):
        self.maxsize = maxsize
        self.ttl = ttl
        self._store: dict[tuple[int, int], tuple[float, object]] = {}

    def get(self, key):
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < asyncio.get_event_loop().time():
            # expired
            self._store.pop(key, None)
            return None
        return value

    def set(self, key, value):
        # simple eviction: pop random when over limit
        if len(self._store) >= self.maxsize:
            try:
                self._store.pop(next(iter(self._store)))
            except StopIteration:
                pass
        self._store[key] = (asyncio.get_event_loop().time() + self.ttl, value)

    def drop(self, key):
        """Forget one entry now, rather than when it expires."""
        self._store.pop(key, None)


_member_fetch_cache = _TTLCache(REST_CACHE_MAX, REST_TTL_SECONDS)
_rest_semaphore = asyncio.Semaphore(REST_CONCURRENCY)

# Dashboard Administrator verdicts, cached separately from the member fetches
# above and for far less time. 180 seconds is the right answer for the
# verification path, where a slightly stale member costs nothing; it is the
# wrong answer for an authority check, because it is also how long a demoted
# admin would keep configuring the server after their role was pulled.
#
# 15s keeps a settings page (four API calls) to one lookup while making
# revocation effectively immediate. Set 0 to expire entries as fast as the
# clock allows, paying a REST call per check.
BOT_API_ADMIN_TTL = _int_env("BOT_API_ADMIN_TTL", 15, minimum=0)
_admin_check_cache = _TTLCache(REST_CACHE_MAX, BOT_API_ADMIN_TTL)

async def fetch_member_cached(
    guild: discord.Guild, user_id: int, *, fresh: bool = False
) -> discord.Member | None:
    """This member, from cache if we have one.

    `fresh=True` costs a REST call and asks Discord now. Pass it wherever the
    answer is an AUTHORITY check rather than a convenience -- the same reason
    the admin-verdict cache below is separate and shorter-lived. A member who
    left, was kicked, or was BANNED stays in this cache for REST_TTL_SECONDS:
    nothing invalidates it, and the bot registers no member-removal listener.
    For the verification path a slightly stale member costs nothing. For the
    group-invite press it was a three-minute window in which somebody a
    moderator had just banned could still put themselves inside that server's
    private VRChat group -- and the Discord ban does not remove them from it
    afterwards.

    A fresh lookup that comes back empty also drops any stale entry, so the
    other callers stop being told the member is still there.
    """
    if not guild:
        return None
    key = (guild.id, user_id)
    if not fresh:
        cached = _member_fetch_cache.get(key)
        if cached:
            return cached  # type: ignore
    async with _rest_semaphore:
        try:
            member = await guild.fetch_member(user_id)
            _member_fetch_cache.set(key, member)
            return member
        except discord.NotFound:
            _member_fetch_cache.drop(key)
            return None


class VRCVerifyBot(discord.Client):
    def __init__(self):
        flags = discord.MemberCacheFlags.none()
        flags.joined = True
        super().__init__(
            intents=intents, chunk_guilds_at_startup=False, member_cache_flags=flags
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # One persistent registration handles button clicks on every panel in
        # every guild. Only the custom_ids are matched, so this instance's
        # locale (and therefore its labels) never reaches a user — each panel
        # keeps whatever labels it was rendered with, and the callbacks resolve
        # locale per interaction.
        self.add_view(VRCVerifyInstructionView(locale="en-US"))
        # The group-invite button carries its guild id in the custom_id, so it
        # is registered as a dynamic item rather than a view: one registration
        # routes every offer DM ever sent, in every server, without the bot
        # holding any of them in memory.
        self.add_dynamic_items(GroupInviteButton)
        # Sync slash commands to the server
        await self.tree.sync()

    async def close(self):
        # Stop listening before the gateway goes away, so the dashboard gets a
        # refused connection rather than requests the bot can no longer answer.
        await stop_bot_api()
        await super().close()


bot = VRCVerifyBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    """Handle common slash-command errors without noisy tracebacks."""
    original = getattr(error, "original", error)

    if isinstance(original, app_commands.MissingPermissions):
        missing = ", ".join(original.missing_permissions)
        msg = (
            "You don't have permission to use this command. "
            f"Missing permission(s): {missing}."
        )
    elif isinstance(original, app_commands.NoPrivateMessage):
        msg = "This command can only be used in a server (not in DMs)."
    elif isinstance(original, app_commands.CheckFailure):
        msg = "You can't use this command here."
    else:
        logger.exception("Unhandled app command error", exc_info=original)
        msg = "Something went wrong while running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        # If we can't respond (e.g., already responded and followup failed), just swallow.
        pass


# -------------------------------------------------------------------
# Premium (Discord App Subscriptions, or a card via Stripe)
# -------------------------------------------------------------------
# Guild-scoped either way: one purchase entitles the whole server. There are
# two ways to buy the identical bundle, and premium is granted if *either* is
# live — a Discord entitlement, or an active row in `stripe_subscription`.
# Never "one overrides the other"; the gate is an OR.
#
# The `subscription_status` / `email` / `last_renewal_date` columns on `servers`
# remain dead leftovers of a Stripe integration removed years before this one,
# and issue #88 deliberately did NOT resurrect them: they cannot hold a customer
# id, a subscription id or a price id, and create_all() cannot add columns to an
# existing table anyway. Card subscriptions live in `stripe_subscription`. If
# you are reading this because you found Stripe code and then found those
# columns, they are still never consulted.


def _optional_int_env(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ %s is set but is not an integer; ignoring it.", name)
        return None


# Where the web dashboard lives, e.g. https://dashboard.vrcverify.com — the
# base only, no path. Configuring settings moved there; the slash commands that
# used to edit them now show what is stored and link here instead.
#
# Unset is handled everywhere rather than assumed away. A self-hoster running
# this bot without a dashboard still gets a usable read-only summary from those
# commands, just with no link on the end, and the reply says where to configure
# instead of dangling a button that goes nowhere.
DASHBOARD_URL = (os.getenv("DASHBOARD_URL") or "").strip().rstrip("/") or None


def _dashboard_page(guild_id, page: str) -> Optional[str]:
    """One guild's page on the dashboard, or None if there is no usable URL."""
    if not DASHBOARD_URL:
        return None
    # Discord rejects a link button whose URL has no scheme, with a 400 that
    # fails the whole interaction -- so a DASHBOARD_URL of
    # "dashboard.vrcverify.com" would not merely omit the button, it would
    # break /vrcverify_setup and every summary command outright. Falling back
    # to no button is a path these callers already handle, so a malformed value
    # costs a link rather than a command.
    if not DASHBOARD_URL.startswith(("https://", "http://")):
        logger.warning(
            "DASHBOARD_URL is missing its scheme (%r); no dashboard link will "
            "be offered. It must start with https://",
            DASHBOARD_URL,
        )
        return None
    return f"{DASHBOARD_URL}/guild/{guild_id}/{page}"


# The VRCVerify Discord, so admins can follow our announcement channel and get
# update posts crossposted into their own servers (issue #138).
#
# ONE value, read from config, rather than the URL written into all twelve
# locale strings. The issue's scope asked for both "invite link in config, not
# hardcoded" and "the URL in all 12 locales", which cannot both be true -- a
# URL repeated twelve times in locales.py is hardcoded twelve times over, and
# rotating it would mean a code change touching every language. So the locale
# strings carry an `{invite}` placeholder and this supplies the value.
#
# Unset means the feature is simply not provisioned, exactly as the invite
# worker treats INVITE_VRCHAT_USERNAME: no sentence is appended anywhere and
# nothing is broken. That is what lets this ship before the channel exists.
SUPPORT_INVITE_URL = (os.getenv("SUPPORT_INVITE_URL") or "").strip() or None


def support_invite_url() -> Optional[str]:
    """The VRCVerify Discord invite, or None if there is no usable one.

    Scheme-checked for the reason _dashboard_page gives, and then some. A bare
    "discord.gg/vrcverify" is the shape somebody will paste, and Discord
    renders an unlinked string rather than a link -- so the admin sees text
    they have to retype. Worse, this string goes into a command reply, so a
    malformed value degrades a support message rather than failing loudly.

    Returning None on a bad value means "no invite line", which every caller
    already handles, so a typo costs a sentence rather than a command.
    """
    if not SUPPORT_INVITE_URL:
        return None
    if not SUPPORT_INVITE_URL.startswith(("https://", "http://")):
        logger.warning(
            "SUPPORT_INVITE_URL is missing its scheme (%r); no invite will be "
            "offered. It must start with https://",
            SUPPORT_INVITE_URL,
        )
        return None
    return SUPPORT_INVITE_URL


def dashboard_guild_url(guild_id) -> Optional[str]:
    """Deep link straight to one server's settings page.

    Landing an admin on the picker and making them find the server they were
    already looking at is the kind of small friction that turns "use the
    website" into "the website is annoying".

    Explicitly `/settings`, not the guild root. The root is the Overview now,
    and every caller of this reached it from a command about *changing*
    something -- the read-only summaries say "change them on the dashboard",
    and `/vrcverify_setup` offers it as the place to finish configuring. A
    button under that text landing on a page of counts would be a worse
    introduction than no button.
    """
    return _dashboard_page(guild_id, "settings")


def dashboard_subscription_url(guild_id) -> Optional[str]:
    """Deep link to one server's Subscriptions page (issue #88).

    Kept apart from the settings link rather than pointed at the guild root,
    for the same reason that one is: every caller reached it from a message
    about *buying or managing* a subscription, and the page that answers is
    not the page the root shows.

    This is the only route by which `/vrcverify_subscription` can offer the
    card path at all. Discord's own purchase button is rendered by Discord and
    knows nothing about the website, and the App Directory store link cannot
    target a guild -- so without DASHBOARD_URL set, the longer plans are
    mentioned in the copy and reachable only by the admin going and finding
    them. That degrades to something honest, which is why it is allowed to
    degrade at all.
    """
    return _dashboard_page(guild_id, "subscription")


# The kill switch. With no SKU configured every gate answers "allowed", so this
# code can ship and run in production before the SKU is even published, with
# behaviour identical to the free bot. Turning the tier on is one env var, not
# a deploy — and if it ever needs turning back off, that is one env var too.
PREMIUM_SKU_ID = _optional_int_env("PREMIUM_SKU_ID")
PREMIUM_ENFORCED = PREMIUM_SKU_ID is not None

# How long a resolved entitlement is trusted before we ask Discord again.
# Purchases don't wait for this: the entitlement gateway events invalidate the
# guild immediately, so the TTL only bounds how stale a *missed* event can get.
PREMIUM_STATUS_TTL = _int_env("PREMIUM_STATUS_TTL", 900)

# How long a *guessed* answer is reused after a failed lookup. Short, because
# it is a guess — but not zero, because without it a Discord outage turns every
# single verification into another failing REST call. That storm arrives
# precisely when Discord is already struggling, and discord.py's 429 backoff
# would then throttle the whole bot, verification DMs included.
PREMIUM_FAILURE_TTL = _int_env("PREMIUM_FAILURE_TTL", 60)

# The second kill switch, for the second way to pay. Unset, the bot never reads
# `stripe_subscription`, never registers the API route that writes it, and tells
# the dashboard `stripe.enabled: false` — so this whole feature ships dormant
# and switches on separately, exactly like PREMIUM_SKU_ID and BOT_API_ENABLED.
#
# Note this is the *bot's* copy of the switch. The dashboard has its own, which
# gates the Stripe secrets and the checkout buttons. They are not redundant:
# the bot's answer is the one that travels in the settings payload, so the
# website can never offer a Buy button for a purchase the bot would refuse to
# record. Turning the dashboard's on without this one means Stripe retries a
# 404 for three days and then gives up — see VPS_RUNBOOK.md before flipping
# either.
#
# The bot holds no Stripe credential of any kind, switched on or off. It reads
# a table the dashboard fills in over mTLS, and that is the entire integration.
STRIPE_ENABLED = (os.getenv("STRIPE_ENABLED") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# How long a resolved card subscription is trusted before we re-read the table.
# Purchases don't wait for this either: the webhook write invalidates the guild
# in the same process, so the TTL only bounds how stale a *missed* webhook can
# get. Longer than the Discord one would be safe — this is a local table, not a
# third party — but matching it keeps one number to reason about.
STRIPE_STATUS_TTL = _int_env("STRIPE_STATUS_TTL", 900)

# Stripe's statuses that mean "this server has paid for what it is using".
#
# `past_due` is deliberately in this set. Stripe is still retrying the card;
# the subscription is not cancelled, a charge is merely late. Cutting a paying
# customer off on the first failed retry is the same mistake the entitlement
# lookup's fail-open exists to avoid. `unpaid` is Stripe having given up, and
# is not in the set.
#
# `canceled` is not here either, but see stripe_active(): a cancelled
# subscription keeps premium until its paid period actually runs out, which is
# how the Discord side already behaves. The two payment paths must not disagree
# about what cancelling means.
STRIPE_PAID_STATUSES = frozenset({"active", "trialing", "past_due"})

# How long a processed webhook event id is kept before the ledger forgets it.
#
# Its only job is to recognise a redelivery, and Stripe stops retrying after
# three days — so anything older than that can no longer be replayed at us and
# keeping it buys nothing. Thirty days is an order of magnitude of headroom on
# a number that is not ours to change.
#
# This exists because the alternative is a table that grows one row per webhook
# forever, which is A-24 exactly: `SessionStore.purge_expired` was written and
# never called, and the table grew unbounded on the VPS until somebody noticed.
STRIPE_EVENT_RETENTION_DAYS = _int_env("STRIPE_EVENT_RETENTION_DAYS", 30)

# Premium guilds get a shorter throttle on actions that hit the shared VRChat
# account. 0 disables the wait entirely.
PREMIUM_VERIFICATION_COOLDOWN_SECONDS = _int_env(
    "PREMIUM_VERIFICATION_COOLDOWN_SECONDS", 3, minimum=0
)

# Auto-verify-on-join is deliberately NOT in this list, and is not gated at
# all. Users expect a verification bot to recognise them and hand out the role
# on join — a server owner described it as simply how these bots work. Charging
# for behaviour people read as baseline doesn't land as "premium", it lands as
# the bot being worse than the alternatives until you pay. It is also the only
# gated feature a *member* could perceive, and members move between servers.
FEATURE_UNVERIFIED_ROLE_REMOVAL = "unverified_role_removal"
FEATURE_NICKNAME_SYNC = "nickname_sync"
FEATURE_CUSTOM_DM = "custom_dm"
FEATURE_REDUCED_COOLDOWN = "reduced_cooldown"
FEATURE_ACTIVITY_LOG = "activity_log"
FEATURE_PRIORITY_QUEUE = "priority_queue"
FEATURE_BRANDED_PANEL = "branded_panel"
# Issue #49. Deliberately not in GRANDFATHERED_FEATURES below and never to
# be added: it did not exist at the cutover, so no server can lose it, and
# it is the one feature whose cost is a real resource rather than code --
# every configured group takes a seat on a VRChat account that holds 100 of
# them (200 with VRC+). Opening it to the free tier means buying accounts
# for servers that pay nothing, which is how a seat cap becomes an outage.
FEATURE_GROUP_INVITE = "group_invite"

# Servers configured before the cutover keep these three for free, forever.
# The reduced cooldown and the activity log are new, so nobody is losing them.
GRANDFATHERED_FEATURES = frozenset(
    {FEATURE_UNVERIFIED_ROLE_REMOVAL, FEATURE_NICKNAME_SYNC, FEATURE_CUSTOM_DM}
)

# Features that exist in the code but are not yet reachable by an admin, and
# are therefore shown to nobody. Issue #49's group invite lands in phases --
# the gate, the storage and the API first, the dashboard that lets anyone
# configure it later -- and everything in between would either sell a feature a
# paying server cannot turn on, or show them a locked control with no way to
# unlock it. Both produce the same support ticket, which nobody can answer.
#
# This one set drives all three surfaces:
#
#   * the premium pitch (locales), via tests/test_premium.py
#   * /vrcverify_settings, via build_settings_summary below
#   * the dashboard settings page, via tests/test_group_invite_config.py
#
# Each of those has a test that fails the moment a name leaves this set, so
# taking the entry out is what forces all three to be built -- in the same
# change that makes the feature real, and it cannot be forgotten afterwards.
# Empty, and normally is. The group invite came out of it when the settings
# page gained controls for it, which is exactly the sequence the comment above
# describes: the name leaves in the change that makes the feature reachable.
UNANNOUNCED_FEATURES = frozenset()


class SettingsField:
    """One configurable setting, and exactly how its plan gate behaves.

    Gating in this bot bites in two different places, and the difference is not
    an accident:

    * `auto_nickname_change` and the panel branding are refused at *save* time —
      write_dashboard_settings rejects them outright and leaves the stored value
      untouched, so an admin who subscribes later gets their original choice
      back rather than whatever a form happened to hold.
    * `unverified_role_id` and the custom DM are saved by anyone. /vrcverify_setup
      stores an unverified role for a free server quite happily; the gate is in
      assign_role, which simply doesn't act on it.

    This table is now the ONLY place that distinction is written down. It used
    to be duplicated in the paged slash-command editor, which is exactly why it
    is worth a docstring: when configuring moved to the website, the editor and
    its second copy of the rules were deleted, and everything that gates a
    setting — the API payload, the write path, and the read-only summary the
    old commands now show — reads it from here.

    `write_locked` says whether an unavailable feature blocks the save.
    `active` — feature allowed right now — is what the "not active on your plan"
    badge keys off, and it is reported for locked and unlocked fields alike.
    """

    def __init__(self, name: str, feature: Optional[str], write_locked: bool):
        self.name = name
        self.feature = feature
        self.write_locked = write_locked

    def state(self, flags: "PremiumFlags") -> dict:
        active = self.feature is None or flags.allows(self.feature)
        return {
            "feature": self.feature,
            "active": active,
            "locked": bool(self.write_locked and not active),
        }


# The complete set of settings the dashboard may ever see or set. An explicit
# allowlist rather than "whatever columns exist": the API cannot express
# "update an arbitrary row" if it only knows about these.
SETTINGS_FIELDS = (
    SettingsField("role_id", None, write_locked=False),
    SettingsField(
        "unverified_role_id", FEATURE_UNVERIFIED_ROLE_REMOVAL, write_locked=False
    ),
    # Never gated, for anyone, ever. See the note above the FEATURE_ constants.
    SettingsField("auto_verify_new_members", None, write_locked=False),
    SettingsField("auto_nickname_change", FEATURE_NICKNAME_SYNC, write_locked=True),
    SettingsField(
        "custom_verification_requested_message", FEATURE_CUSTOM_DM, write_locked=False
    ),
    SettingsField("instructions_locale", None, write_locked=False),
    SettingsField("panel_embed_color", FEATURE_BRANDED_PANEL, write_locked=True),
    SettingsField("panel_show_icon", FEATURE_BRANDED_PANEL, write_locked=True),
    SettingsField("verification_log_channel_id", FEATURE_ACTIVITY_LOG, write_locked=True),
    # write_locked, like the log channel and for the same reason: refusing
    # the save leaves the stored group id and toggle alone, so a server that
    # lapses and resubscribes finds its group still configured rather than
    # having to prove ownership of it a second time.
    SettingsField("vrchat_group_id", FEATURE_GROUP_INVITE, write_locked=True),
    SettingsField(
        "vrchat_group_invite_enabled", FEATURE_GROUP_INVITE, write_locked=True
    ),
)

SETTINGS_FIELDS_BY_NAME = {field.name: field for field in SETTINGS_FIELDS}


def field_is_announced(name: str) -> bool:
    """Whether this setting may be shown to an admin at all yet.

    Read off the field's own feature, so a phased feature is hidden everywhere
    by one entry in UNANNOUNCED_FEATURES rather than by a list per surface.
    """
    field = SETTINGS_FIELDS_BY_NAME.get(name)
    return field is None or field.feature not in UNANNOUNCED_FEATURES


# Which of those the dashboard may WRITE today. Everything else is readable and
# refused on save, including fields that will open later.
#
# Step 5 of issue #65 opened the instructions panel group first and nothing
# else, and the order was deliberate: every one of those values is a constrained
# type — one of a fixed set of language codes, a 24-bit integer, a boolean — so
# the first write path this project ever had could be about the plumbing
# (authorisation, the audit record, refusing a locked field) rather than about
# validating free text or reasoning about role hierarchies at the same time.
# The remaining groups followed once that plumbing was proven, so this is now
# every setting the dashboard offers.
#
# This list is the enforcement point, not the dashboard's form. The website is
# untrusted: it renders whatever it likes, and the bot decides.
DASHBOARD_WRITABLE_FIELDS = frozenset(
    {
        "instructions_locale",
        "panel_embed_color",
        "panel_show_icon",
        "role_id",
        "unverified_role_id",
        "auto_verify_new_members",
        "auto_nickname_change",
        "custom_verification_requested_message",
        "verification_log_channel_id",
        "vrchat_group_id",
        "vrchat_group_invite_enabled",
    }
)

# Settings an already-posted panel RENDERS, as opposed to ones that only affect
# what happens after somebody clicks it. Changing one of these leaves the live
# message stale, because the fleet sweep refreshes the view but passes
# rebuild_embed=False -- so a save that touches any of them has to re-edit the
# panel itself or the change is invisible until an operator forces a refresh.
PANEL_VISIBLE_FIELDS = frozenset(
    {"instructions_locale", "panel_embed_color", "panel_show_icon"}
)

# Outcomes of the post-save restyle that mean "stored, but the live panel does
# not show it". The save is still a success; the admin just needs telling, or
# they are looking at a panel that silently disagrees with their settings.
PANEL_STALE_OUTCOMES = frozenset({"frozen", "style_unreadable", "forbidden", "error"})

# The modal's own cap, so the website cannot store a message the slash command
# could not have produced.
CUSTOM_MESSAGE_MAX_LEN = 1000

# What /vrcverify_setrequestmessage treats as "clear this". Reused rather than
# reimplemented: if the dashboard stored a literal "none", it would be holding
# a value the slash command has no way to set.
CUSTOM_MESSAGE_CLEARING = frozenset({"clear", "reset", "none", "default"})

# How much history one call may ask for. The page shows a recent slice, not an
# export -- an admin wanting the whole thing has the database.
MAX_AUDIT_ROWS = 50


# Raised by the writer below, mapped to a status by bot_api. It lives over
# there because it is the contract between the two halves, not a detail of
# either.
SettingRejected = bot_api.SettingRejected


def _coerce_locale(value):
    if not isinstance(value, str) or value not in LANGUAGE_CODES:
        raise SettingRejected("instructions_locale", "unsupported_language")
    return value


def _coerce_embed_color(value):
    if value is None:
        return None  # Explicitly back to the default blue.
    # bool is a subclass of int, and True would otherwise sail through as the
    # colour #000001.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingRejected("panel_embed_color", "not_a_colour")
    if not 0 <= value <= 0xFFFFFF:
        raise SettingRejected("panel_embed_color", "colour_out_of_range")
    return value


def _coerce_show_icon(value):
    if not isinstance(value, bool):
        raise SettingRejected("panel_show_icon", "not_a_boolean")
    return value


def _role_coercer(field_name: str, *, required: bool):
    """A Discord snowflake, as the string the servers table stores.

    Accepts an int as well as a digit string because JSON has a number type and
    an id that arrived as one is not wrong -- only ambiguous, and normalising
    here is cheaper than a mismatch nobody notices until a role stops matching.
    """

    def coerce(value):
        if value is None or value == "":
            if required:
                # Mirrors /vrcverify_setup, whose verified_role argument is not
                # optional. Verification cannot run without one.
                raise SettingRejected(field_name, "role_required")
            return None
        if isinstance(value, bool):
            raise SettingRejected(field_name, "not_a_role")
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str) or not value.isdigit():
            raise SettingRejected(field_name, "not_a_role")
        return value

    return coerce


def _bool_coercer(field_name: str):
    def coerce(value):
        if not isinstance(value, bool):
            raise SettingRejected(field_name, "not_a_boolean")
        return value

    return coerce


def _coerce_custom_message(value):
    """The custom DM, through the same sanitiser the slash command uses.

    Not a reimplementation: sanitize_custom_message strips zero-width
    characters, defuses @everyone/@here, and allows links only to discord.com
    and vrchat.com. This text is delivered to members by DM, so a second
    implementation that drifted from the first would be a way to say things
    through the bot that the bot's own command refuses to say.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingRejected("custom_verification_requested_message", "not_text")

    raw = value.strip()
    if raw == "" or raw.lower() in CUSTOM_MESSAGE_CLEARING:
        return None
    if len(raw) > CUSTOM_MESSAGE_MAX_LEN:
        raise SettingRejected(
            "custom_verification_requested_message", "message_too_long"
        )

    cleaned, invalid = sanitize_custom_message(raw)
    if invalid:
        raise SettingRejected(
            "custom_verification_requested_message", "message_links_not_allowed"
        )
    # The sanitised text, never the submitted text -- the @everyone defusal has
    # to survive into the database, not just past the check.
    return cleaned


# A VRChat group id: the `grp_` prefix and a UUID. Anchored and exact rather
# than the worker's `startswith("grp_")`, because this is the end a human types
# into. Catching a truncated paste here means the admin is told "that is not a
# group id" by the settings page, instead of "no VRChat group with that ID"
# arriving from a round trip through RabbitMQ several seconds later, which
# reads as the bot being broken rather than the id being wrong.
_GROUP_ID_PATTERN = r"grp_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GROUP_ID_RE = re.compile(r"^" + _GROUP_ID_PATTERN + r"$", re.I)

# https://vrchat.com/home/group/grp_... , with whatever tab or query string the
# admin happened to be on. Accepted because it is what is in the address bar
# when they are looking at the group, and telling someone to extract a
# substring from a URL they can already see is a step that only exists to
# create mistakes.
_GROUP_URL_RE = re.compile(
    r"^https?://(?:www\.)?vrchat\.com/home/group/(" + _GROUP_ID_PATTERN + r")(?:[/?#].*)?$",
    re.I,
)

# vrc.group/SHORTCODE.1234 -- VRChat's own share link, and the one an admin is
# most likely to have copied, because it is what the game hands them. There is
# no endpoint that resolves a short code to a group id, so this cannot be
# accepted however much we would like to: it gets its own refusal rather than
# "that is not a group id", because the admin is holding a perfectly real group
# link and needs telling where the other one is.
_GROUP_SHORTLINK_RE = re.compile(r"^(?:https?://)?vrc\.group/", re.I)


def parse_vrchat_group_id(value) -> Optional[str]:
    """A `grp_...` id out of whatever the admin pasted, or a refusal.

    Empty means "no group", which releases the claim -- the same way leaving
    the channel argument off /vrcverify_logchannel clears the log channel.

    The result is lower-cased. VRChat writes these lower-case, but the claim
    that a group belongs to one guild rests on a UNIQUE column, and two guilds
    submitting the same id in different cases would both be allowed to hold it.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingRejected("vrchat_group_id", "not_a_group")

    raw = value.strip()
    if raw == "":
        return None

    if _GROUP_SHORTLINK_RE.match(raw):
        raise SettingRejected("vrchat_group_id", "group_shortlink_unsupported")

    url = _GROUP_URL_RE.match(raw)
    if url:
        raw = url.group(1)

    if not _GROUP_ID_RE.match(raw):
        raise SettingRejected("vrchat_group_id", "not_a_group")
    return raw.lower()


SETTING_COERCERS = {
    "instructions_locale": _coerce_locale,
    "panel_embed_color": _coerce_embed_color,
    "panel_show_icon": _coerce_show_icon,
    "role_id": _role_coercer("role_id", required=True),
    "unverified_role_id": _role_coercer("unverified_role_id", required=False),
    "auto_verify_new_members": _bool_coercer("auto_verify_new_members"),
    "auto_nickname_change": _bool_coercer("auto_nickname_change"),
    "custom_verification_requested_message": _coerce_custom_message,
    # Same shape as a role id, and clearable the same way /vrcverify_logchannel
    # clears it: by leaving the channel argument off.
    "verification_log_channel_id": _role_coercer(
        "verification_log_channel_id", required=False
    ),
    "vrchat_group_id": parse_vrchat_group_id,
    "vrchat_group_invite_enabled": _bool_coercer("vrchat_group_invite_enabled"),
}

# Fields whose value has to name a real role in *this* guild.
#
# Only existence is enforced, not assignability. /vrcverify_setup performs no
# hierarchy check at all -- Discord's role picker will accept a role above the
# bot quite happily -- so refusing one here would make the website stricter
# than the slash command, and would stop an admin who means to set the role
# first and fix the hierarchy afterwards. The settings page warns about it
# instead, which is the whole reason `assignable` is surfaced.
#
# Existence is different: it is the guarantee Discord's picker provides for
# free and the dashboard has to provide for itself, because it submits a raw
# id rather than a choice from a list the platform vouched for.
ROLE_FIELDS = frozenset({"role_id", "unverified_role_id"})

# The log channel, which unlike a role has rules beyond existing.
#
# An announcement channel is refused outright: other servers can *follow* one,
# and every entry in this log pairs a Discord user with their 18+ status, so a
# followed channel would republish an age disclosure about a named member into
# servers they have no relationship with.
#
# The refusal lives in write_dashboard_settings, with the rest of the
# validation, and is reached from both the website and the read-only commands.
# It used to live in /vrcverify_logchannel, which also confirmed by posting
# into the channel so the write doubled as a permission check; that command no
# longer writes anything, and the permission is now computed from
# `send_messages` rather than proved by sending. One path, checked in one
# place.
#
# THE SAME PROPERTY IS USED DELIBERATELY ELSEWHERE, and the two decisions are
# not in tension. Issue #138 has admins *follow* VRCVerify's own announcement
# channel so update posts reach their servers. Following is the hazard here and
# the entire mechanism there; what differs is the content. Ours is release
# notes written to be broadcast. This log is age disclosures about named
# members, which must never leave the server that produced them. The rule is
# about what is in the channel, never about the channel type.
CHANNEL_FIELDS = frozenset({"verification_log_channel_id"})


# The grandfather line is drawn on the servers table's autoincrementing primary
# key: servers at or below it keep GRANDFATHERED_FEATURES for free, forever.
#
# The id is the cheapest honest answer to "was this server here first": it is
# already recorded, strictly increasing, and needs no backfill and no date
# column that `servers` does not have.
#
# The value itself is NOT configured here — it is captured into
# premium_grandfather_line on the first startup that finds the tier switched
# on. See that model for why. This override exists only as an escape hatch for
# a capture that went wrong (and for tests); leave it unset in normal operation
# so the line stays whatever was true at launch.
PREMIUM_GRANDFATHER_MAX_ID_OVERRIDE = _optional_int_env("PREMIUM_GRANDFATHER_MAX_ID")

# The one-time DM telling existing servers the tier is launching. Manually
# triggered rather than automatic: this is a one-shot announcement to every
# server we have, and it should go out when you decide, not because a container
# happened to restart. Same trickle discipline as the panel nudge once started —
# a burst of DMs across hundreds of unrelated servers is precisely the shape
# Discord's anti-spam heuristics look for.
PREMIUM_CUTOVER_TRIGGER_PATH = os.getenv(
    "PREMIUM_CUTOVER_TRIGGER_PATH", "/tmp/premium_cutover.trigger"
)
PREMIUM_CUTOVER_INTERVAL = _int_env("PREMIUM_CUTOVER_INTERVAL", 300)
PREMIUM_CUTOVER_MAX_PER_SWEEP = _int_env("PREMIUM_CUTOVER_MAX_PER_SWEEP", 20)
PREMIUM_CUTOVER_DM_SPACING = _float_env("PREMIUM_CUTOVER_DM_SPACING", 2.0)
PREMIUM_CUTOVER_MAX_FAILURES = _int_env("PREMIUM_CUTOVER_MAX_FAILURES", 3)

# Verification activity log. Entries are buffered and posted in batches rather
# than one message per verification: Discord allows roughly 5 messages per 5s
# per channel, and that budget is shared with verification DMs and command
# replies. A server running a verification drive would otherwise throttle the
# whole bot — the same reasoning as INSTRUCTIONS_REFRESH_RATE above.
VERIFICATION_LOG_FLUSH_INTERVAL = _int_env("VERIFICATION_LOG_FLUSH_INTERVAL", 5)
# Messages started per second across all guilds during a flush.
VERIFICATION_LOG_RATE = _int_env("VERIFICATION_LOG_RATE", 5)
# Ceiling on entries held for one guild between flushes. Reached only when a
# guild's channel is unreachable or a verification drive outruns the flush, and
# it exists so neither can grow the buffer without bound.
VERIFICATION_LOG_MAX_BUFFERED = _int_env("VERIFICATION_LOG_MAX_BUFFERED", 200)
# Discord's hard limit on message content.
DISCORD_MESSAGE_MAX_LEN = 2000


class PremiumStatusCache:
    """Per-guild entitlement state, in two layers.

    The TTL'd layer keeps us from asking Discord about the same guild on every
    verification. The last-known layer never expires and exists purely for the
    failure path: when a lookup raises, a paying server has to keep working.
    Failing closed there would silently switch off a customer's automation
    because Discord had a bad five minutes, with nothing in the UI to explain
    it — far worse than briefly extending automation to a free server.

    Both maps are bounded by the number of guilds the bot is in, and hold one
    bool each, so there is nothing here for a user to grow on purpose.
    """

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._fresh: dict[str, tuple[float, bool]] = {}
        self._last_known: dict[str, bool] = {}

    def get_fresh(self, guild_id: str) -> bool | None:
        entry = self._fresh.get(guild_id)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._fresh.pop(guild_id, None)
            return None
        return value

    def get_last_known(self, guild_id: str) -> bool | None:
        return self._last_known.get(guild_id)

    def set(self, guild_id: str, value: bool) -> None:
        """Record a value we actually know, and make it the fallback."""
        self._fresh[guild_id] = (time.monotonic() + self.ttl, value)
        self._last_known[guild_id] = value

    def set_provisional(self, guild_id: str, value: bool, ttl: int) -> None:
        """Cache a value we merely inferred, without touching the fallback.

        Two callers, both of which produce answers that are good enough to act
        on but not good enough to become the permanent fail-open baseline: the
        guess made when a lookup fails, and a *negative* read of an interaction
        payload (where "no entitlements field" and "no entitlements" are
        indistinguishable). Letting either write _last_known would let a bad
        guess outlive itself and invert the fail-open behaviour.
        """
        self._fresh[guild_id] = (time.monotonic() + ttl, value)

    def invalidate(self, guild_id: str) -> None:
        """Force the next read to re-check with Discord.

        The last-known value deliberately survives: an entitlement event is a
        reason to re-ask, not a reason to forget what we knew if the re-ask
        then fails.
        """
        self._fresh.pop(guild_id, None)

    def clear(self) -> None:
        self._fresh.clear()
        self._last_known.clear()


premium_status_cache = PremiumStatusCache(PREMIUM_STATUS_TTL)

# The card half of the gate gets its own instance of the same class, never a
# shared one. Two sources of truth sharing a cache means either can corrupt the
# other's fallback — and their fallbacks point in opposite directions on
# purpose (see stripe_active), so a collision would silently invert one of them.
stripe_status_cache = PremiumStatusCache(STRIPE_STATUS_TTL)


def stripe_active(guild_id) -> bool:
    """Does this guild have a live card subscription?

    Reads the mirrored `stripe_subscription` row through a TTL cache, so the
    common answer costs nothing and a miss costs one indexed primary-key
    lookup. The webhook write invalidates the guild directly, in this same
    process, so a purchase takes effect immediately rather than waiting out the
    TTL — the same reason on_entitlement_create invalidates the Discord cache.

    **The failure rule here is the opposite of the Discord one, deliberately.**
    guild_has_premium fails *open* to True, because a Discord API outage must
    not switch off a paying customer. This one falls back to last-known, and to
    **False** when there has never been one. That is not an inconsistency to be
    tidied up: a server that has never had a card subscription cannot acquire
    one during a database blip, so failing open here would hand premium to
    every server in the bot the moment Postgres hiccups. Servers that *do* pay
    by card are still covered, by exactly the same last-known layer the Discord
    path uses. Do not "fix" this to fail open.
    """
    if not STRIPE_ENABLED:
        # Nothing has been sold this way yet. Not merely False — no query at
        # all, which is what keeps resolve_premium_flags_from_interaction free
        # of I/O while the feature is dormant.
        return False
    if guild_id is None:
        return False

    key = panel_view_key(guild_id)
    cached = stripe_status_cache.get_fresh(key)
    if cached is not None:
        return cached

    try:
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            # Any paid row wins. A guild can hold more than one subscription
            # (see the model docstring), and being double-billed must not be
            # able to switch premium off — the warning about it is the
            # website's job, and the gate's only job is to keep saying yes.
            rows = (
                session.query(
                    StripeSubscription.status,
                    StripeSubscription.current_period_end,
                )
                .filter_by(server_id=key)
                .all()
            )
            active = any(
                _stripe_row_is_paid(row.status, row.current_period_end, now)
                for row in rows
            )
        stripe_status_cache.set(key, active)
        return active
    except Exception:
        last_known = stripe_status_cache.get_last_known(key)
        answer = False if last_known is None else last_known
        # Provisional, so a guess can never become the fallback it was derived
        # from — and so a sustained outage doesn't turn every verification into
        # another failing query.
        stripe_status_cache.set_provisional(key, answer, PREMIUM_FAILURE_TTL)
        logger.warning(
            "Stripe subscription lookup failed for guild %s; falling back to %s "
            "for %ss.",
            key,
            "last known value" if last_known is not None else "no subscription",
            PREMIUM_FAILURE_TTL,
            exc_info=True,
        )
        return answer


def record_entitlement_seen(guild_id, source: str = "discord") -> bool:
    """Remember that this guild has held a paid plan. Idempotent.

    Returns True when a row was added, False when one already existed or the
    write failed. Callers use that only for logging: nothing decides anything
    on the difference, because "already recorded" and "recorded just now" mean
    the same thing to the trial gate.

    Never raises. This is bookkeeping hung off gateway events and a startup
    sweep; a failure here must not take down an event handler, and the sweep
    runs again next boot.
    """
    if guild_id is None:
        return False
    key = str(guild_id)
    try:
        with session_scope() as session:
            existing = (
                session.query(PremiumEntitlementSeen.server_id)
                .filter_by(server_id=key)
                .first()
            )
            if existing is not None:
                return False
            session.add(PremiumEntitlementSeen(server_id=key, source=source))
    except IntegrityError:
        # Another writer got there first -- the sweep and a gateway event can
        # land together on a boot. Theirs is as good as ours.
        return False
    except Exception:
        logger.warning(
            "Could not record that guild %s has held a paid plan; the startup "
            "sweep will try again.",
            key,
            exc_info=True,
        )
        return False
    logger.info("Recorded guild %s as having held a paid plan (%s).", key, source)
    return True


def has_ever_paid(guild_id) -> bool:
    """Has this guild held a paid plan at any point, by either route?

    Raises on a database failure rather than answering. The only caller is
    `trial_eligible`, which decides what to do about that, and it is the
    caller that knows which direction is safe -- see there.
    """
    key = str(guild_id)
    with session_scope() as session:
        seen = (
            session.query(PremiumEntitlementSeen.server_id)
            .filter_by(server_id=key)
            .first()
        )
        if seen is not None:
            return True
        # Card subscriptions need no separate ledger: stripe_subscription rows
        # outlive the subscription by design, so the presence of ANY row --
        # active, cancelled, unpaid, years old -- is the record that this guild
        # once paid by card.
        row = (
            session.query(StripeSubscription.stripe_subscription_id)
            .filter_by(server_id=key)
            .first()
        )
        return row is not None


def trial_eligible(guild_id) -> bool:
    """May this guild be offered a free trial?

    The rule is the whole of it: **a server that has never had a paid plan, by
    either route.** Grandfathered servers are eligible — they have never paid,
    and that is the only question asked. Deliberately so: `is_grandfathered`
    compares a row id against a captured line and knows nothing about money,
    and reading it into a payment decision would couple the two in the
    direction the grandfathering rule exists to prevent.

    **This fails CLOSED, and that is the opposite of `guild_has_premium` on
    purpose.** The Discord lookup fails open to True because an API outage must
    not switch off a paying customer — the cost of being wrong is a customer
    losing what they bought. Here the cost of being wrong is the reverse: a
    free month given away to a server that has already had one, repeatably, for
    as long as the database is unhappy. A server that genuinely qualifies and
    is briefly not offered a trial can be offered one a minute later. Do not
    "fix" this to fail open to match the other one.

    Stripe itself cannot help. Each checkout mints a fresh customer object, so
    Stripe holds no memory linking a returning guild to the trial it already
    used; this predicate is the only gate there is.
    """
    if not STRIPE_ENABLED:
        return False
    if guild_id is None:
        return False
    try:
        return not has_ever_paid(guild_id)
    except Exception:
        logger.warning(
            "Could not check trial eligibility for guild %s; refusing the "
            "trial rather than risking a repeat one.",
            guild_id,
            exc_info=True,
        )
        return False


# Same sentinel discipline as BRANDING_UNREADABLE: "we could not read this" is
# a third answer, distinct from "there is no subscription". Collapsing the two
# would put "not subscribed" next to a Buy button on the website during a
# database blip, which is how you sell somebody a second subscription.
STRIPE_UNREADABLE = object()


def load_stripe_subscription(guild_id):
    """One guild's mirrored subscription, for the website to describe.

    Returns a plain dict, None when there has never been a card subscription,
    or STRIPE_UNREADABLE when the table could not be read.

    Deliberately reads the table directly rather than going through
    stripe_active's cache: this is a page render, not the verification gate,
    and it needs the fields behind the answer rather than the boolean. The
    cache exists to keep the *gate* cheap, and borrowing it here would mean
    either caching five more values or answering with a stale renewal date.

    The customer id DOES leave here, and an earlier version of this docstring
    argued it should not. That was wrong, for two reasons found while building
    the page: opening Stripe's billing portal requires a customer id, so the
    website genuinely needs it to offer the one action a subscriber wants; and
    withholding it buys nothing anyway, because the dashboard holds a Stripe
    secret key and can enumerate customers with or without our help.

    No **email** leaves here, and that still holds. Checkout collects one and
    Stripe keeps it; mirroring it would put customer PII in the one database
    that links Discord accounts to VRChat identities, to power a page whose
    answer is "manage billing in the portal".
    """
    if not STRIPE_ENABLED:
        return None
    try:
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            rows = (
                session.query(
                    StripeSubscription.status,
                    StripeSubscription.price_id,
                    StripeSubscription.current_period_end,
                    StripeSubscription.cancel_at_period_end,
                    StripeSubscription.stripe_customer_id,
                )
                .filter_by(server_id=panel_view_key(guild_id))
                .all()
            )
            if not rows:
                return None

            normalised = []
            for row in rows:
                period_end = row.current_period_end
                if period_end is not None and period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=timezone.utc)
                normalised.append(
                    (row, period_end, _stripe_row_is_paid(row.status, period_end, now))
                )

            # Which one the page describes: the paid subscription running
            # longest, or — if none is paid — the one that ended most recently,
            # so "your subscription ended on the 3rd" names the right date
            # rather than an older lapsed one.
            paid = [entry for entry in normalised if entry[2]]
            row, period_end, _active = max(
                paid or normalised,
                key=lambda entry: entry[1] or datetime.min.replace(tzinfo=timezone.utc),
            )
            return {
                "status": row.status,
                # Needed to open Stripe's billing portal, and nothing else.
                "customer_id": row.stripe_customer_id,
                # The price id, not a plan name. Turning it into the words
                # "6 months" needs the slug table, which is dashboard config
                # precisely because test and live mode have different ids --
                # and a second copy of that mapping over here would be a second
                # thing to get wrong on a page about money.
                "price_id": row.price_id,
                "current_period_end": (
                    None if period_end is None else period_end.isoformat()
                ),
                "cancel_at_period_end": bool(row.cancel_at_period_end),
                "active": bool(paid),
                # How many subscriptions this guild is currently paying for.
                # More than one *is* the double-billing case, and the whole
                # reason it can be detected at all is that the table keys by
                # subscription rather than by guild. The website warns; nothing
                # here ever cancels or refunds anything.
                "active_count": len(paid),
            }
    except Exception:
        logger.warning(
            "Could not read the Stripe subscription for guild %s.",
            guild_id,
            exc_info=True,
        )
        return STRIPE_UNREADABLE


def _stripe_row_is_paid(status, current_period_end, now: datetime) -> bool:
    """Does this mirrored row mean the server has paid for what it is using?

    Two independent conditions, both required:

    * the status is one Stripe considers paid (STRIPE_PAID_STATUSES), **or**
      the subscription is cancelled but its paid period has not run out yet;
    * the paid period has not run out.

    That second clause is what makes a `canceled` subscription keep working
    until the date the customer paid through, matching the Discord side's
    "a cancellation leaves it live until the paid period actually runs out".
    """
    if current_period_end is None:
        return False
    # SQLite hands back naive datetimes for a timezone-aware column, so a row
    # written on Postgres and one written in a test compare differently unless
    # this is normalised. Storing UTC is a project-wide invariant, so assuming
    # it here is safe.
    if current_period_end.tzinfo is None:
        current_period_end = current_period_end.replace(tzinfo=timezone.utc)
    if current_period_end <= now:
        return False
    # Past this point the period is still running. `canceled` reaches here and
    # is granted on purpose; `unpaid`, `incomplete` and `incomplete_expired`
    # are refused because no money ever arrived or Stripe has stopped trying.
    return status in STRIPE_PAID_STATUSES or status == "canceled"


def entitlements_grant_premium(entitlements) -> bool:
    """Does this collection of entitlements include a live one for our SKU?"""
    for entitlement in entitlements or ():
        if getattr(entitlement, "sku_id", None) != PREMIUM_SKU_ID:
            continue
        # A refund marks the entitlement deleted; a cancellation leaves it live
        # until the paid period actually runs out. So presence alone is not the
        # test — both of these have to be checked.
        if getattr(entitlement, "deleted", False):
            continue
        if entitlement.is_expired():
            continue
        return True
    return False


def premium_from_interaction(interaction: discord.Interaction) -> bool:
    """Resolve premium straight off an interaction payload, seeding the cache.

    Discord ships the guild's entitlements with every interaction, so this is
    both authoritative and free — no REST call. Absence of our SKU here means
    not entitled, it is not an inconclusive answer.
    """
    if not PREMIUM_ENFORCED:
        return True
    if interaction.guild_id is None:
        return False
    is_premium = entitlements_grant_premium(getattr(interaction, "entitlements", None))
    key = str(interaction.guild_id)
    if is_premium:
        # Finding our SKU here is proof. Safe to make it the fallback.
        premium_status_cache.set(key, is_premium)
    else:
        # Not finding it is only as trustworthy as the payload's completeness,
        # and discord.py builds this from data.get('entitlements', []) — an
        # absent field and a genuinely empty one look identical. Good enough to
        # answer this interaction with, not good enough to become the value a
        # future outage falls back on.
        premium_status_cache.set_provisional(key, is_premium, PREMIUM_STATUS_TTL)
    return is_premium


async def guild_has_premium(guild_id) -> bool:
    """Resolve premium for a guild with no interaction to read it from.

    Used by the paths that run off the RabbitMQ result consumer and the member
    join event. Fails open — see PremiumStatusCache for why.
    """
    if not PREMIUM_ENFORCED:
        return True
    if guild_id is None:
        return False

    key = str(guild_id)
    cached = premium_status_cache.get_fresh(key)
    if cached is not None:
        return cached

    try:
        found = False
        async for entitlement in bot.entitlements(
            guild=discord.Object(id=int(key)),
            skus=[discord.Object(id=PREMIUM_SKU_ID)],
            exclude_ended=True,
            exclude_deleted=True,
        ):
            if entitlements_grant_premium([entitlement]):
                found = True
                break
        premium_status_cache.set(key, found)
        return found
    except Exception:
        last_known = premium_status_cache.get_last_known(key)
        answer = True if last_known is None else last_known
        # Hold the guess briefly so a sustained outage doesn't turn every
        # verification into another failing call. Provisional, so it can never
        # become the fallback it was itself derived from.
        premium_status_cache.set_provisional(key, answer, PREMIUM_FAILURE_TTL)
        logger.warning(
            "Entitlement lookup failed for guild %s; falling back to %s for %ss.",
            key,
            "last known value" if last_known is not None else "premium (fail-open)",
            PREMIUM_FAILURE_TTL,
            exc_info=True,
        )
        return answer


# How many entitlements the startup sweep will page through before giving up.
# Not a limit on the business, a limit on a boot: an unbounded walk of Discord's
# entitlement list on every start is the kind of thing that is free for a year
# and then is not. Overflow is logged loudly, because it means the ledger is
# incomplete and some server will be offered a second free trial.
ENTITLEMENT_SWEEP_MAX = _int_env("ENTITLEMENT_SWEEP_MAX", 2000, minimum=1)


async def sweep_entitlement_history() -> int:
    """Record every guild Discord has ever issued our SKU to.

    The gateway only tells this process about entitlements that change while it
    is connected, which leaves two holes the ledger cannot afford: everything
    that happened before this code shipped — the tier launched 2026-08-03 and
    every server that subscribed and cancelled since then is invisible — and
    anything that changes during a restart or an outage.

    Both close the same way, by asking Discord. `entitlements()` defaults to
    `exclude_ended=False`, so this walks the *ended* ones too, which is the
    entire point: a live entitlement is already visible everywhere, and an
    ended one is the case the trial gate exists to remember.

    Runs on every boot rather than once. Inserts are idempotent, so repeating
    it costs a little API and buys self-healing: a gap opened by downtime is
    closed by the next start rather than needing anyone to notice it. Returns
    the number of guilds newly recorded.
    """
    if not (PREMIUM_ENFORCED and STRIPE_ENABLED):
        # Nothing offers a trial, so nothing needs the ledger yet. This keeps
        # the sweep off the boot path entirely until the feature is live.
        return 0

    seen = 0
    added = 0
    try:
        async for entitlement in bot.entitlements(
            skus=[discord.Object(id=PREMIUM_SKU_ID)], limit=ENTITLEMENT_SWEEP_MAX
        ):
            seen += 1
            if seen > ENTITLEMENT_SWEEP_MAX:
                logger.error(
                    "Entitlement sweep hit its %s cap. The ever-paid ledger is "
                    "INCOMPLETE, and a server past the cap can be offered a "
                    "second free trial. Raise ENTITLEMENT_SWEEP_MAX or replace "
                    "this with an incremental sweep.",
                    ENTITLEMENT_SWEEP_MAX,
                )
                break
            guild_id = getattr(entitlement, "guild_id", None)
            if guild_id is None:
                continue
            if record_entitlement_seen(guild_id):
                added += 1
    except Exception:
        # A failed sweep is not a failed boot. The ledger keeps whatever it
        # already had, gateway events keep adding to it, and the next start
        # tries again -- and trial_eligible fails closed meanwhile, so the
        # worst outcome is a trial not offered rather than one given twice.
        logger.warning(
            "Could not sweep entitlement history; the trial ledger may be "
            "incomplete until the next start.",
            exc_info=True,
        )
        return added

    logger.info(
        "Entitlement sweep: %s entitlements seen, %s guilds newly recorded as "
        "having paid.",
        seen,
        added,
    )
    return added


def capture_grandfather_line() -> int | None:
    """Freeze the grandfather line at today's MAX(servers.id), once, forever.

    Called at startup and does nothing at all unless the tier is switched on
    and no line has been captured yet, so it is a no-op on every boot but one.

    Runs before the tier can gate anything because it happens in on_ready and
    gating happens on interactions. If it fails, no row is written and
    grandfather_line() keeps returning None, which fails open — an outage
    during the one boot that matters must not decide that nobody is
    grandfathered.
    """
    if not PREMIUM_ENFORCED:
        return None
    try:
        with session_scope() as session:
            existing = session.query(PremiumGrandfatherLine).filter_by(id=1).first()
            if existing is not None:
                return existing.max_server_id
            # No servers yet (a brand-new deployment) draws the line at 0:
            # nobody is grandfathered, which is correct — nobody has anything
            # to lose.
            highest = session.query(func.max(Server.id)).scalar() or 0
            session.add(PremiumGrandfatherLine(id=1, max_server_id=highest))
        logger.warning(
            "Premium tier is live. Grandfather line captured at servers.id <= %d: "
            "every server installed before now keeps %s free, permanently. "
            "This is recorded once and will not move.",
            highest,
            ", ".join(sorted(GRANDFATHERED_FEATURES)),
        )
        return highest
    except IntegrityError:
        # Another writer got there first; theirs is as good as ours.
        return grandfather_line()
    except Exception:
        logger.exception(
            "Could not capture the grandfather line; every server will be "
            "treated as grandfathered until this succeeds."
        )
        return None


def grandfather_line() -> int | None:
    """The captured line, or None if it has not been drawn yet.

    None means "we cannot say", and every caller treats that as grandfathered.
    Being wrong in that direction costs a subscription; being wrong the other
    way takes working automation away from someone who had it.
    """
    if PREMIUM_GRANDFATHER_MAX_ID_OVERRIDE is not None:
        return PREMIUM_GRANDFATHER_MAX_ID_OVERRIDE
    try:
        with session_scope() as session:
            row = session.query(PremiumGrandfatherLine.max_server_id).filter_by(id=1).first()
            return None if row is None else row.max_server_id
    except Exception:
        logger.warning("Could not read the grandfather line", exc_info=True)
        return None


def is_grandfathered(guild_id) -> bool:
    """Was this guild installed before the tier went live?

    Compares the server's primary key against the line captured at launch, so
    the answer is fixed the moment the tier is switched on and cannot drift
    afterwards.
    """
    if guild_id is None:
        return False
    try:
        line = grandfather_line()
        if line is None:
            # Not captured (or unreadable). Same fail-open reasoning as the
            # entitlement lookup: don't be the reason a server loses
            # automation it already had.
            return True
        with session_scope() as session:
            row = (
                session.query(Server.id)
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            # No config row at all: nothing was ever configured here, so there
            # is no automation to preserve.
            if row is None or row.id is None:
                return False
            return row.id <= line
    except Exception:
        # Same reasoning as the entitlement fail-open: if we can't tell, don't
        # be the reason an existing server loses automation it already had.
        logger.warning(
            "Could not read grandfather status for guild %s; assuming yes.",
            guild_id,
            exc_info=True,
        )
        return True


class PremiumFlags:
    """Which gated features a guild may use, resolved once.

    assign_role needs three of these answers at once; resolving them
    individually would mean three entitlement reads and three DB queries for a
    single verification.
    """

    def __init__(self, premium: bool, grandfathered: bool):
        self.premium = premium
        self.grandfathered = grandfathered

    def allows(self, feature: str) -> bool:
        if not PREMIUM_ENFORCED or self.premium:
            return True
        return self.grandfathered and feature in GRANDFATHERED_FEATURES

    def cooldown_window(self) -> int | None:
        """The throttle this guild gets, or None for the standard one."""
        if self.allows(FEATURE_REDUCED_COOLDOWN):
            return PREMIUM_VERIFICATION_COOLDOWN_SECONDS
        return None

    def request_priority(self) -> int:
        """Where this guild's requests sit in the queue when there's a backlog."""
        if self.allows(FEATURE_PRIORITY_QUEUE):
            return PREMIUM_REQUEST_PRIORITY
        return DEFAULT_REQUEST_PRIORITY


async def resolve_premium_flags(guild_id) -> PremiumFlags:
    """Resolve every gate for a guild in one pass (no interaction available).

    Premium is an OR over the two ways to buy it. The two sources are resolved
    by separate functions with separate caches and deliberately opposite
    failure rules, and neither is allowed to see the other's answer — see
    stripe_active for why that matters.
    """
    if not PREMIUM_ENFORCED:
        return PremiumFlags(premium=True, grandfathered=True)
    # Short-circuit on the Discord answer: a server already entitled through
    # Discord never needs the table read.
    premium = await guild_has_premium(guild_id) or stripe_active(guild_id)
    # Only worth a DB hit when the answer could still change the outcome.
    grandfathered = False if premium else is_grandfathered(guild_id)
    return PremiumFlags(premium=premium, grandfathered=grandfathered)


def resolve_premium_flags_from_interaction(
    interaction: discord.Interaction,
) -> PremiumFlags:
    """Same, for a command or button where the payload already has the answer.

    `premium_from_interaction` is free — Discord ships the entitlements with
    every interaction — and this function must stay nearly so, because it runs
    on every slash command. The `or` below is what protects that: an entitled
    server never reaches stripe_active at all, and an unentitled one pays one
    cached lookup whose entry is invalidated on purchase rather than expiring
    hot. With STRIPE_ENABLED unset, stripe_active does not query at all.

    There is a test asserting the short-circuit, because losing it would put a
    database query behind every interaction and nothing else would notice.
    """
    if not PREMIUM_ENFORCED:
        return PremiumFlags(premium=True, grandfathered=True)
    premium = premium_from_interaction(interaction) or stripe_active(
        interaction.guild_id
    )
    grandfathered = False if premium else is_grandfathered(interaction.guild_id)
    return PremiumFlags(premium=premium, grandfathered=grandfathered)


# -------------------------------------------------------------------
# Verification activity log
# -------------------------------------------------------------------
# What a guild's log channel is told about each verification. Deliberately
# limited to the Discord user, the outcome and the time.
#
# The VRChat display name and usr_ id are NOT logged, and this is not an
# oversight to be tidied up later. The bot knows the link between someone's
# Discord account and their VRChat identity because it has to; writing that
# link into a server channel publishes it to everyone who can read there, in a
# place whose permissions we neither control nor can audit, and the member
# never agreed to that.
LOG_OUTCOME_VERIFIED = "log_verified"
LOG_OUTCOME_ROLE_FAILED = "log_role_failed"
LOG_OUTCOME_NOT_18 = "log_not_18"


class VerificationLogBuffer:
    """Per-guild pending log lines, drained by the flush task.

    Entries are held here rather than posted as they happen so a busy guild
    becomes one message instead of dozens. Bounded per guild: an unreachable
    channel or a verification drive that outruns the flush must not be able to
    grow this without limit.
    """

    def __init__(self, max_per_guild: int):
        self.max_per_guild = max_per_guild
        self._lines: dict[str, list[str]] = {}
        self._dropped: dict[str, int] = {}

    def add(self, guild_id: str, line: str) -> None:
        lines = self._lines.setdefault(guild_id, [])
        lines.append(line)
        # Drop the oldest rather than refusing the newest: if we are losing
        # entries the recent ones are the ones an admin is looking at.
        while len(lines) > self.max_per_guild:
            lines.pop(0)
            self._dropped[guild_id] = self._dropped.get(guild_id, 0) + 1

    def note_lost(self, guild_id: str, count: int) -> None:
        """Remember that a drained batch never made it to Discord.

        Without this a failed send is invisible: the batch is already out of
        the buffer, so the entries simply cease to exist and the log grows a
        hole that nothing accounts for. An audit log that is quietly
        incomplete is worse than one that is obviously broken.
        """
        if count > 0:
            self._dropped[guild_id] = self._dropped.get(guild_id, 0) + count

    def drain(self) -> dict[str, tuple[list[str], int]]:
        """Take everything buffered, as {guild_id: (lines, dropped_count)}."""
        drained = {
            guild_id: (lines, self._dropped.get(guild_id, 0))
            for guild_id, lines in self._lines.items()
            if lines
        }
        self._lines.clear()
        # Only clear counters we are actually reporting. A guild whose count
        # was recorded while it had no pending lines keeps it for next time.
        for guild_id in drained:
            self._dropped.pop(guild_id, None)
        return drained

    def pending(self, guild_id: str) -> list[str]:
        return list(self._lines.get(guild_id, []))

    def clear(self) -> None:
        self._lines.clear()
        self._dropped.clear()


verification_log_buffer = VerificationLogBuffer(VERIFICATION_LOG_MAX_BUFFERED)


def load_log_channel_id(guild_id) -> Optional[str]:
    """The channel this guild logs verifications to, if any."""
    if guild_id is None:
        return None
    try:
        with session_scope() as session:
            row = (
                session.query(VerificationLogChannel)
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            return row.channel_id if row else None
    except Exception:
        # Logging is never worth breaking verification over.
        logger.warning(
            "Could not read log channel for guild %s.", guild_id, exc_info=True
        )
        return None


def set_log_channel(guild_id, channel_id: Optional[str]) -> None:
    """Point a guild's log at `channel_id`, or clear it when None."""
    key = panel_view_key(guild_id)
    with session_scope() as session:
        row = (
            session.query(VerificationLogChannel).filter_by(server_id=key).first()
        )
        if channel_id is None:
            if row is not None:
                session.delete(row)
            return
        if row is None:
            row = VerificationLogChannel(server_id=key, channel_id=str(channel_id))
            session.add(row)
        else:
            row.channel_id = str(channel_id)
        row.updated_at = datetime.now(timezone.utc)


def generate_group_claim_code() -> str:
    """The code an admin puts in their group description to prove control.

    Same alphabet and same `secrets` source as the member bio code. This one
    gates a group's invite list rather than an 18+ status, but a guessable code
    would let somebody claim a group they do not run.

    A distinct prefix because an admin setting this up may well have their own
    VRC- code on screen at the same time, and two similar codes in two similar
    boxes is a support ticket that explains nothing.
    """
    return "VRCG-" + "".join(
        secrets.choice(_VERIFICATION_CODE_ALPHABET) for _ in range(6)
    )


def load_group_invite_config(guild_id) -> Optional[dict]:
    """This guild's VRChat group row, or None if it has never had one.

    Deliberately does NOT swallow database errors the way load_log_channel_id
    does. Its caller read-modify-writes this row, and a read that answered "no
    group configured" for a connection blip would have the very next write
    store exactly that -- clearing a verified group id and releasing its claim
    for anyone else to take. Failing the whole request is the only safe
    direction here, and read_dashboard_settings already turns an exception into
    "could not read" rather than into defaults.

    Returns plain values rather than the row: the session closes on the way
    out, and a detached instance would raise on first access somewhere else.

    Timestamps come back timezone-aware whatever the backend did with them.
    Everything here stores UTC, but SQLite has no timezone type and hands back
    a naive datetime, so a value written as aware reads back ambiguous -- and
    an ambiguous instant on the wire is one the website renders in whatever
    zone it guesses. Same normalisation load_stripe_subscription does, for the
    same reason.
    """
    if guild_id is None:
        return None

    def utc(value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    with session_scope() as session:
        row = (
            session.query(GroupInviteConfig)
            .filter_by(server_id=panel_view_key(guild_id))
            .first()
        )
        if row is None:
            return None
        return {
            "group_id": row.group_id,
            "group_name": row.group_name,
            "group_icon_url": row.group_icon_url,
            "enabled": bool(row.enabled),
            "invite_account_id": row.invite_account_id,
            "claim_code": row.claim_code,
            "claim_code_issued_at": utc(row.claim_code_issued_at),
            "verify_state": row.verify_state or GROUP_SETUP_UNVERIFIED,
            "verify_error": row.verify_error,
            "verify_requested_at": utc(row.verify_requested_at),
            "verified_at": utc(row.verified_at),
            "verify_job_id": row.verify_job_id,
            "can_invite": bool(row.can_invite),
            "can_see_members": bool(row.can_see_members),
        }


def group_claim_holder(group_id) -> Optional[str]:
    """Which guild, if any, already holds this VRChat group.

    Split out from the save so write_dashboard_settings can refuse a claimed
    group during validation, before it has applied any part of the batch --
    the promise that docstring makes. The save re-checks and the UNIQUE column
    settles the race; this is the check that produces a sentence an admin can
    act on.
    """
    if not group_id:
        return None
    with session_scope() as session:
        row = (
            session.query(GroupInviteConfig)
            .filter(GroupInviteConfig.group_id == group_id)
            .first()
        )
        return row.server_id if row else None


def save_group_invite_config(guild_id, *, group_id, enabled) -> Optional[str]:
    """Store a guild's group id and invite toggle, claiming the group.

    Returns the group the bot has just been DISPLACED from, if any -- the id it
    was in before an admin pointed this guild somewhere else or cleared the
    field. The caller is expected to ask the worker to leave it. Returned
    rather than acted on here because leaving is asynchronous and this function
    is not, and because a seat that is still occupied must not be advertised as
    free before the bot is actually out of the group.

    Raises SettingRejected("vrchat_group_id", "group_claimed_elsewhere") when
    another guild already holds this group. Both halves of that check earn
    their place: the query is what produces an answer worth showing an admin,
    and the UNIQUE constraint is what makes the answer true when two guilds
    submit the same group in the same moment.

    Changing the group id resets everything cached about the VRChat side and
    issues a fresh claim code. One rule, stated twice: nothing learned about
    the old group is evidence about the new one, and a claim code carried over
    would let an admin prove control of a group they have already left.

    `enabled` stays independent of whether a group is configured, because the
    dashboard offers it as its own switch. "On, with no group" therefore
    exists, and every reader has to check both -- which is the same thing they
    would have to do anyway for "on, with a group that failed verification".
    """
    key = panel_view_key(guild_id)
    # Parsed here as well as in the coercer, because this is the function that
    # writes the UNIQUE column and so is the one whose invariant it is. A
    # caller reaching this with an unnormalised id -- a later phase storing
    # what a worker echoed back, say -- would otherwise put a second casing of
    # an already-claimed group into the table, and first-claim-wins would
    # quietly stop being true. Idempotent for anything already parsed.
    group_id = parse_vrchat_group_id(group_id)
    displaced = None
    try:
        with session_scope() as session:
            if group_id is not None:
                clash = (
                    session.query(GroupInviteConfig)
                    .filter(GroupInviteConfig.group_id == group_id)
                    .filter(GroupInviteConfig.server_id != key)
                    .first()
                )
                if clash is not None:
                    raise SettingRejected(
                        "vrchat_group_id", "group_claimed_elsewhere"
                    )

            row = (
                session.query(GroupInviteConfig).filter_by(server_id=key).first()
            )
            if row is None:
                row = GroupInviteConfig(
                    server_id=key, verify_state=GROUP_SETUP_UNVERIFIED
                )
                session.add(row)

            if row.group_id != group_id:
                # Only a group the bot actually got into is worth leaving.
                # verified_at is the marker for that: without it the join
                # never succeeded, so there is nothing to leave and no seat
                # being held on the far side.
                if row.group_id and row.verified_at is not None:
                    displaced = row.group_id
                row.group_id = group_id
                row.group_name = None
                row.group_icon_url = None
                row.invite_account_id = None
                row.verify_state = GROUP_SETUP_UNVERIFIED
                row.verify_error = None
                row.verify_requested_at = None
                row.verified_at = None
                row.verify_job_id = None
                row.can_invite = False
                row.can_see_members = False
                if group_id is None:
                    row.claim_code = None
                    row.claim_code_issued_at = None
                else:
                    row.claim_code = generate_group_claim_code()
                    row.claim_code_issued_at = datetime.now(timezone.utc)

            row.enabled = bool(enabled)
            row.updated_at = datetime.now(timezone.utc)
    except IntegrityError:
        # The unique constraint, i.e. the other guild committed between our
        # query above and our own commit. Same answer, arrived at the hard way.
        raise SettingRejected("vrchat_group_id", "group_claimed_elsewhere")
    return displaced


# The one job type the invite worker handles. Spelled the same as
# vrc_group_inviter.JOB_VERIFY_SETUP; the drift test pins that they match.
JOB_VERIFY_GROUP_SETUP = "verify_group_setup"


def begin_group_verification(guild_id) -> Optional[dict]:
    """Stamp this guild's row as "checking" and return the job to publish.

    The job is built HERE, from the stored row, and never from anything a
    browser sent. That is the same rule the invite worker's module docstring
    enforces from the other end: the only group id that worker will ever join
    is one an authorised admin typed into that guild's own settings. Accepting
    a group id from the request body would hand anyone who can reach the
    dashboard the ability to park the bot in a group of their choosing.

    None means there is nothing to check -- no row, or no group id in it.
    """
    key = panel_view_key(guild_id)
    # Not a UUID: this only has to be unguessable enough that a stale answer
    # cannot be confused with a fresh one, and short enough to log.
    job_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.query(GroupInviteConfig).filter_by(server_id=key).first()
        if row is None or not row.group_id:
            return None
        row.verify_state = GROUP_SETUP_CHECKING
        row.verify_error = None
        row.verify_job_id = job_id
        row.verify_requested_at = now
        row.updated_at = now
        return {
            "type": JOB_VERIFY_GROUP_SETUP,
            "jobID": job_id,
            "guildID": str(guild_id),
            "groupID": row.group_id,
            # Proof the admin controls the group, checked by the worker before
            # it joins anything. None only for a row written before claim codes
            # existed, which the worker treats as "no proof offered".
            "claimCode": row.claim_code,
            # Whether the worker must insist on that proof. True until THIS
            # guild has verified THIS group -- and `verified_at` is cleared
            # whenever the group id changes, so a new group always starts from
            # "prove it".
            #
            # Deciding it here rather than letting the worker skip the check
            # when it finds itself already a member closes a real hole: a guild
            # that releases a group leaves the account inside it, and the next
            # guild to claim that id would inherit a verified setup for a group
            # it has never proved anything about.
            #
            # The flip side is the reason it is not simply "always": an admin
            # who has tidied the code out of their description must not find
            # re-verification failing for it afterwards.
            "requireCode": row.verified_at is None,
        }


def abandon_group_verification(guild_id, job_id: str, message: str) -> None:
    """Undo a "checking" stamp for a job that was never actually sent.

    Without this the row sits in "checking" until the timeout expires, and the
    admin waits two minutes to be told something the bot knew immediately.
    """
    key = panel_view_key(guild_id)
    try:
        with session_scope() as session:
            row = (
                session.query(GroupInviteConfig).filter_by(server_id=key).first()
            )
            # Only if it is still our job. A second click that got further
            # than this one must not be rolled back by this one's failure.
            if row is None or row.verify_job_id != job_id:
                return
            row.verify_state = GROUP_SETUP_WORKER_UNREACHABLE
            row.verify_error = message
            row.verify_job_id = None
            row.updated_at = datetime.now(timezone.utc)
    except Exception:
        logger.warning(
            "Could not clear the group verification state for guild %s.",
            guild_id,
            exc_info=True,
        )


def record_group_verification_result(payload: dict) -> str:
    """Store one verdict from the invite worker. Returns what it did, for logs.

    "stale" is the interesting outcome: the round trip is asynchronous, so a
    slow answer about a group the admin has already replaced can arrive after
    a fast answer about the new one. The job id is what tells them apart, and a
    mismatch is dropped rather than written -- otherwise the page would show a
    verdict about a group it is no longer describing.
    """
    if not isinstance(payload, dict):
        return "bad_payload"
    guild_id = payload.get("guildID")
    job_id = payload.get("jobID")
    state = payload.get("state")
    if not guild_id or not job_id:
        return "bad_payload"
    if state not in GROUP_SETUP_STATES:
        # An unknown verdict stored verbatim would render as nothing at all.
        # Recording it as "the worker said something we do not understand" is
        # the honest version, and names the string in the log.
        logger.warning(
            "Invite worker reported an unknown state %r for guild %s.",
            state,
            guild_id,
        )
        return "unknown_state"

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = (
            session.query(GroupInviteConfig)
            .filter_by(server_id=panel_view_key(guild_id))
            .first()
        )
        if row is None:
            return "unknown_guild"
        if row.verify_job_id != job_id:
            return "stale"
        # The group could have been changed while the job was in flight, in
        # which case save_group_invite_config already cleared verify_job_id --
        # so reaching here means the answer is about the group still stored.
        row.verify_state = state
        row.verify_error = payload.get("error_message") or None
        row.group_name = payload.get("group_name") or None
        row.group_icon_url = payload.get("icon_url") or None
        row.can_invite = bool(payload.get("can_invite"))
        row.can_see_members = bool(payload.get("can_see_members"))
        # Which account actually joined, as reported by the worker that did it.
        # Falls back to this process's configured account for a worker that
        # predates the field; with more than one invite account, only the
        # worker knows the true answer.
        row.invite_account_id = (
            payload.get("accountID") or INVITE_VRCHAT_USER_ID or None
        )
        # Answered, so a duplicate delivery of the same message reads as stale
        # rather than being applied twice.
        row.verify_job_id = None
        if state in GROUP_SETUP_SUCCESS_STATES:
            row.verified_at = now
        row.updated_at = now
        return "applied"


def effective_group_setup_state(config: Optional[dict]) -> str:
    """The stored state, with "checking" expired once nothing answered in time.

    Computed on read rather than by a sweep. A state that expires on its own
    needs no scheduler to be correct, and there is nothing to fail at three in
    the morning -- the same reasoning behind pruning stripe_event from the
    function that inserts into it.
    """
    if not config:
        return GROUP_SETUP_UNVERIFIED
    state = config.get("verify_state") or GROUP_SETUP_UNVERIFIED
    if state != GROUP_SETUP_CHECKING:
        return state
    asked = config.get("verify_requested_at")
    if asked is None:
        # "Checking" with no record of when: nothing can ever expire it, so it
        # is already lost.
        return GROUP_SETUP_TIMED_OUT
    if (datetime.now(timezone.utc) - asked).total_seconds() > GROUP_VERIFY_TIMEOUT_SECONDS:
        return GROUP_SETUP_TIMED_OUT
    return state


# -------------------------------------------------------------------
# One member's standing with one server's group (issue #49, phase 5)
# -------------------------------------------------------------------
def load_group_invite_request(guild_id, discord_id) -> Optional[dict]:
    """This member's row for this server, or None if they have never asked.

    Swallows nothing, for the same reason load_group_invite_config does not:
    its callers decide from the answer whether to offer an invite, and a read
    that answered "never asked" for a connection blip would re-offer to
    somebody who has already said no. Failing loudly is the only safe
    direction; every caller here is wrapped.

    Timestamps come back timezone-aware whatever the backend did with them --
    SQLite has no timezone type and hands back naive datetimes, which the
    cooldown arithmetic below would then compare against an aware `now` and
    raise on.
    """
    if guild_id is None or discord_id is None:
        return None

    def utc(value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    with session_scope() as session:
        row = (
            session.query(GroupInviteRequest)
            .filter_by(
                server_id=panel_view_key(guild_id), discord_id=str(discord_id)
            )
            .first()
        )
        if row is None:
            return None
        return {
            "group_id": row.group_id,
            "vrc_user_id": row.vrc_user_id,
            "state": row.state or GROUP_INVITE_PENDING,
            "job_id": row.job_id,
            "requested_at": utc(row.requested_at),
            "settled_at": utc(row.settled_at),
            "channel_id": row.channel_id,
            "message_id": row.message_id,
        }


def effective_group_invite_state(request: Optional[dict]) -> str:
    """The stored state, with "pending" expired once nothing answered in time.

    Computed on read rather than by a sweep, exactly as
    effective_group_setup_state is: a state that expires on its own needs no
    scheduler to be correct, and there is nothing to fail at three in the
    morning. It matters more here than there, because a request stuck in
    "pending" for ever is a member permanently unable to ask again.
    """
    if not request:
        return GROUP_INVITE_PENDING
    state = request.get("state") or GROUP_INVITE_PENDING
    if state != GROUP_INVITE_PENDING:
        return state
    asked = request.get("requested_at")
    if asked is None:
        # Pending with no record of when: nothing can ever expire it, so it is
        # already lost.
        return GROUP_INVITE_TIMED_OUT
    age = (datetime.now(timezone.utc) - asked).total_seconds()
    if age > GROUP_INVITE_TIMEOUT_SECONDS:
        return GROUP_INVITE_TIMED_OUT
    return state


# Why an invite was not offered or not sent. Returned rather than raised: none
# of these is an error, they are all ordinary answers, and three of the four
# are things the member is told in their own language.
INVITE_REFUSED_SETTLED = "settled"  # already sent / already in / already said no
INVITE_REFUSED_PENDING = "pending"  # one is in flight right now
INVITE_REFUSED_COOLDOWN = "cooldown"  # tried too recently


def group_invite_refusal(
    request: Optional[dict], group_id, *, for_offer: bool = False
) -> Optional[str]:
    """Why this member may not ask for an invite right now, or None if they may.

    One function for both the offer and the press, so the DM and the button
    cannot drift apart about who is eligible. They differ in exactly one place,
    named by `for_offer`, and that difference is the whole re-offer rule:

      * The PRESS refuses every settled state. That is what makes a spent
        button dead -- press it again, press an older one, press one you were
        sent months ago, and the answer is the outcome you already had. Without
        this, a member holding several DMs could turn each one into another
        invite.

      * The OFFER refuses only the cooldown. A new verification is a fresh ask
        by a member who is still 18+ and still here, so EVERY settled outcome
        is re-offered once the cooldown since it has lapsed -- `banned` and
        `blocked` included. GROUP_INVITE_FINAL_STATES is empty and the comment
        on it says why: neither of those is our verdict, both are a cache of
        something VRChat will answer again, and a member cannot be invited
        anywhere a ban still stands because VRChat refuses it.

    What the cooldown bounds is INVITES, not DMs. A member who verifies over
    and over while holding an unpressed button collects more buttons -- that
    has always been true, since a member with no request row at all is offered
    on every verification -- but the first press writes the row that every
    other button then refuses against. So the ceiling stays one invite per
    GROUP_INVITE_COOLDOWN_SECONDS, which is the number VRChat cares about.

    A request about a DIFFERENT group withholds every VERDICT but still spends
    the COOLDOWN, and that split is issue #208. An admin who changes the
    server's group has invalidated everything learned about the old one -- a
    member who already belongs to the group this server used to use knows
    nothing about the one it uses now -- so none of the settled-state refusals
    survive a group change.

    The clock is not a verdict, though. An ATTEMPT spent the invite account's
    shared budget whichever group it was about, and that budget is shared
    across every guild (INVITE_MIN_SPACING_SECONDS). Keying the cooldown on the
    group as well meant an admin alternating between two of them reset it on
    every flip and their members paid nothing: ten attempts, no elapsed time,
    ten jobs. So the cooldown reads the member's request row without comparing
    the group in it.

    Nothing rate-limits the group change itself, and this is why that is not
    the fix. Each flip already costs the admin a full worker verify round trip
    with a fresh claim code (see save_group_invite_config), a limit there would
    constrain an admin with a legitimate reason to move groups twice in a day,
    and it would still leave the per-member attempt rate bounded by nothing but
    a value that admin controls. Bounding the thing being protected needs no
    new clock at all.

    A PENDING request refuses across a group change too, and for a different
    reason from the cooldown: it is not a cached outcome, it is a call that is
    in flight right now. Letting it through would publish a second job and
    overwrite the row the first one's answer will be matched against, orphaning
    it. Not left to the cooldown to catch, even though the shipped defaults
    have it inside that window -- both are env-configurable and this must not
    depend on which way round they are set.
    """
    if not request:
        return None
    state = effective_group_invite_state(request)
    if state == GROUP_INVITE_PENDING:
        return INVITE_REFUSED_PENDING
    if request.get("group_id") == group_id:
        if state in GROUP_INVITE_FINAL_STATES:
            return INVITE_REFUSED_SETTLED
        if state in GROUP_INVITE_SETTLED_STATES and not for_offer:
            return INVITE_REFUSED_SETTLED
    # What is left may be asked again -- a transient failure the member can
    # retry, or (for an offer) an outcome they are allowed a second run at --
    # but not immediately. See GROUP_INVITE_COOLDOWN_SECONDS.
    #
    # Measured from when the attempt CONCLUDED, falling back to when it was
    # made. The two are usually seconds apart, but not under load: the worker
    # spaces its calls by INVITE_MIN_SPACING_SECONDS, and a late answer to a
    # timed-out request is deliberately still accepted, so an invite can land
    # several minutes after it was asked for. Keying on `requested_at` alone
    # would then have the cooldown already spent by the time the member hears
    # anything, and the very next verification would offer again seconds after
    # their invite actually arrived.
    since = request.get("settled_at") or request.get("requested_at")
    if since is not None:
        age = (datetime.now(timezone.utc) - since).total_seconds()
        if age < GROUP_INVITE_COOLDOWN_SECONDS:
            return INVITE_REFUSED_COOLDOWN
    return None


def member_invite_identity(discord_id) -> tuple[Optional[str], bool]:
    """The VRChat account this member is linked to, and whether they are 18+.

    Read separately from the verification flow rather than threaded through
    assign_role's arguments: three of assign_role's four call sites do not have
    it to pass, and the one that does is passing a display name.

    Both halves come from ONE read because every caller needs both and they
    have to describe the same instant. `verification_status` is not a property
    of the Discord account on its own -- it is the verdict on whichever VRChat
    account was linked when it was written -- so reading the flag and the id in
    two queries would let a re-link land between them and invite an account
    that was never the one checked.
    """
    with session_scope() as session:
        user = (
            session.query(User).filter_by(discord_id=str(discord_id)).first()
        )
        if not user:
            return None, False
        return (user.vrc_user_id or None), bool(user.verification_status)


def group_invite_account_fingerprint(vrc_user_id: str) -> str:
    """The account half of an offer button's custom_id.

    Hashed rather than carried whole for two reasons. A custom_id is capped at
    100 characters, and VRChat has used more than one id format (`usr_`+UUID
    today, short alphanumerics historically), so a fixed 16 keeps the template
    from being one format change away from silently matching nothing. It also
    keeps an account id out of a string that gets copied into logs and
    exception reports.

    Only ever compared for equality, never reversed -- this is a change
    detector, not a secret.
    """
    return hashlib.sha256(vrc_user_id.encode("utf-8")).hexdigest()[:16]


JOB_SEND_GROUP_INVITE = "send_group_invite"


def begin_group_invite(
    guild_id, discord_id, *, group_id, vrc_user_id, channel_id, message_id
) -> Optional[dict]:
    """Claim this member's row and return the job to publish, or None.

    Returns None when the row was claimed by something else between the
    button being drawn and being pressed -- the refusal check runs again HERE,
    inside the write, because the check the caller did happened before the
    interaction and a second press could otherwise get through both.

    The job carries no Discord identity. `guildID` and `jobID` are enough for
    the answer to find its way back to this row, and the process holding the
    VRChat session has no business knowing which Discord account is behind a
    usr_ id -- that mapping is the thing the verification log already refuses
    to publish.
    """
    key = panel_view_key(guild_id)
    # secrets, not uuid4, matching begin_group_verification. Nothing here
    # depends on it being unguessable, but two job-id generators in one file
    # that differ for no reason is a question somebody has to answer later.
    job_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        row = (
            session.query(GroupInviteRequest)
            .filter_by(server_id=key, discord_id=str(discord_id))
            .first()
        )
        if row is not None:
            # Every field group_invite_refusal reads, or this check is weaker
            # than the one the caller already did -- and this is the one that
            # matters, because it is the one inside the write. Omitting
            # settled_at made it fall back to requested_at and so let a second
            # job through when a late worker answer settled the row between the
            # caller's check and this one.
            existing = {
                "group_id": row.group_id,
                "state": row.state or GROUP_INVITE_PENDING,
                "requested_at": _utc(row.requested_at),
                "settled_at": _utc(row.settled_at),
            }
            if group_invite_refusal(existing, group_id) is not None:
                return None
        else:
            row = GroupInviteRequest(server_id=key, discord_id=str(discord_id))
            session.add(row)

        row.group_id = group_id
        row.vrc_user_id = vrc_user_id
        row.state = GROUP_INVITE_PENDING
        row.job_id = job_id
        row.requested_at = now
        row.settled_at = None
        row.channel_id = str(channel_id) if channel_id is not None else None
        row.message_id = str(message_id) if message_id is not None else None
        row.updated_at = now

    return {
        "type": JOB_SEND_GROUP_INVITE,
        "jobID": job_id,
        "guildID": str(guild_id),
        "groupID": group_id,
        "vrcUserID": vrc_user_id,
    }


def retire_group_invite_request(guild_id, discord_id) -> bool:
    """Clear a spent request so a fresh offer can be honoured. True if cleared.

    Deleted rather than moved to some "offered again" state, because that IS
    the member's standing now: they have been offered a button and have not
    asked for anything with it, which is exactly a member with no row.

    Called only from the offer, and only after the refusal check has agreed the
    member may be asked again -- so the cooldown has already been measured
    against the request this is about to remove. What stops a re-verification
    loop from becoming an invite tap is that the PRESS writes a new row with a
    new `requested_at`: extra verifications hand out extra buttons, but the
    first press claims the row and every other button then refuses against it.

    Deliberately ahead of the DM rather than after it. If the send fails the
    member is left with no row, which the next verification simply offers
    again; clearing afterwards would leave a live button that the press refuses
    against the very row it was sent to replace.
    """
    try:
        with session_scope() as session:
            deleted = (
                session.query(GroupInviteRequest)
                .filter_by(
                    server_id=panel_view_key(guild_id),
                    discord_id=str(discord_id),
                )
                .delete()
            )
        return bool(deleted)
    except Exception:
        logger.warning(
            "Could not clear a spent invite request for member %s in guild %s.",
            discord_id,
            guild_id,
            exc_info=True,
        )
        return False


def abandon_group_invite(guild_id, discord_id, job_id) -> None:
    """Mark a request whose job never reached the queue.

    Without this the row sits "pending" until the read-side timeout expires it,
    and for that whole window the member cannot ask again for a request that
    provably never left this process. Matched on job_id so a retry that DID get
    through is never clobbered by a late failure report about an older one.
    """
    now = datetime.now(timezone.utc)
    try:
        with session_scope() as session:
            row = (
                session.query(GroupInviteRequest)
                .filter_by(
                    server_id=panel_view_key(guild_id),
                    discord_id=str(discord_id),
                    job_id=job_id,
                )
                .first()
            )
            if row is None or row.state != GROUP_INVITE_PENDING:
                return
            row.state = GROUP_INVITE_WORKER_UNREACHABLE
            row.settled_at = now
            row.updated_at = now
    except Exception:
        logger.warning(
            "Could not record a failed invite publish for guild %s.",
            guild_id,
            exc_info=True,
        )


def record_group_invite_result(data: dict) -> tuple[str, Optional[dict]]:
    """Store one invite verdict. Returns (outcome, the row it landed on).

    The row comes back so the caller can edit the member's DM without a second
    read, and because it carries the channel and message ids that the result
    payload deliberately does not.

    `stale` is the interesting outcome: the answer is about a job this row has
    already moved past. See group_invite_request.job_id -- the round trip is
    asynchronous, so a slow answer to an old question can land after a fast
    answer to a new one, and letting the straggler win would show the member a
    verdict about a request they have already seen the result of.
    """
    if not isinstance(data, dict):
        return "bad_payload", None
    guild_id = data.get("guildID")
    job_id = data.get("jobID")
    state = data.get("state")
    if not guild_id or not job_id:
        return "bad_payload", None
    if state not in GROUP_INVITE_WORKER_STATES:
        # Deliberately narrower than GROUP_INVITE_STATES. See that set: the
        # bot's own three describe this side's bookkeeping and must never
        # arrive from the queue.
        return "unknown_state", None

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = (
            session.query(GroupInviteRequest)
            .filter_by(server_id=panel_view_key(guild_id), job_id=job_id)
            .first()
        )
        if row is None:
            return "unknown_request", None
        if row.state not in {GROUP_INVITE_PENDING, GROUP_INVITE_TIMED_OUT}:
            # Already settled for good -- by an abandon, or by this very
            # message being redelivered. The member has been told something
            # already, and it was true.
            return "stale", None
        # TIMED_OUT is deliberately still open to an answer. The timeout is
        # wall-clock and knows nothing about queue depth, so a verification
        # rush behind INVITE_MIN_SPACING_SECONDS expires rows whose invites are
        # queued and about to succeed. Refusing the late answer left those
        # members reading "VRChat didn't answer" about an invite that was in
        # fact sent, and their row still un-settled, so the next verification
        # offered them a second one.
        row.state = state
        row.settled_at = now
        row.updated_at = now
        return "applied", {
            "discord_id": row.discord_id,
            "group_id": row.group_id,
            "vrc_user_id": row.vrc_user_id,
            "channel_id": row.channel_id,
            "message_id": row.message_id,
            "state": state,
        }


# -------------------------------------------------------------------
# Seats: which invite account serves which guild (issue #49, phase 6)
# -------------------------------------------------------------------
def _utc(value):
    """A stored timestamp, made timezone-aware.

    SQLite has no timezone type and hands back naive datetimes, which every
    comparison against an aware `now` below would raise on.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def seat_usage() -> dict:
    """How many seats each invite account is holding, by account id.

    Counts leases rather than joined groups on purpose: a seat is spent the
    moment it is promised to an admin, not when the bot finally gets into their
    group. See GroupSeatLease.reserved_at.
    """
    with session_scope() as session:
        rows = (
            session.query(
                GroupSeatLease.invite_account_id, func.count(GroupSeatLease.server_id)
            )
            .filter(GroupSeatLease.released_at.is_(None))
            .filter(GroupSeatLease.invite_account_id.isnot(None))
            .group_by(GroupSeatLease.invite_account_id)
            .all()
        )
    return {account_id: int(count) for account_id, count in rows}


def seat_capacity() -> dict:
    """(used, total, free) across the whole roster."""
    usage = seat_usage()
    total = sum(a.seats for a in INVITE_ACCOUNTS)
    # Only counts accounts still on the roster. A retired account's leases are
    # not capacity anyone can use, and counting them as "used" would understate
    # free seats rather than overstate them, which is the safe direction.
    used = sum(usage.get(a.user_id, 0) for a in INVITE_ACCOUNTS)
    return {"used": used, "total": total, "free": max(0, total - used)}


def load_seat_lease(guild_id) -> Optional[dict]:
    """This guild's lease, or None if it has never held a seat."""
    if guild_id is None:
        return None
    with session_scope() as session:
        row = (
            session.query(GroupSeatLease)
            .filter_by(server_id=panel_view_key(guild_id))
            .first()
        )
        if row is None:
            return None
        return {
            "invite_account_id": row.invite_account_id,
            "reserved_at": _utc(row.reserved_at),
            "release_job_id": row.release_job_id,
            "last_premium_at": _utc(row.last_premium_at),
            "released_at": _utc(row.released_at),
        }


def invite_account_for_guild(guild_id) -> Optional[InviteAccount]:
    """The account serving this guild, or None if there is not one.

    Read-only. Assignment is a write and belongs to assign_invite_account,
    which the admin's own actions trigger -- a read that quietly reserved
    capacity would spend seats on anyone who merely opened the settings page.

    A guild with no lease on a SINGLE-account installation gets that account.
    Every guild configured before this table existed is in exactly that
    position, and the alternative is telling them the feature is unavailable
    until a sweep materialises a row that was never in doubt. With several
    accounts the answer genuinely is unknown until one is assigned, so it says
    so instead of guessing.
    """
    lease = load_seat_lease(guild_id)
    if lease and lease["released_at"] is None and lease["invite_account_id"]:
        account = INVITE_ACCOUNTS_BY_ID.get(lease["invite_account_id"])
        if account is not None:
            return account
        # The lease names an account the roster no longer has. Not an error
        # worth failing on -- the operator retired it -- but this guild needs
        # reassigning, and saying None is what makes the dashboard ask for it.
        logger.warning(
            "Guild %s holds a seat on %s, which is no longer in "
            "INVITE_ACCOUNTS.",
            guild_id,
            lease["invite_account_id"],
        )
        return None
    if lease is None and len(INVITE_ACCOUNTS) == 1:
        return INVITE_ACCOUNTS[0]
    return None


def invite_account_to_show(guild_id) -> Optional[InviteAccount]:
    """The account this guild's admin should invite, WITHOUT reserving one.

    The dashboard needs an answer before the guild has committed to anything:
    its own instructions are "paste the code, invite the bot, then run the
    check", so the account has to be on screen first. Reserving on a page load
    would spend capacity on everyone who merely looked.

    A guild that already holds a seat gets that account, always -- that is the
    one sitting in their group, and naming any other would send an admin to
    invite a stranger and get an unexplained "not a member" back.

    Otherwise this previews what assign_invite_account would choose. The two
    can disagree if the last seat on that account goes to somebody else in
    between, which self-corrects: the check assigns for real, and reports
    against whichever account it actually got.
    """
    held = invite_account_for_guild(guild_id)
    if held is not None:
        return held
    usage = seat_usage()
    candidates = [
        account
        for account in INVITE_ACCOUNTS
        if usage.get(account.user_id, 0) < account.seats
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda a: (usage.get(a.user_id, 0), a.user_id))


def assign_invite_account(guild_id) -> Optional[InviteAccount]:
    """Reserve a seat for this guild and return the account, or None if full.

    Sticky. Once a guild holds a seat it keeps it, because the account it was
    told to invite is the account that is in its group -- reassigning would
    tell an admin looking at a working setup to go and invite somebody else.

    Least loaded, not first-with-room. The accounts are separate precisely so
    moderation action against one cannot take the others down, so spreading
    guilds across them means a ban costs a fraction of them rather than all.

    Two guilds assigning in the same instant can both pick the same account and
    put it one over its cap. Left alone deliberately: the loser finds out at
    join time with a clear failure, and the alternative is serialising every
    setup in the product behind one lock to save a seat that VRC+ doubles.
    """
    if not INVITE_ACCOUNTS:
        logger.error(
            "A guild needs an invite account but INVITE_ACCOUNTS is empty."
        )
        return None

    key = panel_view_key(guild_id)
    now = datetime.now(timezone.utc)

    existing = invite_account_for_guild(guild_id)
    if existing is not None:
        # Materialise the lease for a single-account installation that has
        # never had one, so the seat is counted from here on.
        _record_seat_lease(key, existing.user_id, now)
        return existing

    usage = seat_usage()
    candidates = [
        account
        for account in INVITE_ACCOUNTS
        if usage.get(account.user_id, 0) < account.seats
    ]
    if not candidates:
        logger.error(
            "Every invite account is at its seat cap; guild %s cannot be "
            "given one. Provision another account.",
            guild_id,
        )
        return None

    chosen = min(candidates, key=lambda a: (usage.get(a.user_id, 0), a.user_id))
    _record_seat_lease(key, chosen.user_id, now)
    logger.info(
        "Assigned guild %s to invite account %s (%s/%s seats used).",
        guild_id,
        chosen.user_id,
        usage.get(chosen.user_id, 0) + 1,
        chosen.seats,
    )
    return chosen


def _record_seat_lease(key, account_id, now) -> None:
    """Write the reservation. Idempotent for a guild already on that account."""
    with session_scope() as session:
        row = session.query(GroupSeatLease).filter_by(server_id=key).first()
        if row is None:
            row = GroupSeatLease(server_id=key)
            session.add(row)
        elif row.invite_account_id == account_id and row.released_at is None:
            # Already leased to this account. reserved_at deliberately does NOT
            # move -- a guild that merely re-saves its settings would otherwise
            # keep resetting the clock that expires an abandoned reservation.
            #
            # release_job_id DOES clear, and it is the whole point of this
            # branch existing rather than returning early. A guild that lapsed,
            # had a leave sent for it, then resubscribed and pressed "check
            # group setup" arrives here: the account is unchanged, so nothing
            # else needs writing, but it is emphatically no longer waiting on
            # that reclaim. Leaving it set let the in-flight leave's success
            # free a live seat and mark a working setup as reclaimed.
            if row.release_job_id is not None:
                row.release_job_id = None
                row.updated_at = now
            return
        row.invite_account_id = account_id
        row.reserved_at = now
        row.released_at = None
        # A guild that has just been (re)assigned is not waiting on a reclaim.
        # Clearing this is what makes an in-flight leave's answer stale rather
        # than authoritative.
        row.release_job_id = None
        row.updated_at = now


def touch_premium_seen(guild_id, when=None) -> None:
    """Record that this guild was seen paying, for the lapse grace period.

    Only ever moves the timestamp forward. The sweep is what calls this, so a
    guild that stops being swept -- because it lost its lease, say -- keeps the
    last date anyone actually observed rather than drifting to now.
    """
    key = panel_view_key(guild_id)
    moment = when or datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.query(GroupSeatLease).filter_by(server_id=key).first()
        if row is None:
            return
        current = _utc(row.last_premium_at)
        if current is not None and current >= moment:
            return
        row.last_premium_at = moment
        row.updated_at = moment


def release_seat(guild_id) -> Optional[str]:
    """Give a guild's seat back. Returns the account that held it, or None.

    Only the lease is cleared. The guild's configuration is untouched --
    group_id keeps the UNIQUE claim so nobody can take the group while they are
    away, and verified_at survives so a returning admin re-invites the bot
    without pasting the claim code again.
    """
    key = panel_view_key(guild_id)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.query(GroupSeatLease).filter_by(server_id=key).first()
        if row is None or row.released_at is not None:
            return None
        account_id = row.invite_account_id
        row.released_at = now
        row.updated_at = now
    return account_id


def mark_group_seat_released(guild_id) -> None:
    """Record that the bot is out of this guild's group.

    Everything the admin configured survives. Only what the bot LEARNED about
    the VRChat side is cleared, because none of it is true any more:

      * `verified_at` is kept. begin_group_verification computes
        `requireCode` as `verified_at is None`, so keeping it is exactly what
        lets a returning admin re-invite the bot and press check without
        pasting the claim code back into their group description. Clearing it
        would silently turn a one-click return into a full re-setup.
      * `group_id` is kept, and with it the UNIQUE claim -- so nobody can take
        the group while its server is away.
      * `enabled` is kept. The admin never turned it off; we left.
    """
    key = panel_view_key(guild_id)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.query(GroupInviteConfig).filter_by(server_id=key).first()
        if row is None:
            return
        row.verify_state = GROUP_SETUP_SEAT_RELEASED
        row.can_invite = False
        row.can_see_members = False
        row.invite_account_id = None
        row.verify_job_id = None
        row.verify_error = None
        row.updated_at = now


def build_seat_release_job(guild_id, group_id, reason=RELEASE_REASON_RECLAIM) -> dict:
    """The job that takes the bot out of one guild's group.

    Built from the stored lease and the stored group, never from a caller --
    the same rule the join obeys. A leave whose group came from a request body
    would let anyone evict the bot from any group it is in.
    """
    return {
        "type": JOB_LEAVE_GROUP,
        "jobID": secrets.token_hex(16),
        "guildID": str(guild_id),
        "groupID": group_id,
        "reason": reason,
    }


async def request_seat_release(
    guild_id, group_id, account, reason=RELEASE_REASON_RECLAIM
) -> bool:
    """Ask the worker to leave a group. True if the job was accepted.

    The seat is NOT freed here, and for a `displaced` leave it is never freed
    at all -- see RELEASE_REASON_DISPLACED. For a reclaim it is freed when the
    worker reports the bot is actually out, because a seat handed to a second
    guild while the bot still sits in the first one's group is a seat that does
    not exist.
    """
    if not group_id or account is None:
        return False
    job = build_seat_release_job(guild_id, group_id, reason)
    if reason == RELEASE_REASON_RECLAIM:
        # Recorded before the publish, so the answer can be matched to the
        # question. A job that fails to publish leaves a stale id behind, which
        # is harmless: the next sweep overwrites it.
        _record_release_job(guild_id, job["jobID"])
    loop = asyncio.get_running_loop()
    published = await loop.run_in_executor(
        None, publish_group_invite_job, job, account.queue
    )
    if not published:
        logger.warning(
            "Could not ask %s to leave group %s for guild %s; the sweep will "
            "try again.",
            account.user_id,
            group_id,
            guild_id,
        )
        return False
    logger.info(
        "Asked %s to leave group %s, releasing guild %s's seat.",
        account.user_id,
        group_id,
        guild_id,
    )
    return True


def _record_release_job(guild_id, job_id) -> None:
    """Note which reclaim a lease is waiting on."""
    with session_scope() as session:
        row = (
            session.query(GroupSeatLease)
            .filter_by(server_id=panel_view_key(guild_id))
            .first()
        )
        if row is None:
            return
        row.release_job_id = job_id
        row.updated_at = datetime.now(timezone.utc)


async def handle_seat_release_result(data: dict):
    """One leave verdict. The seat is freed only on a definite success."""
    guild_id = data.get("guildID")
    state = data.get("state")
    reason = data.get("reason") or RELEASE_REASON_RECLAIM
    if state not in SEAT_LEAVE_STATES:
        logger.warning(
            "Unknown leave state %r for guild %s; ignoring.", state, guild_id
        )
        return

    if reason != RELEASE_REASON_RECLAIM:
        # A displaced group. Leaving it was the whole job; the guild keeps its
        # seat and whatever it has configured since.
        logger.info(
            "Left guild %s's previous group (%s).", guild_id, state
        )
        return

    if state != SEAT_LEAVE_DONE:
        # Left alone on purpose: the lease stays, so the seat stays spoken for
        # and the sweep tries again. Freeing it now would advertise capacity
        # that the bot is still occupying.
        logger.warning(
            "Could not free guild %s's seat: %s",
            guild_id,
            data.get("error_message"),
        )
        return
    try:
        lease = load_seat_lease(guild_id)
        job_id = data.get("jobID")
        if not lease or lease.get("release_job_id") != job_id:
            # The answer is about a reclaim this lease has already moved past:
            # the guild resubscribed and set itself up again while the leave
            # was in flight. Acting on it would free a live seat and mark a
            # working setup as reclaimed.
            logger.info(
                "Ignoring a stale leave verdict for guild %s (job %s).",
                guild_id,
                job_id,
            )
            return
        account_id = release_seat(guild_id)
        mark_group_seat_released(guild_id)
    except Exception:
        logger.exception("Could not record a released seat for guild %s.", guild_id)
        return
    logger.info(
        "Freed guild %s's seat on %s.", guild_id, account_id or "an unknown account"
    )


def load_seat_sweep_candidates() -> list:
    """Every guild that holds a seat, or should hold one, with its group.

    Returns dicts rather than rows: the session closes on the way out, and the
    sweep does slow asynchronous work with each of these afterwards.
    """
    with session_scope() as session:
        rows = (
            session.query(
                GroupInviteConfig.server_id,
                GroupInviteConfig.group_id,
                GroupInviteConfig.invite_account_id,
                GroupSeatLease.server_id,
                GroupSeatLease.invite_account_id,
                GroupSeatLease.last_premium_at,
                GroupSeatLease.released_at,
            )
            .outerjoin(
                GroupSeatLease,
                GroupSeatLease.server_id == GroupInviteConfig.server_id,
            )
            .filter(GroupInviteConfig.group_id.isnot(None))
            .all()
        )
    return [
        {
            "server_id": server_id,
            "group_id": group_id,
            # Who the worker said actually joined. The only evidence available
            # for a guild that predates the lease table.
            "joined_account_id": joined_account_id,
            # False means there is no lease ROW at all, which is different from
            # a row whose account is null -- the first needs creating, the
            # second needs assigning.
            "has_lease": lease_server_id is not None,
            "invite_account_id": leased_account_id,
            "last_premium_at": _utc(last_premium_at),
            "released_at": _utc(released_at),
        }
        for (
            server_id,
            group_id,
            joined_account_id,
            lease_server_id,
            leased_account_id,
            last_premium_at,
            released_at,
        ) in rows
    ]


def backfill_seat_lease(guild_id, joined_account_id) -> Optional[str]:
    """Give a pre-existing guild the lease its seat always implied.

    Every guild that set up group invites before this table existed holds a
    seat with no row to say so, and that gap is not cosmetic. The lapse clock
    lives on the lease, so without one the guild can never be reclaimed -- and
    seat_capacity() reports its occupied seat as FREE, which sends the next
    admin to an account that is already full.

    Prefers the account the WORKER reported joining: that is the one really
    sitting in the group, and on a multi-account installation it is the only
    thing that knows which. Falls back to the single-account answer, which is
    what every installation running today is.
    """
    account_id = None
    if joined_account_id and joined_account_id in INVITE_ACCOUNTS_BY_ID:
        account_id = joined_account_id
    else:
        account = invite_account_for_guild(guild_id)
        account_id = account.user_id if account else None
    if account_id is None:
        logger.warning(
            "Guild %s holds a group but no seat can be attributed to it; "
            "leaving it out of capacity until an admin sets it up again.",
            guild_id,
        )
        return None
    _record_seat_lease(
        panel_view_key(guild_id), account_id, datetime.now(timezone.utc)
    )
    logger.info("Backfilled guild %s's seat on %s.", guild_id, account_id)
    return account_id


def expire_stale_reservations() -> int:
    """Release seats promised to admins who never finished setting up.

    Without this the feature leaks capacity to everyone who tried it once and
    gave up. Safe to do without asking the worker to leave anything: a
    reservation that never reached a successful verification is, by
    definition, a group the bot never joined.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=SEAT_RESERVATION_GRACE_SECONDS
    )
    now = datetime.now(timezone.utc)
    released = 0
    with session_scope() as session:
        rows = (
            session.query(GroupSeatLease)
            .filter(GroupSeatLease.released_at.is_(None))
            .filter(GroupSeatLease.invite_account_id.isnot(None))
            .all()
        )
        for row in rows:
            reserved = _utc(row.reserved_at)
            if reserved is None or reserved > cutoff:
                continue
            config = (
                session.query(GroupInviteConfig)
                .filter_by(server_id=row.server_id)
                .first()
            )
            # Only an UNUSED reservation. A guild whose setup succeeded holds a
            # real seat, and its release is the lapse path's business, not this
            # one's.
            if config is not None and config.verified_at is not None:
                continue
            row.released_at = now
            row.updated_at = now
            released += 1
    if released:
        logger.info(
            "Released %s seat(s) reserved by setups that were never finished.",
            released,
        )
    return released


def report_seat_capacity() -> dict:
    """Log how much room is left, loudly enough to act on."""
    capacity = seat_capacity()
    if not INVITE_ACCOUNTS:
        return capacity
    if capacity["free"] <= 0:
        logger.error(
            "Every invite account is full (%s/%s seats). New servers cannot "
            "set up group invites until another account is provisioned.",
            capacity["used"],
            capacity["total"],
        )
    elif capacity["free"] <= SEAT_CAPACITY_WARN_FREE:
        logger.warning(
            "Invite capacity is nearly gone: %s of %s seats free. Provision "
            "another VRChat account before it runs out.",
            capacity["free"],
            capacity["total"],
        )
    else:
        logger.info(
            "Invite capacity: %s of %s seats free.",
            capacity["free"],
            capacity["total"],
        )
    return capacity


async def seat_sweep_pass() -> dict:
    """One pass: observe who is paying, reclaim what has lapsed.

    Split from the loop so a test can run exactly one pass without a scheduler,
    and so the loop below has nothing in it but timing.
    """
    now = datetime.now(timezone.utc)
    reclaimed = 0
    asked = 0

    expire_stale_reservations()

    for candidate in load_seat_sweep_candidates():
        if candidate["released_at"] is not None:
            continue
        if asked >= SEAT_SWEEP_MAX_PER_PASS:
            break
        guild_id = candidate["server_id"]
        if not candidate["has_lease"]:
            # A guild from before this table existed. Give it a lease before
            # anything below tries to write to one: touch_premium_seen is a
            # no-op without a row, so the lapse clock would never start and the
            # seat could never be reclaimed.
            if backfill_seat_lease(guild_id, candidate["joined_account_id"]) is None:
                continue

        try:
            flags = await resolve_premium_flags(guild_id)
        except Exception:
            # Never reclaim on an unanswered question. A billing hiccup that
            # read as "not paying" would evict paying customers from their own
            # groups, and the cost of waiting is one sweep.
            logger.warning(
                "Could not resolve premium for guild %s during the seat "
                "sweep; leaving its seat alone.",
                guild_id,
                exc_info=True,
            )
            continue

        if flags.allows(FEATURE_GROUP_INVITE):
            touch_premium_seen(guild_id, now)
            continue

        last_paid = candidate["last_premium_at"]
        if last_paid is None:
            # First time anyone has looked at this guild while it was not
            # paying. The grace period starts NOW rather than at the epoch --
            # a NULL means "not yet observed", never "lapsed long ago", and
            # reading it the other way would evict every lapsed guild the
            # instant this shipped.
            touch_premium_seen(guild_id, now)
            continue
        if (now - last_paid).total_seconds() < SEAT_LAPSE_GRACE_SECONDS:
            continue

        account = invite_account_for_guild(guild_id)
        if account is None:
            # No account to ask, so nothing is holding a group. Free the lease
            # directly rather than leaving it to pin capacity forever.
            release_seat(guild_id)
            reclaimed += 1
            continue

        if asked:
            await asyncio.sleep(SEAT_SWEEP_SPACING)
        if await request_seat_release(guild_id, candidate["group_id"], account):
            asked += 1

    report_seat_capacity()
    return {"asked": asked, "reclaimed": reclaimed}


async def seat_sweep_task(interval_seconds: int = SEAT_SWEEP_INTERVAL):
    """Reclaim seats from guilds that stopped paying a long time ago."""
    while True:
        try:
            outcome = await seat_sweep_pass()
            if outcome["asked"] or outcome["reclaimed"]:
                logger.info(
                    "Seat sweep: asked for %s group(s) to be left, freed %s "
                    "lease(s) outright.",
                    outcome["asked"],
                    outcome["reclaimed"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Seat sweep failed; retrying next interval.")
        # Jittered, like every other repeating schedule here: a fixed interval
        # is what makes many deployments synchronise into a spike.
        await asyncio.sleep(interval_seconds + random.uniform(0, 60))


def forget_log_channel(guild_id) -> None:
    """Drop a log channel reference whose channel no longer exists."""
    try:
        set_log_channel(guild_id, None)
    except Exception:
        logger.exception(f"Failed to clear log channel for guild {guild_id}")


def build_log_line(outcome_key: str, user_id, locale_code: str) -> str:
    """Render one entry.

    The timestamp goes in as Discord's <t:unix:f> markup rather than formatted
    text, so every reader sees it in their own timezone and locale without us
    having to translate a date format twelve times.
    """
    when = int(datetime.now(timezone.utc).timestamp())
    return get_message(
        outcome_key,
        SimpleNamespace(locale=locale_code),
        user=f"<@{user_id}>",
        when=f"<t:{when}:f>",
    )


def queue_verification_log(
    guild_id, user_id, outcome_key: str, log_channel_id, locale: Optional[str] = None
) -> None:
    """Buffer one outcome for the guild's log channel.

    Callers pass the channel id they already loaded, so the common case — a
    guild with no log configured — costs nothing here. They pass the locale for
    the same reason: assign_role has already read it off the server row, and
    re-deriving it here would mean a second query per verification.

    Swallows everything. This is called from inside the verification path, and
    a bookkeeping feature must never be the reason someone fails to get a role.
    """
    if not log_channel_id:
        return
    try:
        locale_code = locale if locale in LANGUAGE_CODES else None
        if locale_code is None:
            guild = bot.get_guild(int(guild_id)) if guild_id else None
            locale_code = get_server_locale_code(str(guild_id), guild)
        verification_log_buffer.add(
            str(guild_id), build_log_line(outcome_key, user_id, locale_code)
        )
    except Exception:
        logger.warning(
            "Could not queue a verification log entry for guild %s.",
            guild_id,
            exc_info=True,
        )


async def log_channel_if_allowed(guild_id, log_channel_id) -> Optional[str]:
    """The guild's log channel, if one is set and the plan allows it.

    Short-circuits before the entitlement read when no channel is configured,
    which is the overwhelming majority of guilds.
    """
    if not log_channel_id:
        return None
    flags = await resolve_premium_flags(guild_id)
    return log_channel_id if flags.allows(FEATURE_ACTIVITY_LOG) else None


# -------------------------------------------------------------------
# Premium: branded instructions panel (colour + thumbnail)
# -------------------------------------------------------------------
DEFAULT_PANEL_COLOR = discord.Color.blue()

# Discord treats an embed colour of 0 as "no colour set" and renders the plain
# grey sidebar, so a server asking for black would appear to have been ignored.
# Nudge it to the darkest value that still registers as a colour.
NEAREST_RENDERABLE_BLACK = 0x010101


def parse_hex_color(raw: str) -> Optional[int]:
    """Parse '#5865F2', '5865F2' or '0x5865F2' into Discord's integer form.

    Returns None for anything else, so the caller can say so rather than
    storing a colour the admin did not choose. Three-digit shorthand (#abc) is
    accepted because people type it.
    """
    if not raw:
        return None
    text = raw.strip().lstrip("#")
    if text[:2].lower() == "0x":
        text = text[2:]
    if len(text) == 3:
        # #abc means #aabbcc.
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    return NEAREST_RENDERABLE_BLACK if value == 0 else value


# Returned when the branding table cannot be read at all. Distinct from None,
# which is the definite answer "this guild has no branding". Collapsing the two
# would mean a database blip during a fleet refresh actively re-editing a
# paying server's panel back to the default look — taking something away
# because we couldn't tell, which is the opposite of how is_grandfathered and
# the entitlement cache behave.
BRANDING_UNREADABLE = object()


def load_panel_branding(guild_id):
    """This guild's stored (colour, show_icon).

    Returns None when the guild definitely has no branding, or
    BRANDING_UNREADABLE when the question could not be answered.
    """
    try:
        with session_scope() as session:
            row = (
                session.query(
                    InstructionPanelBranding.embed_color,
                    InstructionPanelBranding.show_icon,
                )
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            return None if row is None else (row.embed_color, bool(row.show_icon))
    except Exception:
        logger.warning(
            "Could not read panel branding for guild %s; leaving its panel alone.",
            guild_id,
            exc_info=True,
        )
        return BRANDING_UNREADABLE


def save_panel_branding(guild_id, embed_color: Optional[int], show_icon: bool) -> None:
    """Store this guild's panel styling, creating the row on first use.

    Styling that asks for nothing removes the row instead of storing it. The
    settings view saves every page at once, so a premium server that only
    touched its nickname setting would otherwise get a row meaning "default
    colour, no icon" — indistinguishable in effect from having none, but enough
    to make resolve_panel_style do an entitlement lookup for that guild on
    every fleet refresh. That short-circuit is the reason the refresh does not
    cost one REST call per panel, so it is worth protecting.
    """
    key = panel_view_key(guild_id)
    try:
        with session_scope() as session:
            row = (
                session.query(InstructionPanelBranding).filter_by(server_id=key).first()
            )
            if embed_color is None and not show_icon:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = InstructionPanelBranding(server_id=key)
                session.add(row)
            row.embed_color = embed_color
            row.show_icon = bool(show_icon)
            row.updated_at = datetime.now(timezone.utc)
    except Exception:
        logger.exception(f"Failed to save panel branding for guild {guild_id}")


def panel_style(
    branding: Optional[tuple[Optional[int], bool]],
    guild: Optional[discord.Guild],
    allowed: bool,
) -> tuple[discord.Color, Optional[str]]:
    """Turn stored branding into the (colour, thumbnail_url) an embed needs.

    Kept separate from the entitlement read so it can be exercised without a
    database or a Discord connection, and so both call sites provably agree.

    A guild with no icon yields no thumbnail even when show_icon is on: there
    is nothing to show, and Discord rejects an empty URL.
    """
    if not allowed or branding is None:
        return DEFAULT_PANEL_COLOR, None
    stored_color, show_icon = branding
    color = DEFAULT_PANEL_COLOR if stored_color is None else discord.Color(stored_color)
    icon_url = None
    if show_icon and guild is not None:
        icon = getattr(guild, "icon", None)
        icon_url = str(icon.url) if icon else None
    return color, icon_url


async def resolve_panel_style(
    guild_id, guild: Optional[discord.Guild]
) -> Optional[tuple[discord.Color, Optional[str]]]:
    """The styling this guild's panel should currently use.

    None means "we could not tell, leave the panel as it is" — the caller skips
    rebuilding the embed rather than rewriting it to the default. Restyling a
    paying server's panel because of a database hiccup would be worse than
    doing nothing, and the next refresh puts it right.

    Short-circuits before the entitlement read when nothing is configured,
    which is the overwhelming majority of guilds — the fleet refresh would
    otherwise turn into one entitlement lookup per panel.
    """
    branding = load_panel_branding(guild_id)
    if branding is BRANDING_UNREADABLE:
        return None
    if branding is None:
        return DEFAULT_PANEL_COLOR, None
    flags = await resolve_premium_flags(guild_id)
    return panel_style(branding, guild, flags.allows(FEATURE_BRANDED_PANEL))


def chunk_log_lines(lines: list[str], limit: int = DISCORD_MESSAGE_MAX_LEN) -> list[str]:
    """Pack lines into as few messages as Discord's length limit allows."""
    messages: list[str] = []
    current = ""
    for line in lines:
        # A single line longer than the limit would loop forever otherwise.
        line = line[:limit]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def load_log_channels(guild_ids) -> dict[str, str]:
    """Channel ids for several guilds in one query.

    The flush task holds every guild it is about to post to, so reading them
    one at a time would mean a database round trip per guild per cycle, forever.
    """
    keys = [panel_view_key(guild_id) for guild_id in guild_ids]
    if not keys:
        return {}
    try:
        with session_scope() as session:
            rows = (
                session.query(VerificationLogChannel)
                .filter(VerificationLogChannel.server_id.in_(keys))
                .all()
            )
            return {row.server_id: row.channel_id for row in rows}
    except Exception:
        logger.warning("Could not load log channels for flush.", exc_info=True)
        return {}


async def flush_guild_log(
    guild_id: str,
    lines: list[str],
    dropped: int,
    channel_id: Optional[str] = None,
    locale: Optional[str] = None,
) -> str:
    """Post one guild's buffered entries.

    Returns why it went the way it did rather than just whether it worked: the
    caller has to know the difference between "this guild does not want a log"
    and "these entries were lost", because only the second needs reporting to
    the admin later. Same shape as probe_instruction_panel. Never raises.
    """
    if channel_id is None:
        channel_id = load_log_channel_id(guild_id)
    if not channel_id:
        return "no_channel"

    if dropped:
        # Prepended, not appended: the dropped entries are the OLDEST, so a
        # note at the bottom would read as though the gap came after
        # everything above it and put an admin's timeline backwards.
        if locale not in LANGUAGE_CODES:
            guild = bot.get_guild(int(guild_id)) if guild_id else None
            locale = get_server_locale_code(guild_id, guild)
        lines = [
            get_message(
                "log_entries_dropped",
                SimpleNamespace(locale=locale),
                count=dropped,
            )
        ] + lines

    try:
        channel = bot.get_partial_messageable(int(channel_id))
    except (TypeError, ValueError):
        logger.warning("Malformed log channel id for guild %s; clearing.", guild_id)
        forget_log_channel(guild_id)
        return "malformed"

    for content in chunk_log_lines(lines):
        try:
            await channel.send(
                content,
                # Entries render a member as <@id> so they read as a name. Without
                # this every single verification would ping that member, in a
                # channel they may not even be able to see.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            logger.warning(
                "Log channel %s for guild %s is gone; clearing the reference.",
                channel_id,
                guild_id,
            )
            forget_log_channel(guild_id)
            return "gone"
        except discord.Forbidden:
            # Keep the row: permissions get restored, deleted channels do not.
            logger.warning(
                "No permission to post in log channel %s for guild %s; dropping batch.",
                channel_id,
                guild_id,
            )
            return "forbidden"
        except discord.HTTPException:
            logger.warning(
                "Discord rejected a log post for guild %s; dropping batch.",
                guild_id,
                exc_info=True,
            )
            return "http_error"
    return "ok"


# A send that failed means those entries are gone — the batch left the buffer
# before we tried. Only these outcomes count as loss worth telling the admin
# about; "no_channel", "gone" and "malformed" mean the guild has no working log
# to be missing entries from.
LOG_OUTCOMES_LOSING_ENTRIES = {"forbidden", "http_error"}


async def verification_log_flush_task(
    interval_seconds: int = VERIFICATION_LOG_FLUSH_INTERVAL,
):
    """Post buffered verification entries, paced across guilds."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            batches = verification_log_buffer.drain()
            if not batches:
                continue
            channels = load_log_channels(batches.keys())
            pacer = RequestPacer(VERIFICATION_LOG_RATE)
            for guild_id, (lines, dropped) in batches.items():
                await pacer.wait()
                try:
                    outcome = await flush_guild_log(
                        guild_id,
                        lines,
                        dropped,
                        channel_id=channels.get(panel_view_key(guild_id)),
                    )
                except Exception:
                    logger.exception(
                        f"Failed to flush verification log for guild {guild_id}"
                    )
                    outcome = "http_error"
                if outcome in LOG_OUTCOMES_LOSING_ENTRIES:
                    # Carry the count forward so the next batch that does land
                    # says how many never made it.
                    verification_log_buffer.note_lost(guild_id, len(lines) + dropped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Exception during verification log flush", exc_info=True)


# -------------------------------------------------------------------
# Instruction Button
# -------------------------------------------------------------------
class VRCVerifyInstructionView(View):
    def __init__(self, locale: str):
        super().__init__(timeout=None)
        self.locale = locale
        # dynamic labels from localization
        begin_label = localizations.get(locale, localizations['en-US'])['btn_begin_verification']
        update_label = localizations.get(locale, localizations['en-US'])['btn_update_nickname']
        # Fixed custom_ids keep already-posted panels routable after a restart;
        # the labels below are per-guild, but the ids never vary.
        begin_btn = Button(
            label=begin_label,
            style=discord.ButtonStyle.primary,
            custom_id=BEGIN_VERIFICATION_CUSTOM_ID,
        )
        begin_btn.callback = self.begin_verification
        self.add_item(begin_btn)
        # Update Nickname button
        update_btn = Button(
            label=update_label,
            style=discord.ButtonStyle.secondary,
            custom_id=UPDATE_NICKNAME_CUSTOM_ID,
        )
        update_btn.callback = self.update_nickname
        self.add_item(update_btn)
        # Donate button (link buttons can't be colored; the emoji makes it stand out)
        donate_label = localizations.get(locale, localizations['en-US'])['btn_donate']
        donate_btn = Button(label=donate_label, emoji="☕", style=discord.ButtonStyle.link, url=KOFI_URL)
        self.add_item(donate_btn)

    async def begin_verification(self, interaction: discord.Interaction):
        # call the verification helper
        await process_verification(interaction)

    async def update_nickname(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Please use this button inside the server (not in DMs).",
                ephemeral=True,
            )

        user_id = str(interaction.user.id)

        # Resolved once and reused for the queue priority below, so this costs
        # no extra entitlement read.
        flags = resolve_premium_flags_from_interaction(interaction)
        remaining = check_verification_cooldown(
            user_id, window_seconds=flags.cooldown_window()
        )
        if remaining:
            return await interaction.response.send_message(
                get_message("cooldown_active", interaction, seconds=remaining),
                ephemeral=True,
            )

        # fetch user and vrc_user_id
        with session_scope() as session:
            user = session.query(User).filter_by(discord_id=user_id).first()
            if not user:
                return await interaction.response.send_message(
                    get_message("not_verified", interaction), ephemeral=True
                )
            # ensure string type
            vrc_user_id = str(user.vrc_user_id)

        # publish nickname update request
        await publish_to_vrc_checker(
            discord_id=user_id,
            vrc_user_id=vrc_user_id,
            guild_id=str(interaction.guild_id),
            code=None,
            update_nickname=True,
            priority=flags.request_priority(),
        )
        # localized confirmation
        await interaction.response.send_message(
            get_message("nickname_update_requested", interaction), ephemeral=True
        )


# -------------------------------------------------------------------
# Settings summary (read-only -- editing lives on the dashboard)
# -------------------------------------------------------------------
# The paged settings editor used to live here: four pages of selects, a colour
# modal, and its own copy of the premium gating. It was removed when the web
# dashboard took over configuration. What is left reads the SAME payload the
# website renders -- read_dashboard_settings -- so the two can report different
# values only if the bot disagrees with itself.
#
# /vrcverify_setup and /vrcverify_status deliberately survived the cut. One is
# how a server gets configured at all before anyone has heard of the website,
# and the other is what you reach for when something is broken, which is
# exactly when the website may be the broken thing.

# How each setting is titled in the summary. English, like the editor that
# preceded it and like the dashboard itself; the member-facing instructions
# panel is the localised surface and stays that way.
SETTINGS_SUMMARY_LABELS = (
    ("role_id", "Verified role"),
    ("unverified_role_id", "Unverified role"),
    ("auto_verify_new_members", "Auto-verify on join"),
    ("auto_nickname_change", "Nickname sync"),
    ("custom_verification_requested_message", "Custom message"),
    ("instructions_locale", "Instructions language"),
    ("panel_embed_color", "Panel colour"),
    ("panel_show_icon", "Panel icon"),
    ("verification_log_channel_id", "Activity log"),
    ("vrchat_group_id", "VRChat group"),
    ("vrchat_group_invite_enabled", "Group invites"),
)

ROLE_SUMMARY_FIELDS = frozenset({"role_id", "unverified_role_id"})
CHANNEL_SUMMARY_FIELDS = frozenset({"verification_log_channel_id"})


class DashboardLinkView(View):
    """A single link button. Nothing to time out, so no timeout."""

    def __init__(self, url: str, label: str = "Open dashboard"):
        super().__init__(timeout=None)
        self.add_item(
            Button(label=label, style=discord.ButtonStyle.link, url=url)
        )


def _summary_value(name: str, value, guild: discord.Guild) -> str:
    """One stored value, as something an admin can read at a glance.

    Ids resolve through the guild rather than printing raw: an admin who has to
    paste a snowflake somewhere to find out what it is has not been told
    anything.
    """
    if value is None or value == "":
        return "Not set"
    if name in ROLE_SUMMARY_FIELDS:
        role = guild.get_role(int(value)) if str(value).isdigit() else None
        return role.mention if role else f"Deleted role ({value})"
    if name in CHANNEL_SUMMARY_FIELDS:
        channel = guild.get_channel(int(value)) if str(value).isdigit() else None
        return channel.mention if channel else f"Deleted channel ({value})"
    if name == "panel_embed_color":
        return f"#{int(value):06X}"
    if isinstance(value, bool):
        return "On" if value else "Off"
    if name == "custom_verification_requested_message":
        text = str(value)
        # The stored message can be a thousand characters. This is a summary,
        # and the dashboard is one click away for anyone who wants all of it.
        return (text[:97] + "...") if len(text) > 100 else text
    return str(value)


async def build_settings_summary(guild: discord.Guild) -> Optional[discord.Embed]:
    """Everything /vrcverify_settings used to let an admin change, as a read.

    None means the settings could not be read, and the caller must say so
    rather than rendering the defaults. A page of "Not set" for a database
    blip would invite an admin to reconfigure a server that was fine.
    """
    payload = await read_dashboard_settings(guild.id)
    if payload is None:
        return None

    fields = payload.get("fields") or {}
    premium = payload.get("premium") or {}

    embed = discord.Embed(
        title="VRChat Verify settings",
        description=(
            "These are read-only here. Change them on the dashboard."
            if dashboard_guild_url(guild.id)
            else "These are read-only here."
        ),
        color=discord.Color.blurple(),
    )

    # A deployment that never ran the ALTER has no auto_verify_new_members
    # column, so the value below is a default nobody chose and the bot is not
    # acting on it either way. Reporting a bare "On" would be untrue in the one
    # direction that matters -- an admin would believe joiners are being
    # checked when nothing is checking them. The dashboard already says so; the
    # command stopped when it became a summary, and this puts it back.
    auto_verify_present = payload.get("auto_verify_column_present", True)

    for name, label in SETTINGS_SUMMARY_LABELS:
        # A locked row an admin has no way to unlock is worse than no row: it
        # says the bot has a feature, offers no way to reach it, and the answer
        # to "how do I turn this on" is "you cannot yet". See
        # UNANNOUNCED_FEATURES -- one entry hides it here, on the website and
        # in the pitch alike.
        if not field_is_announced(name):
            continue
        state = fields.get(name) or {}
        text = _summary_value(name, state.get("value"), guild)
        # The same two-kinds-of-gated distinction the website draws, for the
        # same reason: "locked" means the bot refuses to store it, "not
        # applied" means it is stored and simply not acted on. Collapsing them
        # would tell an admin they cannot set something they plainly can.
        if state.get("locked"):
            label = f"{label} 🔒"
        elif state.get("active") is False:
            label = f"{label} (not applied)"
        if name == "auto_verify_new_members" and not auto_verify_present:
            label = f"{label} ⚠️"
            text = "Unavailable — this bot's database is missing the column."
        embed.add_field(name=label, value=text, inline=True)

    if premium.get("premium"):
        plan = "Premium is active on this server."
    elif premium.get("grandfathered"):
        plan = "Set up before Premium launched, so several extras stay free."
    else:
        plan = "Free plan. 18+ verification is free forever."
    embed.set_footer(text=plan)
    return embed


async def send_settings_summary(interaction: discord.Interaction) -> None:
    """Shared reply for the commands that used to edit these settings."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = await build_settings_summary(interaction.guild)
    if embed is None:
        await interaction.followup.send(
            get_message("settings_unreadable", interaction), ephemeral=True
        )
        return

    url = dashboard_guild_url(interaction.guild.id)
    extra = {"view": DashboardLinkView(url)} if url else {}
    await interaction.followup.send(embed=embed, ephemeral=True, **extra)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
async def dm_localized(
    member: discord.Member,
    guild: discord.Guild,
    key: str,
    instr_locale: Optional[str] = None,
    **kwargs,
):
    """Send a localized DM to a member; ignore DM permission errors."""
    try:
        locale_code = (
            instr_locale or getattr(guild, "preferred_locale", None)
        ) or "en-US"
        # guild.preferred_locale is a discord.Locale enum, and an enum never
        # compares equal to the plain strings in LANGUAGE_CODES — without this
        # coercion every DM that falls back to the guild's language silently
        # came out in English instead. str(Locale.german) == "de".
        ctx = SimpleNamespace(locale=str(locale_code))
        await member.send(get_message(key, ctx, **kwargs))
    except discord.Forbidden:
        logger.warning(f"⚠️ Cannot DM user {member.id} for key '{key}'.")
    except Exception:
        logger.exception("Unexpected error sending DM.")


async def dm_role_assignment_failure(
    member: discord.Member,
    role: discord.Role,
    guild: discord.Guild,
    instr_locale: Optional[str] = None,
):
    """Explain the common cause: bot role must be above verified/unverified roles."""
    await dm_localized(
        member,
        guild,
        "dm_role_failed_bot_position",
        instr_locale,
        role=role.name,
        server=guild.name,
    )


async def resolve_config_admin(guild: discord.Guild, owner_id) -> Optional[discord.Member]:
    """Find who to DM about a guild's configuration.

    The admin who ran /vrcverify_setup first, since they're the one who made
    the choices; the guild owner if that admin has since left.
    """
    member = None
    try:
        member = await fetch_member_cached(guild, int(owner_id))
    except (TypeError, ValueError):
        pass
    if member is None:
        member = guild.owner or await fetch_member_cached(guild, guild.owner_id)
    return member


def _record_verification_day(guild_id: str) -> None:
    """Add one to this guild's count for today (UTC).

    Its own function, and called before the milestone bookkeeping below, so the
    two do not share a failure. `record_guild_verification` gives up entirely
    when `servers` is missing the columns that feature needs — a deployment
    that never ran that ALTER would otherwise silently never collect a day of
    history either, and the Overview would show empty windows forever with
    nothing to indicate why.

    UPDATE-then-INSERT rather than a dialect-specific upsert: this runs on
    Postgres in production and SQLite in the tests, and the ON CONFLICT syntax
    is not the same in both. The IntegrityError branch is the race where two
    verifications complete on the same day for the same guild at the same
    moment — the loser retries the UPDATE, which now finds the row.
    """
    if not guild_id:
        return
    key = panel_view_key(guild_id)
    today = datetime.now(timezone.utc).date()
    try:
        for attempt in (1, 2):
            with session_scope() as session:
                updated = (
                    session.query(VerificationDaily)
                    .filter_by(server_id=key, day=today)
                    .update(
                        {VerificationDaily.count: VerificationDaily.count + 1},
                        synchronize_session=False,
                    )
                )
                if updated:
                    return
                if attempt == 2:
                    # The insert below already failed once and the row still is
                    # not there. Something is wrong that retrying will not fix.
                    logger.warning(
                        "Could not record a verification day for guild %s.", guild_id
                    )
                    return
                try:
                    session.add(
                        VerificationDaily(server_id=key, day=today, count=1)
                    )
                    session.flush()
                    return
                except IntegrityError:
                    # Someone else inserted the row between our UPDATE and our
                    # INSERT. session_scope rolls back; the retry increments it.
                    session.rollback()
    except Exception:
        # Never let analytics bookkeeping break a verification. The member has
        # already been verified by the time this runs, and a lost count is a
        # gap in a chart, not a failure anyone is waiting on.
        logger.warning(
            "Could not record the verification rollup for guild %s.",
            guild_id,
            exc_info=True,
        )


def _record_server_membership_day() -> None:
    """Snapshot registrations against every guild Discord exposes to us."""
    today = datetime.now(timezone.utc).date()
    try:
        current_guild_ids = {str(guild.id) for guild in bot.guilds}
        with session_scope() as session:
            registered_ids = {
                str(server_id)
                for (server_id,) in session.query(Server.server_id).all()
            }
            registered_count = len(registered_ids)
            active_count = len(current_guild_ids)
            snapshot = session.get(ServerMembershipDaily, today)
            values = {
                "registered_count": registered_count,
                "active_count": active_count,
                "inaccessible_count": len(registered_ids - current_guild_ids),
            }
            if snapshot is None:
                session.add(ServerMembershipDaily(day=today, **values))
            else:
                for field, value in values.items():
                    setattr(snapshot, field, value)
    except Exception:
        logger.warning("Could not record the server membership snapshot.", exc_info=True)


async def server_membership_snapshot_task() -> None:
    """Record one server-membership snapshot at each UTC day boundary."""
    while True:
        now = datetime.now(timezone.utc)
        next_day = now.date() + timedelta(days=1)
        seconds_until_next_day = (
            datetime.combine(next_day, datetime.min.time(), tzinfo=timezone.utc) - now
        ).total_seconds()
        await asyncio.sleep(max(1, seconds_until_next_day))
        _record_server_membership_day()


async def record_guild_verification(guild_id: str, guild: Optional[discord.Guild]):
    """
    Count a completed 18+ verification for a guild. When the guild crosses
    MILESTONE_VERIFICATION_COUNT, DM the admin who configured the bot
    (fallback: the guild owner) a one-time thank-you with the donation link.
    """
    if not guild_id:
        return

    # First, and unconditionally: the daily rollup the dashboard's Overview
    # reads. It shares nothing with the milestone counter below, including that
    # counter's migration guard.
    _record_verification_day(guild_id)

    # Columns may not exist yet if the manual migration hasn't been applied.
    if not (server_has_column("verification_count") and server_has_column("milestone_dm_sent")):
        return

    owner_to_dm = None
    milestone_count = 0
    instr_locale = None
    try:
        with session_scope() as session:
            server = session.query(Server).filter_by(server_id=str(guild_id)).first()
            if not server:
                return
            server.verification_count = (server.verification_count or 0) + 1
            if (
                server.verification_count >= MILESTONE_VERIFICATION_COUNT
                and not server.milestone_dm_sent
            ):
                # Flag first so a DM failure can never cause repeat sends.
                server.milestone_dm_sent = True
                owner_to_dm = server.owner_id
                milestone_count = server.verification_count
                instr_locale = server.instructions_locale
    except Exception:
        logger.warning("⚠️ Could not record guild verification count.", exc_info=True)
        return

    if owner_to_dm is None or guild is None:
        return
    member = await resolve_config_admin(guild, owner_to_dm)
    if member:
        await dm_localized(
            member,
            guild,
            "milestone_owner_dm",
            instr_locale,
            server=guild.name,
            count=milestone_count,
            kofi_link=KOFI_URL,
        )


def get_server_locale_code(guild_id: str | None, guild: Optional[discord.Guild] = None) -> str:
    if guild_id:
        try:
            with session_scope() as session:
                server = session.query(Server).filter_by(server_id=guild_id).first()
                if server and server.instructions_locale in LANGUAGE_CODES:
                    return str(server.instructions_locale)
        except Exception:
            logger.warning("Could not load server locale; falling back.", exc_info=True)

    preferred_locale = getattr(guild, "preferred_locale", None) if guild else None
    return preferred_locale if preferred_locale in LANGUAGE_CODES else "en-US"


def build_vrchat_issue_message(data: dict, locale_code: str = "en-US") -> str:
    error_type = data.get("error_type") or "vrchat_error"
    confirmed_outage = bool(data.get("vrchat_outage_confirmed"))
    suspected_outage = bool(data.get("vrchat_outage"))
    status_message = (data.get("vrchat_status_message") or "").strip()
    status_page = data.get("vrchat_status_page") or "https://status.vrchat.com/"
    ctx = SimpleNamespace(locale=locale_code)

    if error_type == "vrchat_user_not_found":
        return get_message("vrchat_issue_user_not_found", ctx)

    if error_type == "vrchat_rate_limited":
        return get_message("vrchat_issue_rate_limited", ctx)

    if error_type in {"vrchat_auth_error", "vrchat_session_unavailable"}:
        return get_message("vrchat_issue_temp_unavailable", ctx)

    if confirmed_outage:
        if status_message:
            return get_message(
                "vrchat_issue_outage_confirmed_with_status",
                ctx,
                status_page=status_page,
                status_message=escape(status_message[:500]),
            )
        return get_message(
            "vrchat_issue_outage_confirmed",
            ctx,
            status_page=status_page,
        )

    if suspected_outage or error_type in {"vrchat_upstream_error", "vrchat_timeout"}:
        return get_message(
            "vrchat_issue_outage_suspected",
            ctx,
            status_page=status_page,
        )

    return get_message("vrchat_issue_unexpected", ctx)

async def process_verification(interaction: discord.Interaction):
    """
    Processes a verification request by doing one of the following:
      1. If the server's configuration is missing (or the verification role is not set), it notifies the user.
      2. If the user is already verified, it defers the response, assigns the role (or re-assigns), and sends a followup message.
      3. If the user exists but is not verified, it triggers a "no-code" re-check by publishing a verification request.
      4. If the user is not in the database, it presents a modal for the user to enter their VRChat username.
    """
    if interaction.guild_id is None:
        # Happens when the command is used in DMs / user-install context.
        msg = "Please run this command inside the server you want to verify in."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    user_id = str(interaction.user.id)

    # Use a session block to load data and extract only the necessary values.
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server or not server.role_id:
            await interaction.response.send_message(
                get_message("setup_missing", interaction), ephemeral=True
            )
            return

        # Extract the values you need from the user object while still in the session.
        user = session.query(User).filter_by(discord_id=user_id).first()
        if user:
            # Record attempt time when user clicks Begin Verification and already exists in DB
            user.last_verification_attempt = datetime.now(timezone.utc)
            is_verified = user.verification_status
            stored_vrc_user_id = user.vrc_user_id or ""
        else:
            is_verified = None
            stored_vrc_user_id = None

    # CASE A: User exists and is verified.
    if user is not None and is_verified:
        await interaction.response.defer(ephemeral=True)
        await assign_role(user_id, True, guild_id)
        await interaction.followup.send(
            get_message("already_verified", interaction), ephemeral=True
        )
        return

    # CASE B: User exists but is not verified => trigger a "no-code" re-check.
    if user is not None and not is_verified:
        # If we don't have a stored VRChat user id, fall back to relinking.
        if not stored_vrc_user_id:
            await interaction.response.send_modal(VRCUsernameModal(interaction))
            return

        flags = resolve_premium_flags_from_interaction(interaction)
        remaining = check_verification_cooldown(
            user_id, window_seconds=flags.cooldown_window()
        )
        if remaining:
            await interaction.response.send_message(
                get_message("cooldown_active", interaction, seconds=remaining),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await publish_to_vrc_checker(
            discord_id=user_id,
            vrc_user_id=stored_vrc_user_id,
            guild_id=guild_id,
            code=None,  # No-code re-check
            priority=flags.request_priority(),
        )
        await interaction.followup.send(
            get_message("recheck_started", interaction), ephemeral=True
        )
        return

    # CASE C: User is not in the database => show the VRChat username modal.
    await interaction.response.send_modal(VRCUsernameModal(interaction))


# O/I are excluded: visually indistinguishable from 0/1 in the fonts users
# read their code in, which caused users to mistype the code into their bio.
_VERIFICATION_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OI")


def generate_verification_code() -> str:
    # secrets, not random: this token gates 18+ verification, and random's
    # Mersenne Twister is predictable from observed output. 34**6 keyspace.
    return "VRC-" + "".join(secrets.choice(_VERIFICATION_CODE_ALPHABET) for _ in range(6))


# Discord rejects nicknames over 32 characters with a 400 -- which surfaces as
# discord.HTTPException, NOT Forbidden. VRChat display names have no such
# limit, so clamp before sending or the edit raises past the Forbidden-only
# handlers and skips the bookkeeping that follows it.
DISCORD_NICK_MAX_LEN = 32


def discord_safe_nickname(display_name) -> str | None:
    """Clamp a VRChat display name to something Discord will accept."""
    if not isinstance(display_name, str):
        return None
    return display_name.strip()[:DISCORD_NICK_MAX_LEN] or None


# Per-user cooldown on actions that publish to the checker, so a single user
# can't hammer the VRChat API through us (protects the shared bot account).
VERIFICATION_COOLDOWN_SECONDS = int(os.getenv("VERIFICATION_COOLDOWN_SECONDS", "10"))

_verification_cooldowns: dict[str, float] = {}


def check_verification_cooldown(
    user_id: str,
    window_seconds: int | None = None,
    scope: str = "default",
) -> int:
    """Start the user's cooldown for the given action scope if none is active.

    Returns 0 when the action is allowed (cooldown now started), otherwise the
    whole seconds remaining. Blocked attempts do not extend the cooldown.

    Scopes are independent buckets: the code-based Verify button uses its own
    scope so a user who just triggered a re-check or nickname update is never
    blocked from completing verification — only repeated presses of the same
    action are throttled.
    """
    window = VERIFICATION_COOLDOWN_SECONDS if window_seconds is None else window_seconds
    now = time.monotonic()
    key = f"{scope}:{user_id}"
    expires_at = _verification_cooldowns.get(key, 0.0)
    if now < expires_at:
        return int(expires_at - now) + 1

    _verification_cooldowns[key] = now + window
    # Opportunistic cleanup so the map can't grow unbounded.
    if len(_verification_cooldowns) > 10_000:
        for key, expiry in list(_verification_cooldowns.items()):
            if expiry <= now:
                _verification_cooldowns.pop(key, None)
    return 0


VRC_PROFILE_URL_PATTERN = re.compile(r"https?://vrchat\.com/home/user/([A-Za-z0-9\-_]+)")


def parse_vrc_user_input(raw_input: str) -> str | None:
    """Extract a VRChat user ID from a profile URL or a raw usr_ ID.

    Returns None when the input looks like a display name (or anything else
    we can't safely resolve to a user ID).
    """
    raw_input = raw_input.strip()
    m = VRC_PROFILE_URL_PATTERN.match(raw_input)
    if m:
        return m.group(1)
    if raw_input.startswith("usr_"):
        return raw_input
    return None


CUSTOM_MESSAGE_ALLOWED_HOSTS = {"discord.com", "www.discord.com", "vrchat.com", "www.vrchat.com"}


def _is_allowed_custom_message_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in CUSTOM_MESSAGE_ALLOWED_HOSTS


def sanitize_custom_message(raw: str) -> tuple[str, list[str]]:
    """Sanitize an admin-provided custom message.

    Strips zero-width characters, neutralizes @everyone/@here, and returns
    (sanitized_text, invalid_urls) where invalid_urls lists any link whose
    host is not discord.com / vrchat.com (https only).
    """
    raw = re.sub("[\u200B-\u200D\uFEFF]", "", raw)
    raw = re.sub(r"@(everyone|here)\b", r"@ \1", raw, flags=re.IGNORECASE)
    url_pattern = re.compile(r"https?://[^\s>]+", re.IGNORECASE)
    urls = url_pattern.findall(raw)
    invalid = [u for u in urls if not _is_allowed_custom_message_url(u)]
    return raw, invalid


async def publish_to_vrc_checker(
    discord_id: str,
    vrc_user_id: str,
    guild_id: str,
    code: str | None,
    update_nickname: bool = False,
    priority: int = DEFAULT_REQUEST_PRIORITY,
):
    def _publish():
        message = {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID":   guild_id,
            "verificationCode": code
        }
        if update_nickname:
            message["updateNickname"] = True

        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # persistent
            priority=priority,
        )

        max_publish_tries = int(os.getenv("RABBITMQ_PUBLISH_TRIES", "3"))
        last_exc: Exception | None = None
        for attempt in range(1, max_publish_tries + 1):
            conn = None
            try:
                conn = _rabbitmq_connect_with_retry(max_tries=1)
                channel = conn.channel()
                channel.queue_declare(
                    queue=RABBITMQ_REQUEST_QUEUE,
                    durable=True,
                    arguments=request_queue_arguments(),
                )
                channel.basic_publish(
                    exchange="",
                    routing_key=RABBITMQ_REQUEST_QUEUE,
                    body=json.dumps(message),
                    properties=properties,
                )
                logger.info("📤 Sent to vrc_online_checker: %s", message)
                return
            except AMQPError as e:
                # Retrying this one is pointless: the queue's arguments will not
                # change on their own, so every attempt fails identically and
                # the real cause never reaches the log.
                if is_queue_argument_mismatch(e):
                    log_queue_argument_mismatch(RABBITMQ_REQUEST_QUEUE)
                    return
                last_exc = e
                logger.warning(
                    "RabbitMQ publish failed (attempt %s/%s); retrying...",
                    attempt,
                    max_publish_tries,
                    exc_info=True,
                )
                import time

                time.sleep(min(10.0, 1.5 * attempt))
            finally:
                try:
                    if conn and conn.is_open:
                        conn.close()
                except Exception:
                    pass
        logger.error("RabbitMQ publish failed after retries; dropping request", exc_info=last_exc)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _publish)


# -------------------------------------------------------------------
# The member-facing group invite (issue #49, phase 5)
# -------------------------------------------------------------------
# Which sentence the member reads for each outcome. A map rather than a chain
# of ifs because several states deliberately share one message: the difference
# between "the group is gone" and "the bot lost its invite permission" matters
# enormously to the server's admin and not at all to the member, who can do
# nothing about either. Their message says so and points at the admins.
GROUP_INVITE_MESSAGE_KEYS = {
    GROUP_INVITE_SENT: "group_invite_sent",
    GROUP_INVITE_ALREADY_MEMBER: "group_invite_already_member",
    GROUP_INVITE_ALREADY_INVITED: "group_invite_already_invited",
    GROUP_INVITE_BLOCKED: "group_invite_blocked",
    GROUP_INVITE_BANNED: "group_invite_banned",
    GROUP_INVITE_GROUP_NOT_FOUND: "group_invite_setup_problem",
    GROUP_INVITE_USER_NOT_FOUND: "group_invite_account_missing",
    GROUP_INVITE_NO_PERMISSION: "group_invite_setup_problem",
    GROUP_INVITE_VRCHAT_UNAVAILABLE: "group_invite_unavailable",
    # Not "try again in a few minutes": a job the worker calls malformed is a
    # stored VRChat id that will be just as malformed next time. The only
    # thing that changes it is verifying again, which is what this says.
    GROUP_INVITE_BAD_JOB: "group_invite_account_missing",
    GROUP_INVITE_TIMED_OUT: "group_invite_unavailable",
    GROUP_INVITE_WORKER_UNREACHABLE: "group_invite_unavailable",
    GROUP_INVITE_PENDING: "group_invite_working",
}

# Bump this when the button's meaning changes, the way
# INSTRUCTIONS_VIEW_VERSION is bumped. Old DMs keep their old custom_id and
# simply stop matching, which is the correct outcome for a stale offer sitting
# in someone's DMs -- unlike a panel, there is nothing to re-edit.
#
# v1 -> v2: the custom_id gained the account fingerprint below. Every v1 button
# still sitting in a DM stops matching, and that is the point rather than a
# side effect: a v1 button carries no record of which VRChat account it was
# offered for, so there is no way to honour one safely. They were the
# vulnerable population, and retiring them is the fix.
GROUP_INVITE_VIEW_VERSION = 2
GROUP_INVITE_CUSTOM_ID_PREFIX = f"vrcverify:groupinvite:v{GROUP_INVITE_VIEW_VERSION}:"

# Strong references to the per-request expiry tasks below.
#
# asyncio keeps only a weak reference to a running task, so one whose handle
# nobody holds can be collected mid-await. These sleep for
# GROUP_INVITE_TIMEOUT_SECONDS before doing anything, which is an unusually
# long window in which to be quietly discarded -- and the symptom would be a
# member left watching "asking VRChat" with nothing in the log.
#
# Deliberately NOT bot.background_tasks: that dict is the registry of the
# long-running singletons on_ready starts, and its contents are asserted
# against a fixed list. These are ordinary short-lived tasks that happen to
# need an owner.
_group_invite_expiry_tasks: set = set()


class GroupInviteButton(
    discord.ui.DynamicItem[Button],
    template=(
        rf"vrcverify:groupinvite:v{GROUP_INVITE_VIEW_VERSION}:"
        r"(?P<guild_id>[0-9]{1,20}):(?P<account>[0-9a-f]{16})"
    ),
):
    """"Send me an invite", in one member's DM, for one server's group.

    A DynamicItem rather than a fixed custom_id like the instruction panel's
    buttons, because this one has to carry WHICH server it is about. The panel
    lives in a guild channel, so an interaction on it arrives with a guild_id
    attached; a DM has none, and a member verified in several servers can be
    holding several of these at once. The guild id therefore travels in the
    custom_id and discord.py matches it back out with the template above.

    Neither field is trusted for anything. They select a row; every question
    that follows -- is the feature on, is the plan current, is this member
    still 18+, has this member already asked -- is answered from that row and
    from the member's own record. A custom_id can only come from a message this
    bot posted, but the config behind it can have changed completely since it
    did.

    The account fingerprint is what makes this button an offer to one VRChat
    account rather than a standing capability. The callback resolves the
    member's CURRENT account and refuses if it is not the one the offer was
    made for -- without that, re-linking silently hands an old button's invite
    to the new account, which is how a member who had lost their 18+ status
    reached a private group.
    """

    def __init__(self, guild_id: int, account: str, locale: str = "en-US"):
        self.guild_id = guild_id
        self.account = account
        super().__init__(
            Button(
                label=localizations.get(locale, localizations["en-US"]).get(
                    "btn_group_invite", localizations["en-US"]["btn_group_invite"]
                ),
                style=discord.ButtonStyle.primary,
                custom_id=f"{GROUP_INVITE_CUSTOM_ID_PREFIX}{guild_id}:{account}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        # The label here is never rendered. Discord already has the message,
        # with whatever label it was posted with; this instance exists only to
        # route the click, so it deliberately does not try to re-resolve the
        # server's locale for a string nobody will see.
        return cls(int(match["guild_id"]), match["account"])

    async def callback(self, interaction: discord.Interaction):
        await handle_group_invite_press(
            interaction, self.guild_id, self.account
        )


class GroupInviteOfferView(View):
    """The offer DM's single button. Persistent: timeout=None, no state."""

    def __init__(self, guild_id: int, account: str, locale: str = "en-US"):
        super().__init__(timeout=None)
        self.add_item(GroupInviteButton(guild_id, account, locale))


async def group_invite_target(guild_id) -> Optional[dict]:
    """This server's group, if it is genuinely ready to invite people, else None.

    Every condition that has to hold before ANY invite goes out, in one place,
    so the DM that offers the button and the callback that acts on it cannot
    drift apart. The callback re-runs this rather than trusting that the offer
    was correct when it was made -- a subscription can lapse, an admin can turn
    the feature off, and a group's permissions can be taken away, all between
    the DM being sent and the button being pressed.
    """
    try:
        config = load_group_invite_config(guild_id)
    except Exception:
        logger.warning(
            "Could not read the group config for guild %s.", guild_id, exc_info=True
        )
        return None
    if not config or not config.get("group_id") or not config.get("enabled"):
        return None
    # `enabled` is never the gate on its own: the settings field is
    # write_locked, so a lapsed server keeps its True untouched. The plan has
    # to be resolved as well -- see the comment on the column.
    if effective_group_setup_state(config) != GROUP_SETUP_READY:
        return None
    if not config.get("can_invite"):
        return None
    try:
        flags = await resolve_premium_flags(guild_id)
    except Exception:
        logger.warning(
            "Could not resolve premium flags for guild %s while considering a "
            "group invite.",
            guild_id,
            exc_info=True,
        )
        return None
    if not flags.allows(FEATURE_GROUP_INVITE):
        return None
    return config


async def offer_group_invite(
    member: discord.Member,
    guild: discord.Guild,
    instr_locale: Optional[str] = None,
    premium: Optional["PremiumFlags"] = None,
) -> None:
    """DM a freshly verified member the opt-in button, if they should have one.

    Nothing here touches VRChat. The whole point of the opt-in design is that
    no invite exists until the member asks for one, and a membership check
    before the DM would spend an API call per verification on the members who
    never press it. The worker checks membership as the first thing it does
    when they DO press -- see vrc_group_inviter.send_group_invite.

    Silent on every "no", because there is no sentence worth sending about a
    button somebody was never going to be shown.

    A previous outcome is not one of those "no"s at all. A member who was
    invited and never accepted, who has since left the group, who was banned
    and then unbanned, or who switched group invites off and back on, is
    offered again on their next verification -- see group_invite_refusal,
    which is where the offer and the press part company, and
    GROUP_INVITE_FINAL_STATES, which is empty on purpose.
    """
    guild_id = str(guild.id)
    if premium is not None and not premium.allows(FEATURE_GROUP_INVITE):
        # Already resolved by the caller; skip the second entitlement read.
        return

    config = await group_invite_target(guild_id)
    if not config:
        return

    vrc_user_id = None
    try:
        vrc_user_id, verified = member_invite_identity(member.id)
    except Exception:
        logger.warning(
            "Could not read the VRChat id for member %s.", member.id, exc_info=True
        )
        return
    if not vrc_user_id:
        # Verified with no stored VRChat account. Nothing to invite, and no
        # sentence worth sending about it.
        return
    if not verified:
        # Belt and braces: this function is only ever called from assign_role's
        # 18+ branch, so reaching here means the stored verdict disagrees with
        # the one that just ran. Offering anyway would put a live button in the
        # DMs of somebody the database says is not 18+.
        logger.warning(
            "Declined to offer member %s a group invite in guild %s: the "
            "stored verification says they are not 18+.",
            member.id,
            guild_id,
        )
        return

    try:
        request = load_group_invite_request(guild_id, member.id)
    except Exception:
        logger.warning(
            "Could not read the invite standing for member %s in guild %s.",
            member.id,
            guild_id,
            exc_info=True,
        )
        return
    if group_invite_refusal(request, config["group_id"], for_offer=True) is not None:
        return

    # A previous outcome that is being offered again has to be cleared, or the
    # press would refuse the very button this DM is about: the press treats
    # every settled state as final, which is what keeps spent buttons dead.
    # Clearing here is what makes "a new verification is a new chance" real,
    # and it is the only place that distinction is drawn.
    if (
        request
        and request.get("group_id") == config["group_id"]
        and effective_group_invite_state(request) in GROUP_INVITE_REOFFERABLE_STATES
    ):
        if not retire_group_invite_request(guild_id, member.id):
            # Could not clear it, so the button would arrive dead. Better to
            # send nothing than a control that answers "you already had one".
            logger.warning(
                "Not re-offering member %s in guild %s: the spent request "
                "could not be cleared.",
                member.id,
                guild_id,
            )
            return

    locale_code = get_server_locale_code(guild_id, guild)
    try:
        await member.send(
            get_message(
                "dm_group_invite_offer",
                SimpleNamespace(locale=locale_code),
                server=guild.name,
                group=(config.get("group_name") or "their VRChat group"),
            ),
            view=GroupInviteOfferView(
                guild.id,
                group_invite_account_fingerprint(vrc_user_id),
                locale_code,
            ),
        )
    except discord.Forbidden:
        logger.warning(
            "Cannot DM user %s the group invite offer for guild %s.",
            member.id,
            guild_id,
        )
    except Exception:
        logger.exception("Unexpected error sending a group invite offer.")


async def handle_group_invite_press(
    interaction: discord.Interaction, guild_id: int, offered_account: str
) -> None:
    """The member pressed "send me an invite".

    Acknowledged FIRST, with the button removed, before anything slow happens.
    Discord gives an interaction three seconds, and the work below includes a
    blocking RabbitMQ publish -- but more importantly, a button that is still
    there while the publish runs is a button that can be pressed again.

    `offered_account` is the MEMBER's VRChat account, fingerprinted, as carried
    by the button that was pressed. Named at length because `account` further
    down is the invite bot's own account -- two different things one word
    apart, and getting them confused compiles perfectly and refuses every
    invite.
    """
    # Resolved from the id in the custom_id, NOT from interaction.guild --
    # this button only ever lives in a DM, where interaction.guild is always
    # None. Reading it there would name every server "this server".
    guild = bot.get_guild(guild_id)
    server_name = guild.name if guild else "this server"
    locale_code = get_server_locale_code(str(guild_id), guild)
    ctx = SimpleNamespace(locale=locale_code)

    async def settle(key: str, *, retryable: bool = False, **kwargs) -> None:
        # Reissued with the SAME fingerprint the press arrived with. A retry is
        # the same offer to the same account, not a fresh one -- rebuilding it
        # from whatever is linked now would let a transient failure launder a
        # stale button into a valid one for a different account.
        view = (
            GroupInviteOfferView(guild_id, offered_account, locale_code)
            if retryable
            else None
        )
        try:
            await interaction.edit_original_response(
                content=get_message(key, ctx, **kwargs), view=view
            )
        except discord.HTTPException:
            logger.warning(
                "Could not update the invite DM for guild %s.", guild_id, exc_info=True
            )

    try:
        await interaction.response.edit_message(
            content=get_message("group_invite_working", ctx), view=None
        )
    except discord.HTTPException:
        logger.warning(
            "Could not acknowledge an invite press for guild %s.",
            guild_id,
            exc_info=True,
        )
        return

    # Which account serves this guild. Resolved read-only: a member pressing a
    # button must never reserve capacity, and a guild that has lost its seat --
    # reclaimed after a long lapse, or on an account the operator retired --
    # has no invite to send until an admin sets it up again.
    account = invite_account_for_guild(str(guild_id))
    if account is None:
        await settle("group_invite_setup_problem", server=server_name)
        return

    config = await group_invite_target(str(guild_id))
    if not config:
        # Between the offer and the press, the server turned this off, let its
        # subscription lapse, or lost the group. Not the member's doing, and
        # not worth a retry button that would fail the same way.
        await settle("group_invite_setup_problem", server=server_name)
        return

    # Are they still in the server this offer came from?
    #
    # Checked HERE and not at the offer, because the offer is only ever made to
    # someone assign_role just verified. This DM, though, has no expiry and the
    # button routes for ever -- so without this, somebody who left the server,
    # or was kicked or banned from it, could press a months-old button and be
    # invited into that server's private VRChat group. The entire feature is
    # "members of this Discord get into this group", and leaving is the most
    # obvious way to stop being one.
    if guild is None:
        # NOT a reason to skip the check -- the strongest possible reason to
        # refuse. get_guild returns None when the bot has been removed from
        # the server, or has not received it over the gateway yet. In the first
        # case the server is gone and its members are not our business; in the
        # second we simply cannot tell. Skipping here let an ex-member of a
        # server the bot had been kicked out of press a months-old button and
        # be invited into that server's private group.
        await settle("group_invite_unavailable", retryable=True)
        return

    try:
        # fresh=True, at the cost of one REST call per press. The cached answer
        # is up to REST_TTL_SECONDS old and nothing invalidates it, so a member
        # who left -- or was kicked or BANNED -- read as present for three more
        # minutes. The verification that sent this button is itself what warms
        # that cache, so the stale window lined up exactly with the button
        # being in their hands. Presses are rare and already cost a VRChat
        # call; being right here is worth a REST call.
        still_a_member = await fetch_member_cached(
            guild, interaction.user.id, fresh=True
        )
    except discord.HTTPException:
        # Could not establish it either way. Refused rather than assumed:
        # the cost of a wrong "yes" is a stranger in somebody's private
        # group, and the cost of a wrong "no" is one retry.
        logger.warning(
            "Could not confirm guild membership before a group invite for "
            "guild %s.",
            guild_id,
            exc_info=True,
        )
        await settle("group_invite_unavailable", retryable=True)
        return
    if still_a_member is None:
        # Nothing recorded. If they rejoin and verify, they are offered
        # again from scratch, which is the right outcome.
        await settle("group_invite_not_a_member", server=server_name)
        return

    try:
        vrc_user_id, verified = member_invite_identity(interaction.user.id)
    except Exception:
        logger.warning(
            "Could not read the VRChat id for member %s on press.",
            interaction.user.id,
            exc_info=True,
        )
        await settle("group_invite_unavailable", retryable=True)
        return
    if not vrc_user_id:
        await settle("group_invite_unavailable")
        return

    # Is this member 18+ RIGHT NOW?
    #
    # Checked here for the same reason guild membership is, only more so: this
    # DM never expires and the button routes for ever, so an offer made to
    # somebody who was verified stays pressable long after a re-check has taken
    # their role away. Without this, failing a re-check and pressing the old
    # button was a way into a private group -- which inverts the one thing the
    # whole feature exists to guarantee.
    #
    # Never retryable. Their verification is the thing that is wrong, and no
    # amount of pressing this button will fix it; the sentence points them at
    # verifying again instead.
    if not verified:
        logger.warning(
            "Refused a group invite for member %s in guild %s: not verified 18+.",
            interaction.user.id,
            guild_id,
        )
        await settle("group_invite_not_verified", server=server_name)
        return

    # ...and is it still the same VRChat account the offer was made for?
    #
    # `verified` above is the verdict on whichever account is linked now, so it
    # alone would already refuse the account that caused this. This refuses the
    # wider case: any re-link at all invalidates the old button, so an invite
    # can only ever reach the account a completed verification actually put in
    # front of the group. Re-verifying issues a fresh button for the new
    # account, which is the supported way through.
    if group_invite_account_fingerprint(vrc_user_id) != offered_account:
        logger.warning(
            "Refused a group invite for member %s in guild %s: the linked "
            "VRChat account is not the one this offer was made for.",
            interaction.user.id,
            guild_id,
        )
        await settle("group_invite_account_changed", server=server_name)
        return

    group_name = config.get("group_name") or "the group"

    try:
        request = load_group_invite_request(str(guild_id), interaction.user.id)
    except Exception:
        logger.warning(
            "Could not read the invite standing for member %s on press.",
            interaction.user.id,
            exc_info=True,
        )
        await settle("group_invite_unavailable", retryable=True)
        return

    refusal = group_invite_refusal(request, config["group_id"])
    if refusal is not None:
        state = effective_group_invite_state(request)
        if refusal == INVITE_REFUSED_SETTLED:
            # They pressed a button that should not have been there -- an old
            # DM, most likely. Tell them where they actually stand.
            await settle(
                GROUP_INVITE_MESSAGE_KEYS.get(state, "group_invite_unavailable"),
                server=server_name,
                group=group_name,
            )
        else:
            # The button survives a cooldown refusal, and that is the whole
            # point of distinguishing the two.
            #
            # A transient failure hands the member their button back. Pressing
            # it inside GROUP_INVITE_COOLDOWN_SECONDS is refused -- so removing
            # it here would leave them with a message telling them to wait a
            # few minutes and nothing to wait FOR, short of verifying all over
            # again. A request genuinely in flight needs no button: its result
            # is already on its way to rewrite this message.
            await settle(
                "group_invite_too_soon",
                retryable=(refusal == INVITE_REFUSED_COOLDOWN),
            )
        return

    try:
        job = begin_group_invite(
            str(guild_id),
            interaction.user.id,
            group_id=config["group_id"],
            vrc_user_id=vrc_user_id,
            channel_id=interaction.channel_id,
            message_id=(interaction.message.id if interaction.message else None),
        )
    except Exception:
        logger.exception(
            "Could not record an invite request for member %s in guild %s.",
            interaction.user.id,
            guild_id,
        )
        await settle("group_invite_unavailable", retryable=True)
        return

    if job is None:
        # The row was claimed between the read above and this write -- a double
        # press that beat the button being removed.
        await settle("group_invite_too_soon")
        return

    loop = asyncio.get_running_loop()
    published = await loop.run_in_executor(
        None, publish_group_invite_job, job, account.queue
    )
    if not published:
        abandon_group_invite(str(guild_id), interaction.user.id, job["jobID"])
        await settle("group_invite_unavailable", retryable=True)
        return

    logger.info(
        "group-invite requested guild=%s member=%s group=%s job=%s",
        guild_id,
        interaction.user.id,
        job["groupID"],
        job["jobID"],
    )
    task = asyncio.create_task(
        expire_group_invite_if_unanswered(
            str(guild_id), interaction.user.id, job["jobID"]
        )
    )
    _group_invite_expiry_tasks.add(task)
    task.add_done_callback(_group_invite_expiry_tasks.discard)


async def expire_group_invite_if_unanswered(guild_id, discord_id, job_id) -> None:
    """Give up on one request that nothing ever answered, and say so.

    A per-request task, like assign_role's delayed role cleanup, rather than a
    registered background sweep -- there is nothing to sweep, only this one
    member's message to correct, and a sweep would be a scheduler that can fail
    quietly for a job that already knows when it is due.

    Its counterpart is effective_group_invite_state, which expires the same row
    on read. That one is what makes a bot restart mid-flight survivable: this
    task dies with the process, and the row then expires the next time anyone
    looks at it. The member's message stays wrong until they verify again,
    which is the honest cost of not running a scheduler for it.
    """
    try:
        await asyncio.sleep(GROUP_INVITE_TIMEOUT_SECONDS)
        now = datetime.now(timezone.utc)
        row = None
        with session_scope() as session:
            record = (
                session.query(GroupInviteRequest)
                .filter_by(
                    server_id=panel_view_key(guild_id),
                    discord_id=str(discord_id),
                    job_id=job_id,
                )
                .first()
            )
            if record is None or record.state != GROUP_INVITE_PENDING:
                return
            record.state = GROUP_INVITE_TIMED_OUT
            record.settled_at = now
            record.updated_at = now
            row = {
                "discord_id": record.discord_id,
                "vrc_user_id": record.vrc_user_id,
                "channel_id": record.channel_id,
                "message_id": record.message_id,
            }
        await tell_member_about_invite(guild_id, row, GROUP_INVITE_TIMED_OUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Could not expire an unanswered invite for guild %s.",
            guild_id,
            exc_info=True,
        )


async def assign_role(
    discord_id: str,
    is_18_plus: bool,
    guild_id: str,
    verification_code: str | None = None,  # no longer required for nickname logic
    display_name: str | None = None
):
    """
    Assigns or skips the 18+ role in one guild.
    Automatically updates the nickname if the server setting is on.
    """
    # Load server settings
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        role_id = server.role_id if server else None
        # Optional unverified role to remove on success
        unverified_role_id = getattr(server, "unverified_role_id", None) if server else None
        auto_nick = server.auto_nickname_change if server else False
        instr_locale = (server.instructions_locale if server and server.instructions_locale else None)
        custom_success_msg = (
            server.custom_verification_requested_message if server and server.custom_verification_requested_message else None
        )
        # Read in the session we already have open. Most guilds have no log
        # channel, and finding that out here means they never reach the
        # entitlement check below.
        log_row = (
            session.query(VerificationLogChannel)
            .filter_by(server_id=panel_view_key(guild_id))
            .first()
        )
        log_channel_id = log_row.channel_id if log_row else None

    guild = bot.get_guild(int(guild_id))
    if not guild:
        logger.warning(f"⚠️ Guild {guild_id} not found.")
        return

    member = await fetch_member_cached(guild, int(discord_id))
    if not member:
        logger.warning(f"⚠️ Member {discord_id} not in guild.")
        return

    if not role_id:
        logger.warning(f"⚠️ No verification role configured for guild {guild_id}.")
        return

    role = discord.utils.get(guild.roles, id=int(role_id))
    if not role:
        logger.warning(f"⚠️ Role ID {role_id} missing in guild {guild_id}.")
        return

    # Assign or notify
    if is_18_plus:
        # Resolved here rather than at the top of the function: every gated
        # feature below lives in this branch, and everything above can return
        # early. Resolving sooner meant a REST round-trip for members who had
        # left, guilds with no role configured, and — on every failed
        # verification — the not-18+ path, which uses none of these.
        #
        # Resolved once rather than per-feature, since three separate calls
        # would mean three entitlement reads per verification.
        premium = await resolve_premium_flags(guild_id)
        if not premium.allows(FEATURE_UNVERIFIED_ROLE_REMOVAL):
            unverified_role_id = None
        if not premium.allows(FEATURE_NICKNAME_SYNC):
            auto_nick = False
        if not premium.allows(FEATURE_CUSTOM_DM):
            # Falls through to the standard localized success DM below, so the
            # member still hears that they were verified.
            custom_success_msg = None

        # Reuses the flags already resolved above rather than asking again.
        loggable = log_channel_id if premium.allows(FEATURE_ACTIVITY_LOG) else None

        # 1) Add verified role first
        try:
            await member.add_roles(role)
            logger.info(f"Assigned role {role.name} to {member}.")
            queue_verification_log(
                guild_id, discord_id, LOG_OUTCOME_VERIFIED, loggable, instr_locale
            )
            if custom_success_msg:
                try:
                    await member.send(custom_success_msg)
                except discord.Forbidden:
                    logger.warning(f"⚠️ Cannot DM user {member.id} custom success message.")
            else:
                await dm_localized(member, guild, "dm_role_success", instr_locale, role=role.name, server=guild.name)
        except discord.Forbidden:
            logger.warning(f"Missing permission to add {role.name} in {guild_id}.")
            # The failure mode an admin would otherwise never learn about: the
            # member is told privately, and the server sees nothing at all.
            queue_verification_log(
                guild_id, discord_id, LOG_OUTCOME_ROLE_FAILED, loggable, instr_locale
            )
            await dm_role_assignment_failure(member, role, guild, instr_locale)

        # 2) Remove unverified role (if configured)
        unverified_role = None
        if unverified_role_id:
            unverified_role = discord.utils.get(guild.roles, id=int(unverified_role_id))
            if unverified_role and unverified_role in member.roles:
                try:
                    await member.remove_roles(unverified_role)
                    logger.info(f"Removed unverified role {unverified_role.name} from {member}.")
                except discord.Forbidden:
                    logger.warning(f"Missing permission to remove {unverified_role.name} in {guild_id}.")
                    await dm_localized(
                        member,
                        guild,
                        "dm_unverified_failed_bot_position",
                        instr_locale,
                        role=unverified_role.name,
                        server=guild.name
                    )


            # 3) Delayed re-check after 1s to catch race conditions with other bots
            async def _delayed_cleanup():
                try:
                    await asyncio.sleep(1)
                    try:
                        fresh_member = await guild.fetch_member(int(discord_id))
                    except Exception:
                        fresh_member = None
                    if fresh_member and unverified_role and unverified_role in fresh_member.roles:
                        try:
                            await fresh_member.remove_roles(unverified_role)
                            logger.info(f"(retry) Removed unverified role {unverified_role.name} from {fresh_member}.")
                        except discord.Forbidden:
                            logger.warning(f"Missing permission to remove {unverified_role.name} in {guild_id} on retry.")
                except Exception:
                    logger.warning("Delayed unverified role cleanup failed.", exc_info=True)

            if unverified_role is not None:
                asyncio.create_task(_delayed_cleanup())

        # Auto-nickname change if enabled
        safe_nick = discord_safe_nickname(display_name)
        if auto_nick and safe_nick:
            try:
                await member.edit(nick=safe_nick)
                logger.info(f"🔄 Updated nickname to {safe_nick} for {member}.")
                await dm_localized(member, guild, "nickname_updated", instr_locale, display_name=safe_nick)
            # Forbidden subclasses HTTPException; catching the parent also covers
            # a 400 from an unacceptable nickname. Letting that escape would skip
            # the milestone bookkeeping that runs after assign_role returns.
            except discord.HTTPException:
                logger.warning(f"Could not set nickname for {member}.", exc_info=True)
                await dm_localized(member, guild, "nickname_update_failed", instr_locale)

        # Last, and in its own DM. Every path that verifies somebody arrives
        # here -- a fresh verification, a re-check, pressing Begin Verification
        # while already verified, and auto-verify on join -- because all four
        # call assign_role, which is why the offer lives here rather than in
        # any one of them.
        #
        # Its own message rather than a button on the success DM above: that
        # DM can be a server's custom text, which an admin wrote without
        # knowing a button would be attached to it.
        #
        # Reuses the flags resolved at the top of this branch, so the offer
        # costs no extra entitlement read. offer_group_invite is silent for
        # everyone who should not see it.
        try:
            await offer_group_invite(member, guild, instr_locale, premium)
        except Exception:
            # Never let this take down a verification that has already
            # succeeded. The role is on, the DM has gone out, and the milestone
            # bookkeeping after this call still has to run.
            logger.exception(
                "Could not offer a group invite to %s in guild %s.",
                discord_id,
                guild_id,
            )
    else:
        # Not 18+. Resolved separately from the branch above, which never runs
        # here — and still short-circuits before the entitlement read when the
        # guild has no log channel configured.
        queue_verification_log(
            guild_id,
            discord_id,
            LOG_OUTCOME_NOT_18,
            await log_channel_if_allowed(guild_id, log_channel_id),
            instr_locale,
        )
        await dm_localized(member, guild, "not_18_plus", instr_locale)


# -------------------------------------------------------------------
# Modal: Collect VRChat Username
# -------------------------------------------------------------------
class VRCUsernameModal(discord.ui.Modal, title="Enter Your VRChat Profile URL or UserID"):
    vrc_username = discord.ui.TextInput(
        label="VRChat Profile URL or UserID",
        placeholder="https://vrchat.com/home/user/usr_1234d567-b12e-123d-a1c2-fd12345a67ea"
    )

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        # Accept a full profile URL or a raw usr_… ID; reject display names
        vrc_user_id = parse_vrc_user_input(self.vrc_username.value)
        if vrc_user_id is None:
            await interaction.response.send_message(
                get_message("invalid_vrc_id_input", interaction), ephemeral=True
            )
            return

        discord_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)

        # Check if this VRChat ID is already linked to a *different* Discord account
        with session_scope() as session:
            existing_user = session.query(User).filter_by(vrc_user_id=vrc_user_id).first()
            if existing_user and existing_user.discord_id != discord_id:
                # This VRChat profile is already registered to another Discord account
                # (you can localize this later if you want)
                await interaction.response.send_message(
                    get_message("vrc_id_already_linked", interaction),
                    ephemeral=True
                )
                return

            # If we reach here, either:
            #  - no one is using this vrc_user_id, or
            #  - it's the same Discord user (which shouldn't happen for this modal path, but is safe)

            verification_code = generate_verification_code()
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

            # Remove any old pending entry for this user/guild
            session.query(PendingVerification).filter_by(
                discord_id=discord_id,
                guild_id=guild_id
            ).delete()

            pending = PendingVerification(
                discord_id=discord_id,
                guild_id=guild_id,
                vrc_user_id=vrc_user_id,
                verification_code=verification_code,
                expires_at=expires_at
            )
            session.add(pending)

        view = VRCVerificationButton(vrc_user_id, verification_code, guild_id)
        # Use localized instruction strings for the numbered steps
        step1 = get_message("bio_verify_instructions1", interaction)
        step2 = get_message("bio_verify_instructions2", interaction)
        await interaction.response.send_message(
            f"✅ **VRChat userID saved!**\n\n"
            f"{step1}\n"
            f"```\n{verification_code}\n```\n"
            f"{step2}",
            view=view,
            ephemeral=True
        )


# -------------------------------------------------------------------
# Button: triggers code-based check
# -------------------------------------------------------------------
class VRCVerificationButton(discord.ui.View):
    def __init__(self, vrc_username: str, verification_code: str, guild_id: str):
        super().__init__(timeout=None)
        self.vrc_username = vrc_username
        self.verification_code = verification_code
        self.guild_id = guild_id

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        When pressed, we publish a request with verificationCode != None,
        meaning code-based flow.

        Always looks up the *current* pending code from the DB instead of
        trusting self.verification_code: /vrcverify replaces a user's pending
        row on every run, so an older instruction message's button would
        otherwise keep re-submitting a stale code that no longer matches
        what the user was told to put in their bio.
        """
        discord_id = str(interaction.user.id)

        flags = resolve_premium_flags_from_interaction(interaction)
        # Own scope: a prior re-check/nickname request must never block Verify.
        remaining = check_verification_cooldown(
            discord_id,
            window_seconds=flags.cooldown_window(),
            scope="verify",
        )
        if remaining:
            await interaction.response.send_message(
                get_message("cooldown_active", interaction, seconds=remaining),
                ephemeral=True,
            )
            return

        with session_scope() as session:
            pending = (
                session.query(PendingVerification)
                .filter_by(discord_id=discord_id, guild_id=self.guild_id)
                .first()
            )
            if not pending or datetime.now(timezone.utc) > pending.expires_at:
                await interaction.response.send_message(
                    get_message("verify_button_expired", interaction), ephemeral=True
                )
                return
            vrc_user_id = pending.vrc_user_id
            verification_code = pending.verification_code

        await interaction.response.defer(ephemeral=True)

        await publish_to_vrc_checker(
            discord_id,
            vrc_user_id,
            self.guild_id,
            verification_code,
            priority=flags.request_priority(),
        )

        await interaction.followup.send(
            get_message("verification_requested", interaction), ephemeral=True
        )


# -------------------------------------------------------------------
# Slash Command: /vrcverify
# -------------------------------------------------------------------
@app_commands.guild_only()
@bot.tree.command(name="vrcverify", description="Verify your VRChat 18+ status")
async def vrcverify(interaction: discord.Interaction):
    await process_verification(interaction)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_setup
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="vrcverify_setup",
    description="Admin command: Set or update the verified role for this server."
)
@app_commands.describe(
    verified_role="Role to assign to verified users (required)",
    unverified_role="Optional role to remove from users once verified"
)
@app_commands.rename(verified_role="verified-role", unverified_role="unverified-role")
async def vrcverify_setup(
    interaction: discord.Interaction,
    verified_role: discord.Role,
    unverified_role: Optional[discord.Role] = None
):
    """
    Inserts or updates a row in the 'servers' table with this server_id,
    storing the admin's user ID as 'owner_id' and the chosen role ID as 'role_id'.
    """
    guild_id = str(interaction.guild.id)
    owner_id = str(interaction.user.id)
    role_id_str = str(verified_role.id)

    with session_scope() as session:
        # See if we already have a row for this server_id
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server:
            # If no entry, create one
            server = Server(
                server_id=guild_id,
                owner_id=owner_id,
                role_id=role_id_str,
                unverified_role_id=(str(unverified_role.id) if unverified_role else None)
            )
            session.add(server)
            action = "created"
        else:
            # Update the existing row
            server.owner_id = owner_id  # optional, if you want to update the owner each time
            server.role_id = role_id_str
            # Only set if column exists; if migration failed, ignore silently
            try:
                server.unverified_role_id = (str(unverified_role.id) if unverified_role else None)
            except Exception:
                pass
            action = "updated"

        has_panel = bool(server.instructions_message_id)

    # A configured server with no panel is half-configured — members have no
    # button to click. Start the nudge clock so we can follow up if it stays
    # that way. Servers that already have a panel need neither.
    if not has_panel:
        record_guild_onboarding(guild_id)

    # Localized confirmation
    base = get_message(
        "setup_success",
        interaction,
        action=action,
        role=verified_role.name,
        role_id=verified_role.id
    )
    if unverified_role:
        extra_local = get_message(
            "setup_unverified_set",
            interaction,
            role=unverified_role.name,
            role_id=unverified_role.id
        )
    else:
        extra_local = get_message("setup_unverified_missing", interaction)
    panel_nudge = "" if has_panel else get_message("setup_panel_nudge", interaction)
    # The invite, at the one moment an admin has just proved they care whether
    # this bot works. Reuses support_invite_line rather than inventing a second
    # sentence saying the same thing -- #122 asks for that explicitly, and two
    # near-duplicate strings is twelve near-duplicate translations later.
    #
    # Empty when no invite is configured, so an unset SUPPORT_INVITE_URL leaves
    # this reply exactly as it was.
    invite = support_invite_url()
    invite_hint = (
        "\n\n" + get_message("support_invite_line", interaction, invite=invite)
        if invite
        else ""
    )
    donate_hint = get_message("setup_donate_hint", interaction, kofi_link=KOFI_URL)
    # This command survived the move to the dashboard because it is how a
    # server gets configured before anyone has heard of the website. Pointing
    # at the dashboard here is the introduction -- a link on the one reply
    # every new admin definitely reads, rather than an announcement nobody
    # sees. A link button rather than a URL in the text so it is the obvious
    # next thing on the screen.
    url = dashboard_guild_url(interaction.guild.id)
    extra = {"view": DashboardLinkView(url)} if url else {}
    # Donate hint stays last so it reads as a footer under everything else.
    await interaction.response.send_message(
        base + extra_local + panel_nudge + invite_hint + donate_hint,
        ephemeral=True,
        **extra,
    )


# -------------------------------------------------------------------
# Slash Command: /vrcverify_subscription
# -------------------------------------------------------------------
class PremiumUpgradeView(View):
    """Both ways to buy: Discord's own purchase button, and the website.

    Discord renders its button's label and the current price itself, so
    nothing here needs to know what the tier costs. The website button is a
    plain link and is added only when there is a URL to point it at.

    Two buttons rather than one because the two paths do not sell the same
    thing. Discord subscription SKUs bill monthly only -- there is no annual
    interval to configure -- so the 6- and 12-month plans exist on the website
    and nowhere else. An admin shown only Discord's button is not being
    offered a choice of checkout; they are being quietly denied the cheaper
    plans.
    """

    def __init__(self, subscription_url: Optional[str] = None):
        super().__init__(timeout=None)
        if PREMIUM_SKU_ID is not None:
            self.add_item(Button(sku_id=PREMIUM_SKU_ID))
        if subscription_url:
            self.add_item(
                Button(
                    label="More plans on the website",
                    style=discord.ButtonStyle.link,
                    url=subscription_url,
                )
            )


@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="vrcverify_subscription",
    description="Admin command: Get subscription info to unlock premium features."
)
async def vrcverify_subscription(interaction: discord.Interaction):
    """Show this server's premium status and, if it isn't subscribed, how to.

    Ko-fi deliberately does not appear here. Donations and the subscription are
    separate things, and mixing them in the one place people come to buy makes
    both read as optional. Ko-fi still has the instruction-panel button and the
    setup hint.
    """
    # Before the SKU exists there is nothing to sell, so this stays the honest
    # "it's free, tips welcome" message it has always been.
    if not PREMIUM_ENFORCED:
        await interaction.response.send_message(
            get_message("subscription_info", interaction, kofi_link=KOFI_URL),
            ephemeral=True,
        )
        return

    server_name = interaction.guild.name if interaction.guild else "this server"
    subscription_url = dashboard_subscription_url(interaction.guild_id)

    # The two sources, resolved separately and deliberately.
    #
    # Every other caller asks resolve_premium_flags_from_interaction, which
    # short-circuits on the Discord answer so an entitled guild never pays for
    # a Stripe lookup -- a property a test pins, because losing it would put a
    # database query behind every slash command. This command is the one place
    # that cannot use it. "Yes, you are subscribed" is not the whole answer
    # here: the message has to say where to go to change or cancel, and
    # telling a card subscriber to open Discord's User Settings sends them
    # looking for something that does not exist there.
    #
    # The cost is one extra cached lookup, on an admin command run rarely, and
    # it buys copy that is true. With STRIPE_ENABLED unset, stripe_active does
    # not query at all and every branch below collapses to what this command
    # did before Stripe existed.
    by_discord = premium_from_interaction(interaction)
    by_card = stripe_active(interaction.guild_id)

    if by_discord and by_card:
        # Paying twice. Never cancelled for them and never refunded
        # automatically -- code that moves money without a human deciding is
        # the wrong failure direction -- so this says so plainly and points at
        # the page that can cancel the half we control.
        logger.warning(
            "Guild %s holds both a Discord entitlement and a live card "
            "subscription; it is being billed twice.",
            interaction.guild_id,
        )
        message = get_message(
            "premium_status_active_both", interaction, server=server_name
        )
        extra = (
            {"view": DashboardLinkView(subscription_url, "Manage subscription")}
            if subscription_url
            else {}
        )
    elif by_card:
        message = get_message(
            "premium_status_active_card", interaction, server=server_name
        )
        extra = (
            {"view": DashboardLinkView(subscription_url, "Manage subscription")}
            if subscription_url
            else {}
        )
    elif by_discord:
        message = get_message("premium_status_active", interaction, server=server_name)
        # No purchase button: they already bought it. send_message() calls
        # view.is_finished(), so an absent view has to be MISSING, not None.
        extra = {}
    else:
        key = (
            "premium_status_grandfathered"
            if is_grandfathered(interaction.guild_id)
            else "premium_status_inactive"
        )
        message = get_message(key, interaction, server=server_name)
        extra = {"view": PremiumUpgradeView(subscription_url)}

    await interaction.response.send_message(message, ephemeral=True, **extra)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_support
# -------------------------------------------------------------------
@bot.tree.command(
    name="vrcverify_support",
    description="Get help with the VRChat 18+ verification process."
)
async def vrcverify_support(interaction: discord.Interaction):
    """Where to get help, plus the VRCVerify Discord if one is configured.

    The invite is appended rather than folded into `support_info`, for two
    reasons. It is optional -- an unset SUPPORT_INVITE_URL has to leave this
    reply exactly as it was, which a single string cannot do -- and the two
    sentences answer different questions: one is "I am stuck", the other is "I
    want to know what changed". Anyone can run this command, so the invite is
    the one part of #138 that is not admin-only, and that is deliberate: a
    member who finds the server is a member who can be told to ask their admin
    to follow the channel.
    """
    message = get_message("support_info", interaction)
    invite = support_invite_url()
    if invite:
        message = f"{message}\n\n" + get_message(
            "support_invite_line", interaction, invite=invite
        )
    await interaction.response.send_message(message, ephemeral=True)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_instructions
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="vrcverify_instructions",
    description="Admin only: Post instructions for using the verification bot."
)
async def vrcverify_instructions(interaction: discord.Interaction):
    # determine instructions locale (server setting overrides user locale)
    with session_scope() as session:
        srv = (
            session.query(Server).filter_by(server_id=str(interaction.guild.id)).first()
        )
        instr_locale = (
            str(srv.instructions_locale)
            if srv and srv.instructions_locale
            else get_locale(interaction)
        )
    # Same builder the refresh path uses, so a posted panel and a refreshed one
    # match. Styling comes from the interaction's own entitlements, which costs
    # nothing, rather than the REST lookup the refresh path has to use.
    branding = load_panel_branding(interaction.guild.id)
    if branding is BRANDING_UNREADABLE:
        # Nothing to preserve on a panel being posted for the first time, so
        # the default look is the right answer rather than a refusal.
        branding = None
    style_flags = resolve_premium_flags_from_interaction(interaction)
    panel_color, panel_icon = panel_style(
        branding, interaction.guild, style_flags.allows(FEATURE_BRANDED_PANEL)
    )
    embed = build_instructions_embed(instr_locale, panel_color, panel_icon)

    view = VRCVerifyInstructionView(locale=instr_locale)
    # An ordinary channel message, NOT this command's reply. A reply is owned by
    # a webhook, and Discord answers 200 to an embed edit on a webhook-owned
    # message and then keeps the old embed -- only the components change. Panels
    # posted as the reply could therefore never be restyled or translated again:
    # a language change came out as new button labels above the old text. The
    # refresh path is the whole point of tracking the panel, so the panel has to
    # be something the bot can actually edit.
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        await interaction.followup.send(
            "I can't post in this channel. Give VRCVerify **Send Messages** and "
            "**Embed Links** here, then run this again.",
            ephemeral=True,
        )
        return
    except Exception:
        logger.exception(
            "Failed to post the instructions panel for guild %s", interaction.guild.id
        )
        await interaction.followup.send(
            "Something went wrong posting the panel. Please try again shortly.",
            ephemeral=True,
        )
        return

    # Save the channel and message IDs to your database for reinitialization.
    guild_id = str(interaction.guild.id)
    channel_id = str(channel.id)
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server:
            # Posting the panel before running /vrcverify_setup used to drop the
            # ids on the floor: the panel went up but nothing tracked it, so the
            # startup refresh never saw it and /vrcverify_status would call it
            # missing. Create the row like the settings view does instead.
            server = Server(
                server_id=guild_id, owner_id=panel_row_owner_id(interaction.guild, interaction.user.id)
            )
            session.add(server)
        server.instructions_channel_id = channel_id
        server.instructions_message_id = str(message.id)

    # Posted with the current custom_ids already, so no restart needs to touch it.
    record_panel_view_version(guild_id)
    # Panel is up; retire any pending nudge for this guild.
    complete_guild_onboarding(guild_id)

    # Quiet, admin-only nudge after the public panel is posted
    await interaction.followup.send(
        get_message("setup_donate_hint", interaction, kofi_link=KOFI_URL).strip(),
        ephemeral=True,
    )


# -------------------------------------------------------------------
# Slash Command: /vrcverify_settings
# -------------------------------------------------------------------
@bot.tree.command(
    name="vrcverify_settings",
    description="Admin: See this server's settings and open the dashboard.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def vrcverify_settings(interaction: discord.Interaction):
    """Show what is stored, and where to change it.

    This used to be the editor. Editing moved to the dashboard, but the command
    stayed a read rather than becoming a bare link: an admin who only wanted to
    check which role is set should not have to open a browser and sign in to
    find out.
    """
    await send_settings_summary(interaction)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_status
# -------------------------------------------------------------------
def load_status_snapshot(guild_id: str):
    """Read everything /vrcverify_status needs in one session.

    Returns (role_id, panel_entry). `panel_entry` is None when no panel has
    ever been posted, otherwise it is shaped for probe_instruction_panel().
    """
    with session_scope() as session:
        srv = session.query(Server).filter_by(server_id=guild_id).first()
        if not srv:
            return None, None
        if not srv.instructions_message_id:
            return srv.role_id, None
        version_row = (
            session.query(InstructionPanelView)
            .filter_by(server_id=panel_view_key(srv.server_id))
            .first()
        )
        entry = {
            "server_id": srv.server_id,
            "channel_id": srv.instructions_channel_id,
            "message_id": srv.instructions_message_id,
            "locale": srv.instructions_locale or "en-US",
            "view_version": version_row.view_version if version_row else 0,
        }
        return srv.role_id, entry


# How each probe outcome reads to an admin. "gone" and the two malformed-id
# cases collapse together: from the admin's side all three mean "the saved
# panel isn't there any more, post a new one".
PANEL_STATUS_MESSAGE_KEYS = {
    "ok": "status_panel_ok",
    "gone": "status_panel_gone",
    "missing_ids": "status_panel_gone",
    "malformed": "status_panel_gone",
    "forbidden": "status_panel_unreachable",
    "archived": "status_panel_archived",
    "http_error": "status_panel_unreachable",
    "error": "status_panel_unreachable",
}


@bot.tree.command(
    name="vrcverify_status",
    description="Admin: Check whether this server's verification setup is healthy.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def vrcverify_status(interaction: discord.Interaction):
    """Report the two things that silently leave a server unable to verify:
    a missing/deleted verified role, and a missing or unreachable panel."""
    guild = interaction.guild
    guild_id = str(guild.id)

    # Each run edits the real, member-visible panel message, so repeated
    # invocations burn the per-channel edit budget. Same throttle the
    # verification actions use, on its own scope.
    #
    # Deliberately not shortened for premium: this one protects Discord's edit
    # budget, not the shared VRChat account, so paying for the tier is no
    # reason to let it be hit harder.
    remaining = check_verification_cooldown(str(interaction.user.id), scope="status")
    if remaining:
        await interaction.response.send_message(
            get_message("cooldown_active", interaction, seconds=remaining),
            ephemeral=True,
        )
        return

    # Probing the panel means a real message edit, which can outrun the 3s
    # window Discord gives us to answer an interaction.
    await interaction.response.defer(ephemeral=True)

    role_id, panel_entry = load_status_snapshot(guild_id)

    lines = [get_message("status_header", interaction, server=guild.name)]

    if not role_id:
        lines.append(get_message("status_role_missing", interaction))
    else:
        try:
            role = guild.get_role(int(role_id))
        except (TypeError, ValueError):
            role = None
        if role is None:
            lines.append(get_message("status_role_deleted", interaction))
        else:
            lines.append(get_message("status_role_ok", interaction, role=role.name))

    if panel_entry is None:
        panel_healthy = False
        lines.append(get_message("status_panel_missing", interaction))
    else:
        # Re-attaching the same view is idempotent, so using the refresh path
        # as the probe costs one API call and leaves the panel looking the
        # same. It does bump the message's edited timestamp, which is why this
        # command is throttled above.
        outcome = await probe_instruction_panel(panel_entry, rebuild_embed=False)
        panel_healthy = outcome == "ok"
        lines.append(
            get_message(
                PANEL_STATUS_MESSAGE_KEYS.get(outcome, "status_panel_unreachable"),
                interaction,
            )
        )

    if not panel_healthy:
        lines.append(get_message("status_tips", interaction))

    # The link belongs on the answer that gives an admin something to do. A
    # healthy server needs no next step, so it gets no button; an unhealthy one
    # needs the panel reposted or a role re-picked, and both of those live on
    # the dashboard.
    #
    # This used to be the other way round -- `panel_healthy` rather than `not`
    # -- which withheld the button precisely when it was worth offering. The
    # comment here argued the command must stay useful on a day the dashboard
    # itself is down, and it must: that is why the diagnosis is text and always
    # present. The button is additive. One that leads somewhere unreachable
    # costs an admin a click; withholding it while they work out where to go
    # costs considerably more.
    url = dashboard_guild_url(interaction.guild.id)
    extra = {"view": DashboardLinkView(url)} if url and not panel_healthy else {}
    await interaction.followup.send("\n".join(lines), ephemeral=True, **extra)


# -------------------------------------------------------------------
# The commands that used to edit settings
# -------------------------------------------------------------------
# All three now show the same read-only summary and link to the dashboard.
# They were kept rather than deleted outright: removing a slash command leaves
# admins typing a name Discord no longer offers and getting nothing back, with
# no clue where it went. A command that answers is the migration notice.
@bot.tree.command(
    name="vrcverify_logchannel",
    description="Admin: See this server's settings and open the dashboard.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def vrcverify_logchannel(interaction: discord.Interaction):
    """Formerly set the activity log channel; now shows it.

    The `channel` parameter is gone rather than kept-and-refused. A command
    that still accepts a channel and then declines to store it is worse than
    one that cannot accept it: Discord would offer the picker, the admin would
    choose, and only the reply would reveal nothing happened.

    Every check this used to perform -- announcement channels refused, the
    premium gate, the bot can actually post there -- moved with the write to
    write_dashboard_settings, which the website calls. None of them was lost.
    """
    await send_settings_summary(interaction)


@bot.tree.command(
    name="vrcverify_setrequestmessage",
    description="Admin: See this server's settings and open the dashboard.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def vrcverify_setrequestmessage(interaction: discord.Interaction):
    """Formerly a modal for the custom post-verification DM; now shows it.

    sanitize_custom_message did not go anywhere -- the dashboard write path
    calls it, so the zero-width stripping, the @everyone defusing and the
    https-only discord.com/vrchat.com allowlist still stand between an admin
    and what the bot will send on their behalf.
    """
    await send_settings_summary(interaction)


def publish_group_invite_job(job: dict, queue: Optional[str] = None) -> bool:
    """Put one job on one invite worker's queue. True if it was accepted.

    The queue names WHICH account does the work, and getting it wrong is not a
    performance problem -- it hands a job about group X to an account that has
    never joined group X. Callers pass the queue belonging to the guild's
    seat; the default is the single-account queue, which is what every
    installation with one account still uses.

    Blocking, so callers run it off the event loop. Declares the queue with
    `durable=True` and nothing else, matching vrc_group_inviter exactly -- an
    argument this end added would fail every declare with 406 and take the
    worker down with it.
    """
    queue = queue or RABBITMQ_GROUP_INVITE_QUEUE
    properties = pika.BasicProperties(
        content_type="application/json",
        delivery_mode=2,  # persistent
    )
    max_publish_tries = int(os.getenv("RABBITMQ_PUBLISH_TRIES", "3"))
    for attempt in range(1, max_publish_tries + 1):
        conn = None
        try:
            conn = _rabbitmq_connect_with_retry(max_tries=1)
            channel = conn.channel()
            channel.queue_declare(queue=queue, durable=True)
            channel.basic_publish(
                exchange="",
                routing_key=queue,
                body=json.dumps(job),
                properties=properties,
            )
            logger.info(
                "Sent %s job %s for guild %s to '%s'",
                job.get("type"),
                job.get("jobID"),
                job.get("guildID"),
                queue,
            )
            return True
        except AMQPError as e:
            if is_queue_argument_mismatch(e):
                # Retrying cannot help: the queue's arguments will not change
                # on their own, so every attempt fails identically.
                log_queue_argument_mismatch(queue)
                return False
            logger.warning(
                "Could not publish group-setup job (attempt %s/%s); retrying...",
                attempt,
                max_publish_tries,
                exc_info=True,
            )
            time.sleep(min(10.0, 1.5 * attempt))
        except Exception:
            logger.warning(
                "Unexpected failure publishing group-setup job (attempt %s/%s)",
                attempt,
                max_publish_tries,
                exc_info=True,
            )
            time.sleep(min(10.0, 1.5 * attempt))
        finally:
            try:
                if conn and conn.is_open:
                    conn.close()
            except Exception:
                pass
    logger.error("Group-setup job dropped after %s attempts", max_publish_tries)
    return False


async def request_group_verification(guild_id, actor_id):
    """Ask the invite worker to join this guild's group and report back.

    The dashboard's "check invite & join" button. Returns the re-read settings
    on success -- the same contract write_dashboard_settings has, so the page
    lands on what is actually stored -- None when the request could not be
    made at all, and raises SettingRejected for anything the caller got wrong.

    Nothing about the group comes from the caller. `begin_group_verification`
    builds the job from the stored row; this function only decides whether the
    guild is allowed to ask.
    """
    try:
        flags = await resolve_premium_flags(guild_id)
    except Exception:
        logger.warning(
            "Could not resolve premium flags while verifying the group for "
            "guild %s.",
            guild_id,
            exc_info=True,
        )
        return None
    if not flags.allows(FEATURE_GROUP_INVITE):
        raise SettingRejected("vrchat_group_id", "requires_premium", locked=True)

    # Assignment happens HERE rather than on the settings read, because this is
    # the first moment the guild commits to setup and the first moment a seat
    # has to be real. It is sticky, so pressing check twice does not move them.
    account = assign_invite_account(guild_id)
    if account is None:
        # Either no account is provisioned at all, or every one of them is
        # full. Both are the operator's problem rather than the admin's, and
        # both are a 503 rather than a refusal that blames the caller.
        logger.error(
            "A group verification was requested for guild %s but no invite "
            "account could be assigned.",
            guild_id,
        )
        return None

    try:
        job = begin_group_verification(guild_id)
    except Exception:
        logger.warning(
            "Could not start a group verification for guild %s.",
            guild_id,
            exc_info=True,
        )
        return None
    if job is None:
        raise SettingRejected("vrchat_group_id", "no_group_configured")

    loop = asyncio.get_running_loop()
    published = await loop.run_in_executor(
        None, publish_group_invite_job, job, account.queue
    )
    if not published:
        abandon_group_verification(
            guild_id,
            job["jobID"],
            "The bot could not reach the group-invite worker. Try again shortly.",
        )
    else:
        try:
            with session_scope() as session:
                # An action, like posting the panel -- so the pair is (what was
                # done, to which group) rather than (old value, new value).
                _record_dashboard_audit(
                    session,
                    guild_id,
                    actor_id,
                    [("group_verify", "requested", job["groupID"])],
                )
        except Exception:
            logger.warning(
                "Could not record the group verification request for guild %s.",
                guild_id,
                exc_info=True,
            )
        logger.info(
            "dashboard VERIFY-GROUP actor=%s guild=%s group=%s job=%s",
            actor_id,
            guild_id,
            job["groupID"],
            job["jobID"],
        )

    return await read_dashboard_settings(guild_id)


# -------------------------------------------------------------------
# RabbitMQ Consumer - handle group-invite results
# -------------------------------------------------------------------
async def consume_group_invite_results():
    """Drain the invite worker's result queue into group_invite_config.

    Its own consumer rather than a second binding on the verification results
    queue: the two carry different payloads, and one callback that had to
    guess which it was holding is a bug waiting for a payload that looks like
    both.
    """
    loop = asyncio.get_running_loop()

    def on_message(ch, method, properties, body):
        try:
            data = json.loads(body)
        except Exception:
            logger.exception(
                "Invalid JSON on the group-invite results queue; dropping"
            )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        asyncio.run_coroutine_threadsafe(handle_group_invite_result(data), loop)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def do_blocking_consume():
        while True:
            connection = None
            try:
                connection = _rabbitmq_connect_with_retry(max_tries=0)
                channel = connection.channel()
                channel.queue_declare(
                    queue=RABBITMQ_GROUP_INVITE_RESULT_QUEUE, durable=True
                )
                channel.basic_qos(prefetch_count=10)
                logger.info(
                    "Listening for group-invite results on '%s'...",
                    RABBITMQ_GROUP_INVITE_RESULT_QUEUE,
                )
                channel.basic_consume(
                    queue=RABBITMQ_GROUP_INVITE_RESULT_QUEUE,
                    on_message_callback=on_message,
                    auto_ack=False,
                )
                channel.start_consuming()
            except (
                pika.exceptions.AMQPConnectionError,
                pika.exceptions.StreamLostError,
                OSError,
            ):
                logger.warning(
                    "Group-invite results consumer disconnected; reconnecting "
                    "soon...",
                    exc_info=True,
                )
                time.sleep(3)
            except Exception:
                logger.exception(
                    "Unexpected error in the group-invite results consumer; "
                    "restarting"
                )
                time.sleep(3)
            finally:
                try:
                    if connection and connection.is_open:
                        connection.close()
                except Exception:
                    pass

    await loop.run_in_executor(None, do_blocking_consume)


async def handle_group_invite_result(data: dict):
    """One verdict from the invite worker.

    Both job types come back on this one queue, so the first thing to settle is
    which of the two this is. Routed on `type` rather than on which fields are
    present: the two payloads overlap in almost every field, and a callback
    that had to guess would guess wrong the first time somebody added a field.

    A payload with no `type` at all is treated as a setup verdict, because at
    the time this consumer shipped that was the only kind there was and one may
    still be in the queue across the upgrade.
    """
    if isinstance(data, dict) and data.get("type") == JOB_SEND_GROUP_INVITE:
        await handle_member_invite_result(data)
        return

    if isinstance(data, dict) and data.get("type") == JOB_LEAVE_GROUP:
        await handle_seat_release_result(data)
        return

    try:
        outcome = record_group_verification_result(data)
    except Exception:
        logger.exception(
            "Could not store a group-setup result for guild %s.",
            (data or {}).get("guildID"),
        )
        return
    logger.info(
        "group-setup result guild=%s job=%s state=%s -> %s",
        data.get("guildID"),
        data.get("jobID"),
        data.get("state"),
        outcome,
    )


async def handle_member_invite_result(data: dict):
    """One member's invite verdict: stored, then shown to them."""
    try:
        outcome, row = record_group_invite_result(data)
    except Exception:
        logger.exception(
            "Could not store a group-invite result for guild %s.",
            (data or {}).get("guildID"),
        )
        return

    logger.info(
        "group-invite result guild=%s job=%s state=%s -> %s",
        data.get("guildID"),
        data.get("jobID"),
        data.get("state"),
        outcome,
    )
    if outcome != "applied" or not row:
        return

    try:
        await tell_member_about_invite(data.get("guildID"), row, row["state"])
    except Exception:
        # The verdict is already stored and the queue message already acked.
        # An exception from here would travel into the concurrent.futures
        # Future that run_coroutine_threadsafe returns and that the consumer
        # discards -- which logs nothing at all. The member would be left
        # reading "asking VRChat..." for ever, on a row that is settled and so
        # is never offered again, with no trace anywhere of why.
        logger.exception(
            "Stored a group-invite verdict for guild %s but could not tell "
            "the member.",
            data.get("guildID"),
        )


async def tell_member_about_invite(guild_id, row: dict, state: str) -> None:
    """Replace the member's offer DM with what actually happened.

    Edited in place rather than sent as a second DM. The message they are
    looking at says "asking VRChat", and answering it somewhere else leaves
    that sentence standing for ever next to the real answer.

    The button comes back for anything that was not the member's own answer --
    VRChat unreachable, the server's permissions broken, the job lost. Those
    are not "no", and a member who hit one should not have to verify all over
    again to get another chance. GROUP_INVITE_COOLDOWN_SECONDS is what stops
    that becoming a retry loop.
    """
    guild = bot.get_guild(int(guild_id)) if guild_id else None
    locale_code = get_server_locale_code(str(guild_id) if guild_id else None, guild)
    ctx = SimpleNamespace(locale=locale_code)

    group_name = None
    try:
        config = load_group_invite_config(guild_id)
        group_name = (config or {}).get("group_name")
    except Exception:
        logger.warning(
            "Could not read the group name while reporting an invite for "
            "guild %s.",
            guild_id,
            exc_info=True,
        )

    text = get_message(
        GROUP_INVITE_MESSAGE_KEYS.get(state, "group_invite_unavailable"),
        ctx,
        server=(guild.name if guild else "this server"),
        group=(group_name or "the group"),
    )

    # Stamped with the account the request was MADE for, read off the stored
    # row rather than resolved fresh. A retry is the same offer to the same
    # account -- and if the member has re-linked since, the press refuses on
    # the fingerprint rather than inviting whoever is linked now.
    #
    # No account on the row means no button. That should not happen (every row
    # is written with one by begin_group_invite), but the alternative is a
    # ValueError out of DynamicItem for a custom_id that cannot match the
    # template, thrown while reporting an outcome on an already-settled row --
    # which the callers' catch-all would swallow, leaving the member reading
    # "asking VRChat" for ever with nothing left to answer them.
    view = None
    offered_account = row.get("vrc_user_id")
    if (
        state not in GROUP_INVITE_SETTLED_STATES
        and guild is not None
        and offered_account
    ):
        view = GroupInviteOfferView(
            guild.id, group_invite_account_fingerprint(offered_account), locale_code
        )

    channel_id = row.get("channel_id")
    message_id = row.get("message_id")
    if channel_id and message_id:
        try:
            # A partial messageable rather than a fetched channel: DM channels
            # are not in the bot's cache (member_cache_flags is none()), and
            # fetching one costs an API call to reach a message we already
            # know the id of.
            channel = bot.get_partial_messageable(
                int(channel_id), type=discord.ChannelType.private
            )
            await channel.get_partial_message(int(message_id)).edit(
                content=text, view=view
            )
            return
        except discord.NotFound:
            # They deleted the message. Nothing is left standing, so a fresh
            # DM is the only way they hear the answer -- fall through.
            pass
        except discord.HTTPException:
            # The original message is probably still standing, still reading
            # "asking VRChat...", so a fresh DM leaves that stale sentence
            # above the real answer. Sent anyway, deliberately.
            #
            # By the time this runs the row is SETTLED -- record_group_invite_
            # result stored the verdict before calling here. So there is no
            # retry and no future offer: staying quiet on a transient 429 or
            # 500 means this member never learns the outcome at all, and is
            # never offered again either. A confusing pair of messages is
            # recoverable; silence is not.
            logger.warning(
                "Could not edit the invite DM for guild %s; sending a new one.",
                guild_id,
                exc_info=True,
            )

    if guild is None:
        return
    try:
        member = await fetch_member_cached(guild, int(row["discord_id"]))
    except Exception:
        logger.warning(
            "Could not reach member %s to report a group invite.",
            row.get("discord_id"),
            exc_info=True,
        )
        return
    if not member:
        return
    try:
        await member.send(text, view=view)
    except discord.Forbidden:
        logger.warning(
            "Cannot DM user %s their group-invite outcome.", row["discord_id"]
        )
    except Exception:
        logger.exception("Unexpected error sending a group-invite outcome DM.")


# -------------------------------------------------------------------
# RabbitMQ Consumer - handle verification results
# -------------------------------------------------------------------
async def consume_results_queue():
    loop = asyncio.get_running_loop()

    def on_message(ch, method, properties, body):
        try:
            data = json.loads(body)
        except Exception:
            logger.exception("Invalid JSON in results queue; dropping message")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        asyncio.run_coroutine_threadsafe(handle_verification_result(data), loop)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def do_blocking_consume():
        import time

        while True:
            connection = None
            try:
                connection = _rabbitmq_connect_with_retry(max_tries=0)
                channel = connection.channel()
                channel.queue_declare(queue=RABBITMQ_RESULT_QUEUE, durable=True)
                channel.basic_qos(prefetch_count=10)
                logger.info("✅ Listening for verification results on '%s'...", RABBITMQ_RESULT_QUEUE)
                channel.basic_consume(
                    queue=RABBITMQ_RESULT_QUEUE,
                    on_message_callback=on_message,
                    auto_ack=False,
                )
                channel.start_consuming()
            except (pika.exceptions.AMQPConnectionError, pika.exceptions.StreamLostError, OSError):
                logger.warning("RabbitMQ consumer disconnected; reconnecting soon...", exc_info=True)
                time.sleep(3)
            except Exception:
                logger.exception("Unexpected error in RabbitMQ consumer; restarting consumer loop")
                time.sleep(3)
            finally:
                try:
                    if connection and connection.is_open:
                        connection.close()
                except Exception:
                    pass

    await loop.run_in_executor(None, do_blocking_consume)


async def handle_verification_result(data: dict):
    """
    Called when vrc_online_checker returns a result.
    Distinguishes code-based flow vs. no-code re-check.
    Also handles on-demand nickname updates.
    """
    try:
        logger.info(f"🔎 Received verification result: {data}")
        discord_id = data.get("discordID")
        guild_id = data.get("guildID")
        is_18_plus = data.get("is_18_plus", False)
        verification_code = data.get("verificationCode")   # None => re-check
        update_nick       = data.get("updateNickname", False)
        display_name      = data.get("display_name")
        lookup_ok         = data.get("lookup_ok", True)
        guild  = bot.get_guild(int(guild_id)) if guild_id else None
        member = await fetch_member_cached(guild, int(discord_id)) if guild and discord_id else None

        if not lookup_ok:
            locale_code = get_server_locale_code(guild_id, guild)
            issue_message = build_vrchat_issue_message(data, locale_code)
            logger.warning(
                "VRChat lookup/session failure for discord_id=%s guild_id=%s error_type=%s outage=%s confirmed=%s",
                discord_id,
                guild_id,
                data.get("error_type"),
                data.get("vrchat_outage"),
                data.get("vrchat_outage_confirmed"),
            )
            if member:
                try:
                    await member.send(issue_message)
                except discord.Forbidden:
                    logger.warning("⚠️ Cannot DM user about VRChat outage / API issue.")
            else:
                logger.warning("⚠️ Could not find guild member %s in guild %s to DM VRChat issue.", discord_id, guild_id)
            return

        # — On-demand nickname update flow —
        if update_nick:
            safe_nick = discord_safe_nickname(display_name)
            if member and safe_nick:
                # try to change nickname
                try:
                    await member.edit(nick=safe_nick)
                # Forbidden subclasses HTTPException; the parent also covers a
                # 400 from a nickname Discord won't accept.
                except discord.HTTPException:
                    logger.warning("Could not update nickname for %s.", discord_id, exc_info=True)
                    # Notify user on failure
                    try:
                        await member.send("⚠️ We could not update your username.")
                    except discord.Forbidden:
                        logger.warning("⚠️ Cannot DM user after nickname failure.")
                    return
                # Confirm on success
                try:
                    await member.send(f"✅ Your nickname has been updated to **{safe_nick}**.")
                except discord.Forbidden:
                    logger.warning("⚠️ Cannot DM user after nickname success.")
            return

        # — No-code re-check flow —
        if verification_code is None:
            with session_scope() as session:
                user = session.query(User).filter_by(discord_id=discord_id).first()
                if not user:
                    logger.warning(f"⚠️ No user row for {discord_id} in re-check.")
                    return
                user.verification_status = is_18_plus
                # preserve vrc_user_id if provided
                if data.get("vrcUserID"):
                    user.vrc_user_id = data["vrcUserID"]

            # Now assign role + maybe nickname
            await assign_role(discord_id, is_18_plus, guild_id, display_name=display_name)
            if is_18_plus:
                await record_guild_verification(guild_id, guild)
            return

        # — Code-based flow —
        now_utc = datetime.now(timezone.utc)
        with session_scope() as session:
            pending = (
                session.query(PendingVerification)
                .filter_by(
                    discord_id=discord_id,
                    guild_id=guild_id,
                    verification_code=verification_code
                )
                .first()
            )
            if not pending:
                logger.warning(f"⚠️ No pending verification for {discord_id}/{verification_code}.")
                return
            if now_utc > pending.expires_at:
                session.delete(pending)
                logger.warning(f"⚠️ Verification code expired for {discord_id}.")
                return
            if not data.get("code_found", False):
                # Don't delete the pending row here: the DM tells the user to
                # "try again", which means clicking the same Verify button.
                # That re-sends this same verification_code, so the pending
                # row needs to still exist for the retry to find a match.
                # It gets cleaned up by expiry (expired_pending_cleanup_task)
                # or replaced when they resubmit /vrcverify.
                guild  = bot.get_guild(int(guild_id))
                member = await fetch_member_cached(guild, int(discord_id)) if guild else None
                if member:
                    try:
                        await member.send(
                            get_message(
                                "code_not_found",
                                SimpleNamespace(locale=(getattr(guild, "preferred_locale", None) or "en-US"))
                            )
                        )
                    except discord.Forbidden:
                        logger.warning("⚠️ Cannot DM user about missing code.")
                return

            # Everything checks out — create/update user row
            user = session.query(User).filter_by(discord_id=discord_id).first()
            if not user:
                user = User(discord_id=discord_id)
                session.add(user)
                # First successful verification creates the user; set initial last attempt
                user.last_verification_attempt = datetime.now(timezone.utc)
            user.vrc_user_id = data["vrcUserID"]
            user.verification_status = is_18_plus
            session.delete(pending)

        # Assign role + maybe nickname
        await assign_role(discord_id, is_18_plus, guild_id, display_name=display_name)
        if is_18_plus:
            await record_guild_verification(guild_id, guild)

    except Exception:
        logger.error("❌ Exception in handle_verification_result", exc_info=True)


# -------------------------------------------------------------------
# Background cleanup: remove expired pending verifications
# -------------------------------------------------------------------
async def expired_pending_cleanup_task(interval_seconds: int = 60):
    """Periodically delete expired rows so they don't linger indefinitely."""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            with session_scope() as session:
                # Use synchronize_session=False for performance as we don't keep these in memory
                deleted = (
                    session.query(PendingVerification)
                    .filter(PendingVerification.expires_at < now_utc)
                    .delete(synchronize_session=False)
                )
            if deleted:
                logger.info(f"Removed {deleted} expired pending verification(s)")
        except Exception:
            logger.error("Exception during expired pending cleanup", exc_info=True)
        await asyncio.sleep(interval_seconds)


# -------------------------------------------------------------------
# Background nudge: servers that were set up but never posted a panel
# -------------------------------------------------------------------
def load_panel_nudge_candidates(limit: int):
    """Guilds past the grace period that still have no instruction panel.

    Only guilds with a guild_onboarding row can appear here, and that table is
    only written from this release onward — which is what keeps the nudge off
    servers that were configured long before it existed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PANEL_NUDGE_GRACE_HOURS)
    with session_scope() as session:
        rows = (
            session.query(GuildOnboarding)
            .filter(GuildOnboarding.panel_nudge_dm_sent == False)  # noqa: E712
            .filter(GuildOnboarding.setup_at <= cutoff)
            .order_by(GuildOnboarding.setup_at)
            .limit(limit)
            .all()
        )
        candidates = []
        for row in rows:
            srv = (
                session.query(Server)
                .filter_by(server_id=panel_view_key(row.server_id))
                .first()
            )
            # Rows we can never act on have to be retired here, not just
            # skipped. The LIMIT above is applied before this filtering, so a
            # skipped row comes back at the front of every future sweep (it is
            # the oldest) and permanently consumes one of the slots — enough of
            # them and real candidates behind them are never reached.
            if srv is None:
                # No config row to nudge about.
                session.delete(row)
                continue
            if srv.instructions_message_id:
                # Panel went up but the flag never got set (the helper that
                # sets it swallows its own errors). Retire it now.
                row.panel_nudge_dm_sent = True
                continue
            candidates.append({"server_id": row.server_id, "owner_id": srv.owner_id})
        return candidates


async def send_panel_nudge_dm(candidate) -> bool:
    """DM one guild's configuring admin about the missing panel."""
    server_id = candidate["server_id"]
    try:
        guild = bot.get_guild(int(server_id))
    except (TypeError, ValueError):
        guild = None

    if guild is None:
        # We're not in this guild any more; drop the row instead of retrying
        # forever, and don't burn the guild's one nudge on nobody.
        forget_guild_onboarding(server_id)
        return False

    # Mark before sending, exactly like the milestone DM: a delivery failure
    # must never turn into a repeat send.
    complete_guild_onboarding(server_id)

    member = await resolve_config_admin(guild, candidate["owner_id"])
    if member is None:
        return False

    await dm_localized(
        member,
        guild,
        "panel_nudge_dm",
        get_server_locale_code(server_id, guild),
        server=guild.name,
    )
    return True


async def panel_nudge_sweep_task(interval_seconds: int = PANEL_NUDGE_INTERVAL):
    """Periodically DM admins whose server never got an instruction panel.

    Capped per sweep and spaced between sends. A blast of DMs to unrelated
    servers is exactly the shape Discord's anti-spam heuristics look for, so a
    backlog, a clock jump, or a long outage has to trickle out over subsequent
    sweeps rather than going out all at once.
    """
    while True:
        try:
            candidates = load_panel_nudge_candidates(PANEL_NUDGE_MAX_PER_SWEEP)
            sent = 0
            for index, candidate in enumerate(candidates):
                if index:
                    await asyncio.sleep(PANEL_NUDGE_DM_SPACING)
                try:
                    if await send_panel_nudge_dm(candidate):
                        sent += 1
                except Exception:
                    logger.exception(
                        f"Failed to send panel nudge for guild {candidate['server_id']}"
                    )
            if candidates:
                logger.info(
                    f"Panel nudge sweep: DMed {sent}/{len(candidates)} eligible server(s)."
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Exception during panel nudge sweep", exc_info=True)
        await asyncio.sleep(interval_seconds)


# -------------------------------------------------------------------
# Background campaign: one-time premium cutover announcement
# -------------------------------------------------------------------
def load_premium_cutover_candidates(limit: int):
    """Grandfathered guilds that still owe a cutover DM.

    Driven off `servers` itself rather than a ledger of who *should* be told,
    with the notice table used only to exclude the already-told. That way the
    campaign has nothing to backfill before it can run, and the audience is
    exactly the set of servers the grandfather rule actually covers.

    The LIMIT lands after the not-yet-notified filter, so unlike the panel
    nudge there is no way for an unreachable row to sit at the front of every
    sweep eating a slot — send_premium_cutover_dm marks before it sends, which
    removes a row from this query whether the DM lands or not.

    Bounded by the captured grandfather line so the message only reaches the
    servers it is true for: it says nothing changes for you, which is only
    accurate for servers that predate the launch. An uncaptured line means the
    tier has never been switched on, so there is nothing to announce yet.
    """
    line = grandfather_line()
    if line is None:
        return []
    with session_scope() as session:
        notified = {
            panel_view_key(row.server_id)
            for row in session.query(PremiumCutoverNotice.server_id).all()
        }
        rows = (
            session.query(Server)
            .filter(Server.id <= line)
            .order_by(Server.id)
            .all()
        )
        candidates = []
        for srv in rows:
            if panel_view_key(srv.server_id) in notified:
                continue
            candidates.append(
                {"server_id": panel_view_key(srv.server_id), "owner_id": srv.owner_id}
            )
            if len(candidates) >= limit:
                break
        return candidates


def count_pending_cutover_notices() -> int:
    """How many grandfathered servers still owe a cutover DM.

    The same audience as load_premium_cutover_candidates, minus the limit —
    this answers "has the campaign finished?", so it has to count the exact
    set that campaign would send to.

    The "already notified" half is matched in Python, not with a SQL IN, and
    must stay that way: `servers.server_id` is an integer column on the
    deployed database while `premium_cutover_notice.server_id` really is text,
    so the comparison becomes `bigint = character varying` — which Postgres
    rejects outright. See panel_view_key. SQLite types both as text, so a SQL
    IN passes every test here and fails only in production.

    Returns 0 on error. This only drives a warning; a database hiccup at
    startup must not invent an alarming number, and a real backlog will still
    be reported on the next boot.
    """
    line = grandfather_line()
    if line is None:
        return 0
    try:
        with session_scope() as session:
            notified = {
                panel_view_key(row.server_id)
                for row in session.query(PremiumCutoverNotice.server_id).all()
            }
            rows = session.query(Server.server_id).filter(Server.id <= line).all()
            return sum(
                1 for row in rows if panel_view_key(row.server_id) not in notified
            )
    except Exception:
        logger.warning("Could not count pending cutover notices", exc_info=True)
        return 0


# on_ready fires on every reconnect, so anything called from it that is not
# idempotent has to say so itself — the same reasoning as start_background_task
# below. This is a reminder, not a correctness guard: re-announcing an
# outstanding campaign on every gateway blip is noise, and it would re-run two
# queries on the event loop each time.
_cutover_reminder_logged = False


def warn_if_cutover_incomplete() -> int:
    """Remind us, once, if the tier is live and servers still owe a DM.

    Purely a courtesy check now. Under the captured-line model nobody can lose
    a feature they already had, so an un-run campaign means existing servers
    were never told the tier exists — untidy, not harmful. It no longer guards
    a correctness property, and deliberately does not pretend to: what makes
    issue #59 impossible is PremiumGrandfatherLine, not this.

    Self-clearing: once every grandfathered server has its notice row this is
    silent forever. Servers added after launch are past the line and are never
    counted, so it cannot decay into a permanent warning.
    """
    global _cutover_reminder_logged
    if not PREMIUM_ENFORCED or _cutover_reminder_logged:
        return 0
    pending = count_pending_cutover_notices()
    # Set regardless of the outcome: the backlog can only shrink (the line is
    # fixed, so no new server can fall inside it), which makes a second look
    # pointless whether it found work or not. The cost of a database blip here
    # is one missed reminder in one process lifetime, and a restart re-checks.
    _cutover_reminder_logged = True
    if pending:
        logger.warning(
            "%d server(s) predating the premium tier have not had the cutover "
            "DM. They keep their grandfathered features either way; this is "
            "just to say they were never told. Trigger the campaign: touch %s",
            pending,
            PREMIUM_CUTOVER_TRIGGER_PATH,
        )
    return pending


def complete_premium_cutover(server_id) -> None:
    """Record that this guild has had its announcement, once and for all."""
    key = panel_view_key(server_id)
    try:
        with session_scope() as session:
            existing = (
                session.query(PremiumCutoverNotice).filter_by(server_id=key).first()
            )
            if existing is None:
                session.add(PremiumCutoverNotice(server_id=key))
    except Exception:
        logger.exception(f"Failed to mark cutover DM sent for guild {server_id}")


async def send_premium_cutover_dm(candidate) -> bool:
    """DM one guild's configuring admin about the premium cutover."""
    server_id = candidate["server_id"]
    try:
        guild = bot.get_guild(int(server_id))
    except (TypeError, ValueError):
        guild = None

    if guild is None:
        # Not in this guild any more. Retire it rather than retrying forever.
        # Grandfathering itself is unaffected: it is the server's row id, so
        # the deal survives a re-invite regardless of this ledger.
        complete_premium_cutover(server_id)
        return False

    # Mark before sending, exactly like the milestone and nudge DMs: this is a
    # one-shot announcement, and a delivery failure must never turn into a
    # second copy landing in someone's DMs.
    complete_premium_cutover(server_id)

    member = await resolve_config_admin(guild, candidate["owner_id"])
    if member is None:
        return False

    await dm_localized(
        member,
        guild,
        "premium_cutover_dm",
        get_server_locale_code(server_id, guild),
        server=guild.name,
    )
    return True


async def premium_cutover_sweep_task(interval_seconds: int = PREMIUM_CUTOVER_INTERVAL):
    """Trickle the cutover announcement out until every guild has had it.

    Exits once there is nothing left to send, so the campaign is genuinely
    one-shot rather than a loop that idles forever after it finishes.
    """
    logger.info("Premium cutover DM campaign started.")
    total = 0
    consecutive_failures = 0
    while True:
        try:
            candidates = load_premium_cutover_candidates(PREMIUM_CUTOVER_MAX_PER_SWEEP)
            if not candidates:
                logger.info(
                    "Premium cutover DM campaign finished; %s server(s) notified.", total
                )
                return
            sent = 0
            for index, candidate in enumerate(candidates):
                if index:
                    await asyncio.sleep(PREMIUM_CUTOVER_DM_SPACING)
                try:
                    if await send_premium_cutover_dm(candidate):
                        sent += 1
                except Exception:
                    logger.exception(
                        f"Failed to send cutover DM for guild {candidate['server_id']}"
                    )
            total += sent
            consecutive_failures = 0
            logger.info(
                f"Premium cutover sweep: DMed {sent}/{len(candidates)} server(s)."
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Give up rather than spin. The caller is the trigger watcher, and
            # it cannot get back to polling while this is still running — so an
            # unbounded retry here would wedge the watcher for the life of the
            # process on something like a DB outage. Every server already
            # reached stays marked, so re-touching the trigger resumes cleanly.
            consecutive_failures += 1
            logger.error(
                "Exception during premium cutover sweep (%s/%s)",
                consecutive_failures,
                PREMIUM_CUTOVER_MAX_FAILURES,
                exc_info=True,
            )
            if consecutive_failures >= PREMIUM_CUTOVER_MAX_FAILURES:
                logger.error(
                    "Premium cutover campaign giving up after %s consecutive "
                    "failures; %s server(s) notified. Re-create %s to resume.",
                    consecutive_failures,
                    total,
                    PREMIUM_CUTOVER_TRIGGER_PATH,
                )
                return
        await asyncio.sleep(interval_seconds)


async def watch_premium_cutover_trigger(
    path: str = PREMIUM_CUTOVER_TRIGGER_PATH, poll_interval: int = 30
):
    """Wait for the trigger file, then run the campaign exactly once.

    Deliberately not started from on_ready without the file: an announcement
    to the whole install base should go out because you decided it was time,
    not because a container restarted.

    That cuts both ways, and it is worth being plain about it. The trigger is
    removed *before* the campaign runs, so a crash or a redeploy part-way
    through leaves the remaining servers un-notified with nothing left to fire.
    This is not self-healing: re-create the trigger file to resume. Doing so is
    safe at any point, because every server already reached is marked in
    premium_cutover_notice and is skipped on the next pass.
    """
    logger.info(f"Premium cutover trigger watcher started (path={path})")
    while True:
        try:
            if os.path.exists(path):
                logger.info("Premium cutover trigger detected — starting campaign.")
                # Remove first. If the campaign raises partway, the marked rows
                # mean a rerun only picks up what is genuinely still owed, but
                # a trigger file left behind would restart it on every poll.
                try:
                    os.remove(path)
                except Exception:
                    logger.warning(
                        "Could not remove premium cutover trigger file; "
                        "manual cleanup may be required."
                    )
                try:
                    await premium_cutover_sweep_task()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Premium cutover campaign failed.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error in premium cutover trigger watcher.")
        await asyncio.sleep(poll_interval)


# Guards against a startup refresh and a trigger-file refresh overlapping.
instruction_refresh_lock = asyncio.Lock()


class RequestPacer:
    """Hands out evenly spaced start times to stay under a per-second ceiling.

    discord.py only reacts to 429s after the fact; this keeps the fleet refresh
    from provoking them in the first place.
    """

    def __init__(self, per_second: float):
        self.min_interval = 1.0 / per_second
        self.next_slot = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.min_interval
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)


def panel_row_owner_id(guild, actor_id) -> str:
    """`owner_id` for a servers row created by posting a panel.

    The guild's real owner when the bot can see one, not whoever clicked. That
    column feeds resolve_config_admin, which decides who gets configuration DMs
    — so filling it with the acting admin quietly appoints them, and on the
    dashboard that admin need not be the owner at all. Both panel paths use
    this, so the two cannot drift.

    Falls back to the actor only when the guild is not in cache, which is the
    same guess the code made unconditionally before, and still better than a
    NOT NULL violation that would lose the panel's ids.
    """
    owner_id = getattr(guild, "owner_id", None) if guild is not None else None
    return str(owner_id or actor_id)


def panel_view_key(server_id) -> str:
    """Normalise a guild id for the instruction_panel_views table.

    `servers.server_id` is declared String but can come back as an int, because
    the deployed column is an integer type and SQLAlchemy returns whatever the
    driver gives. instruction_panel_views.server_id really is text, so an
    un-normalised id makes Postgres reject `character varying = bigint` — and,
    worse, makes the in-memory version lookup silently never match.
    """
    return str(server_id)


def load_instruction_panel(guild_id) -> Optional[dict]:
    """One guild's saved panel, in the same shape load_instruction_panels uses."""
    try:
        with session_scope() as session:
            server = (
                session.query(Server)
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            if server is None or not server.instructions_message_id:
                return None
            # The real recorded version, not a placeholder: probe_instruction_panel
            # re-records it whenever the entry disagrees, so hardcoding 0 would
            # buy a redundant write on every restyle.
            recorded = (
                session.query(InstructionPanelView.view_version)
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            return {
                "server_id": server.server_id,
                "channel_id": server.instructions_channel_id,
                "message_id": server.instructions_message_id,
                "locale": server.instructions_locale or "en-US",
                "view_version": 0 if recorded is None else recorded.view_version,
            }
    except Exception:
        logger.warning(
            "Could not load the instruction panel for guild %s.",
            guild_id,
            exc_info=True,
        )
        return None


# asyncio only holds a weak reference to a running task, so a fire-and-forget
# create_task() can be garbage collected mid-flight and the panel edit silently
# never happens. Keeping the task here until it finishes is what stops that.
# Not start_background_task: that is keyed by name and these are per-guild.
_restyle_tasks: set = set()


def schedule_panel_restyle(guild_id) -> None:
    """Restyle a panel in the background, keeping the task alive until done."""
    coro = restyle_instruction_panel(guild_id)
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop (e.g. called from sync code). Close the coroutine we
        # never awaited, exactly as start_background_task does, so Python does
        # not warn about it. The next refresh picks the styling up anyway.
        coro.close()
        logger.debug("No event loop to restyle guild %s on.", guild_id)
        return
    _restyle_tasks.add(task)
    task.add_done_callback(_restyle_tasks.discard)


async def restyle_instruction_panel(guild_id) -> str:
    """Re-edit one guild's panel so a styling change is visible right away.

    The startup fleet refresh runs with rebuild_embed=False, so without this a
    colour change would not show up until an operator triggered a full refresh
    — potentially months. Called after an admin saves, and when a guild's
    entitlements change.

    Never raises: this is a nicety on top of a save that has already succeeded.
    """
    entry = load_instruction_panel(guild_id)
    if entry is None:
        return "no_panel"
    try:
        return await probe_instruction_panel(entry, rebuild_embed=True)
    except Exception:
        logger.warning(
            "Could not restyle the instruction panel for guild %s.",
            guild_id,
            exc_info=True,
        )
        return "error"


def load_instruction_panels(stale_only: bool = False):
    """Snapshot saved instruction panels from the DB.

    With `stale_only`, returns just the panels whose buttons predate the current
    INSTRUCTIONS_VIEW_VERSION — the ones a restart actually has to re-edit.
    Everything else is already routable through the persistent view.
    """
    with session_scope() as session:
        servers = (
            session.query(Server).filter(Server.instructions_message_id != None).all()
        )
        versions = {
            panel_view_key(row.server_id): row.view_version
            for row in session.query(InstructionPanelView).all()
        }
        panels = []
        for server in servers:
            version = versions.get(panel_view_key(server.server_id), 0)
            # Deliberately `==`, not `>=`: skip only panels matching this
            # process exactly. One recorded *newer* carries custom_ids we never
            # registered, so rolling a release back has to re-migrate it or its
            # buttons stay dead.
            if stale_only and version == INSTRUCTIONS_VIEW_VERSION:
                continue
            panels.append(
                {
                    "server_id": server.server_id,
                    "channel_id": server.instructions_channel_id,
                    "message_id": server.instructions_message_id,
                    "locale": server.instructions_locale or "en-US",
                    "view_version": version,
                }
            )
        return panels


def record_panel_view_version(server_id, version: int = INSTRUCTIONS_VIEW_VERSION):
    """Remember that this guild's panel now carries `version`'s custom_ids."""
    key = panel_view_key(server_id)
    try:
        with session_scope() as session:
            row = session.query(InstructionPanelView).filter_by(server_id=key).first()
            if row is None:
                row = InstructionPanelView(server_id=key)
                session.add(row)
            row.view_version = version
            row.updated_at = datetime.now(timezone.utc)
    except Exception:
        # Losing this only costs a redundant edit on the next boot.
        logger.exception(f"Failed to record panel view version for guild {server_id}")


def forget_panel_view_version(server_id):
    """Drop the recorded version so a re-posted panel is never wrongly skipped."""
    try:
        with session_scope() as session:
            session.query(InstructionPanelView).filter_by(
                server_id=panel_view_key(server_id)
            ).delete()
    except Exception:
        logger.exception(f"Failed to clear panel view version for guild {server_id}")


def record_guild_onboarding(server_id):
    """Start this guild's panel-nudge clock for the current gap.

    Callers only reach this when the guild has no panel, so a row that is
    already flagged as nudged means the last gap was closed and a *new* one has
    opened — the panel was posted and has since been deleted or lost. Reopen
    it: one nudge per gap, not one per server for all time. Otherwise the
    feature goes permanently quiet on exactly the degraded servers it exists
    to catch.

    Within a single open gap `setup_at` is never touched, so re-running
    /vrcverify_setup cannot keep pushing the deadline out — an admin who tweaks
    roles daily would otherwise never be nudged at all.
    """
    key = panel_view_key(server_id)
    try:
        with session_scope() as session:
            row = session.query(GuildOnboarding).filter_by(server_id=key).first()
            if row is None:
                session.add(GuildOnboarding(server_id=key))
            elif row.panel_nudge_dm_sent:
                # New gap: restart the grace period rather than firing on the
                # next sweep, so this behaves like a fresh setup.
                row.panel_nudge_dm_sent = False
                row.setup_at = datetime.now(timezone.utc)
    except Exception:
        # Bookkeeping only — never let this break the setup command.
        logger.exception(f"Failed to record onboarding for guild {server_id}")


def complete_guild_onboarding(server_id):
    """Retire the pending nudge — the panel is up, or the DM just went out."""
    key = panel_view_key(server_id)
    try:
        with session_scope() as session:
            row = session.query(GuildOnboarding).filter_by(server_id=key).first()
            if row is not None:
                row.panel_nudge_dm_sent = True
    except Exception:
        logger.exception(f"Failed to complete onboarding for guild {server_id}")


def forget_guild_onboarding(server_id):
    """Drop onboarding state for a guild the bot is no longer in."""
    try:
        with session_scope() as session:
            session.query(GuildOnboarding).filter_by(
                server_id=panel_view_key(server_id)
            ).delete()
    except Exception:
        logger.exception(f"Failed to clear onboarding for guild {server_id}")


def forget_instruction_panel(server_id: str):
    """Drop a saved panel reference whose channel or message no longer exists."""
    try:
        with session_scope() as session:
            srv = session.query(Server).filter_by(server_id=server_id).first()
            if srv:
                srv.instructions_channel_id = None
                srv.instructions_message_id = None
    except Exception:
        logger.exception(f"Failed to clear missing instruction panel for guild {server_id}")
    forget_panel_view_version(server_id)


def build_instructions_embed(
    locale: str,
    color: Optional[discord.Color] = None,
    icon_url: Optional[str] = None,
) -> Embed:
    """Build the localized instruction panel embed.

    Styling arrives as arguments rather than being looked up here, so this stays
    a pure function of its inputs: no database, no entitlement read, and no way
    for the posted panel and the refreshed panel to disagree. Both call sites
    resolve the style the same way and hand it in.

    The instruction copy itself is never customisable — see
    InstructionPanelBranding for why.
    """
    strings = localizations.get(locale, localizations["en-US"])
    embed = Embed(
        title=strings.get("instructions_title", ""),
        description=strings.get("instructions_desc", ""),
        color=color if color is not None else DEFAULT_PANEL_COLOR,
    )
    usage_example = "**Example Usage**:\n" "```bash\n" "/vrcverify\n" "```"
    embed.add_field(name="Example Command", value=usage_example, inline=False)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    return embed


async def probe_instruction_panel(
    entry, rebuild_embed: bool, already_checked: bool = False
) -> str:
    """Re-attach a fresh view (and optionally a rebuilt embed) to one saved panel.

    Uses a partial message so this costs exactly one API call and needs nothing
    in the gateway cache — no channel fetch, no message fetch.

    Returns why it went the way it did, not just whether it worked: the fleet
    refresh only needs the boolean, but /vrcverify_status has to tell an admin
    which of these it is. Editing the panel is the only honest test of whether
    the bot can still reach it, so the status command shares this exact path
    rather than guessing from cached permissions.
    """
    channel_id = entry.get("channel_id")
    message_id = entry.get("message_id")
    if not channel_id or not message_id:
        logger.warning(f"⚠️ Missing channel/message id for guild {entry['server_id']}; skipping.")
        return "missing_ids"

    try:
        messageable = bot.get_partial_messageable(int(channel_id))
        message = messageable.get_partial_message(int(message_id))
    except (TypeError, ValueError):
        logger.warning(f"⚠️ Malformed channel/message id for guild {entry['server_id']}; skipping.")
        return "malformed"

    try:
        # Built inside the try on purpose: a bad locale row must not escape and
        # abort the whole fleet pass, it only costs this one guild.
        payload = {"view": VRCVerifyInstructionView(locale=entry["locale"])}
        if rebuild_embed:
            # A panel posted as a slash-command reply belongs to a webhook, and
            # Discord answers 200 to an embed edit on one while keeping the old
            # embed. The view would still apply, so editing anyway is precisely
            # the half-update that hid this bug -- new button labels above text
            # nothing can change.
            #
            # It costs one fetch, on the paths that rebuild the embed. That is
            # the startup sweep's exemption (it passes rebuild_embed=False) but
            # NOT the manual whole-fleet refresh, which passes True and so pays
            # it per panel -- worth knowing before raising
            # INSTRUCTIONS_REFRESH_RATE, since the pacer reserves one slot per
            # panel and each now issues two calls.
            #
            # `already_checked` is how the dashboard's panel button avoids
            # asking twice; it has just asked in order to decide between
            # refreshing and replacing. A read that fails answers None and falls
            # through to the edit, because a fetch hiccup must not start
            # refusing ordinary refreshes.
            if not already_checked and await _panel_is_webhook_owned(
                messageable, message_id
            ):
                logger.warning(
                    "⚠️ The instructions panel for guild %s cannot be edited by "
                    "Discord (it was posted as a command reply). It has to be "
                    "replaced -- use the dashboard's panel button.",
                    entry["server_id"],
                )
                return "frozen"
            # Resolving the style is what reverts a lapsed server to the default
            # look, so it happens here rather than being cached with the panel.
            # Guilds with no branding row skip the entitlement read entirely.
            try:
                guild = bot.get_guild(int(entry["server_id"]))
            except (TypeError, ValueError):
                # An unparseable id costs the thumbnail, nothing else. The panel
                # still gets its rebuilt embed, and the existing malformed-id
                # handling above owns deciding what to do about the id itself.
                guild = None
            style = await resolve_panel_style(entry["server_id"], guild)
            # None means the branding table could not be read, so the embed
            # cannot be rebuilt without restyling a paying server over a
            # database hiccup. Editing anyway used to send the view on its own,
            # which produced the worst of the three outcomes: buttons in the new
            # language above an embed still in the old one, logged as a success.
            # Leave the whole panel alone instead. It stays internally
            # consistent, and the caller is told this is a retry rather than a
            # refusal.
            if style is None:
                logger.warning(
                    "⚠️ Could not resolve the panel style for guild %s; leaving "
                    "the panel untouched rather than half-updating it.",
                    entry["server_id"],
                )
                return "style_unreadable"
            payload["embed"] = build_instructions_embed(entry["locale"], *style)
        await message.edit(**payload)
        if entry.get("view_version") != INSTRUCTIONS_VIEW_VERSION:
            record_panel_view_version(entry["server_id"])
        # Says what went, not just that something did. A panel coming out in two
        # languages was invisible in the logs for exactly as long as this line
        # read "Refreshed" whether or not the embed was part of the edit.
        logger.debug(
            "Refreshed instructions message %s for guild %s (locale=%s, sent=%s)",
            message_id,
            entry["server_id"],
            entry["locale"],
            "+".join(sorted(payload)),
        )
        return "ok"
    except discord.NotFound:
        # Either the channel or the message is gone; both cases make the saved
        # reference useless, so stop retrying it on every restart.
        logger.warning(
            f"⚠️ Instruction panel {message_id} in channel {channel_id} not found (404) "
            f"for guild {entry['server_id']}; removing saved message reference."
        )
        forget_instruction_panel(entry["server_id"])
        return "gone"
    except discord.Forbidden:
        logger.warning(
            f"⚠️ No permission to edit instruction panel {message_id} in channel "
            f"{channel_id} for guild {entry['server_id']}; skipping."
        )
        return "forbidden"
    except discord.HTTPException as error:
        # Discord refused the edit for a reason that isn't ours to fix — most
        # often 50083, the panel lives in a thread that has since been archived.
        # The reference is still valid (unarchiving revives it), so keep it and
        # log a single line instead of a traceback per guild.
        logger.warning(
            f"⚠️ Discord rejected the instruction panel edit for guild "
            f"{entry['server_id']} (HTTP {error.status}, code {error.code}): {error.text}"
        )
        return "archived" if error.code == ARCHIVED_THREAD_ERROR_CODE else "http_error"
    except Exception:
        logger.exception(f"Failed to edit instructions message for guild {entry['server_id']}")
        return "error"


async def refresh_instruction_panel(entry, rebuild_embed: bool) -> bool:
    """Fleet-refresh wrapper: all the caller there needs is did-it-work."""
    return await probe_instruction_panel(entry, rebuild_embed) == "ok"


def partition_reachable_panels(panels):
    """Split saved panels into ones we can still edit and ones we cannot.

    A panel in a guild the bot has been removed from can never be edited: the
    edit comes back 403, and probe_instruction_panel deliberately keeps the
    saved reference on Forbidden because a revoked permission can be restored.
    A kick cannot, so without this those rows are retried on every single boot
    forever, and the count only grows as more servers remove the bot.

    The cost is not really the wasted calls, it is that a genuine permission
    problem in an active server ends up as one line among a hundred identical
    ones — the exact signal this refresh exists to produce.

    Skipping is deliberately non-destructive. If the guild cache were somehow
    cold, the worst case is that a refresh waits for the next boot.
    """
    reachable, departed = [], []
    for entry in panels:
        try:
            guild = bot.get_guild(int(entry["server_id"]))
        except (TypeError, ValueError):
            # An id we cannot parse is not evidence that we left the guild.
            # Let it through so probe_instruction_panel reports it as
            # malformed, rather than silently skipping it forever on a guess.
            reachable.append(entry)
            continue
        (reachable if guild is not None else departed).append(entry)
    return reachable, departed


async def refresh_all_instruction_panels(
    rebuild_embed: bool, reason: str, stale_only: bool = False
):
    """Refresh saved instruction panels concurrently, paced and bounded.

    Serialized behind a lock so a startup pass and a trigger-file pass can't
    fight over the same messages.
    """
    async with instruction_refresh_lock:
        panels, departed = partition_reachable_panels(
            load_instruction_panels(stale_only=stale_only)
        )
        departed_note = (
            f", {len(departed)} skipped (bot no longer in the guild)"
            if departed
            else ""
        )
        if not panels:
            logger.info(
                f"No instruction panels need refreshing ({reason}){departed_note}."
            )
            return

        started = time.monotonic()
        semaphore = asyncio.Semaphore(INSTRUCTIONS_REFRESH_CONCURRENCY)
        pacer = RequestPacer(INSTRUCTIONS_REFRESH_RATE)

        async def refresh_one(entry):
            # Pace before taking a slot so waiting tasks don't idle in the
            # semaphore and starve the ones ready to run.
            await pacer.wait()
            async with semaphore:
                return await refresh_instruction_panel(entry, rebuild_embed)

        # return_exceptions so one guild can never abort the pass: the startup
        # task is run_once, so an escaped error would strand every remaining
        # panel until the next process restart.
        results = await asyncio.gather(
            *(refresh_one(entry) for entry in panels), return_exceptions=True
        )

        updated = 0
        crashed = 0
        for result in results:
            if result is True:
                updated += 1
            elif isinstance(result, asyncio.CancelledError):
                raise result  # shutdown; don't report it as a per-panel failure
            elif isinstance(result, BaseException):
                crashed += 1
                logger.error("Instruction panel refresh worker crashed", exc_info=result)

        elapsed = time.monotonic() - started
        crashed_note = f", {crashed} crashed" if crashed else ""
        logger.info(
            f"Instruction panel refresh ({reason}): {updated}/{len(panels)} "
            f"updated in {elapsed:.1f}s{crashed_note}{departed_note}"
        )


async def update_all_instruction_messages():
    """Rebuild and edit saved instruction messages for all servers (uses DB-stored locale)."""
    await refresh_all_instruction_panels(rebuild_embed=True, reason="manual trigger")


async def watch_update_trigger_file(path: str = None, poll_interval: int = 5):
    """Poll for existence of `path`. When created, run update_all_instruction_messages() once and remove the file."""
    trigger_path = path or os.getenv("INSTRUCTIONS_TRIGGER_PATH", "/tmp/update_instructions.trigger")
    try:
        poll = int(os.getenv("INSTRUCTIONS_TRIGGER_POLL", str(poll_interval)))
    except Exception:
        poll = poll_interval

    logger.info(f"Instruction update trigger watcher started (path={trigger_path}, interval={poll}s)")
    while True:
        try:
            if os.path.exists(trigger_path):
                logger.info("Trigger file detected — updating all instruction messages.")
                try:
                    await update_all_instruction_messages()
                except Exception:
                    logger.exception("Failed to update all instruction messages.")
                # attempt to remove the trigger so it can be reused later
                try:
                    os.remove(trigger_path)
                except Exception:
                    logger.warning("Could not remove trigger file; manual cleanup may be required.")
        except Exception:
            logger.exception("Unexpected error in trigger watcher loop.")
        await asyncio.sleep(poll)


# -------------------------------------------------------------------
# Background task supervision
# -------------------------------------------------------------------
# on_ready fires again after every gateway reconnect, so anything it starts has
# to be idempotent. Without this, each reconnect leaked another RabbitMQ
# consumer (plus its executor thread), another cleanup loop, and another
# trigger-file watcher racing the others to os.remove() the same file.
background_tasks = {}


# -------------------------------------------------------------------
# Dashboard API (issue #65)
# -------------------------------------------------------------------
# The readers the web dashboard is allowed to reach, and nothing else. They
# take and return plain data so src/bot_api.py never has to touch a discord.py
# object, a session, or a model — see the module docstring there for why that
# separation is a security control rather than a style preference.
def dashboard_guild_present(guild_id) -> bool:
    """Is the bot in this guild? A cache lookup; never a REST call."""
    try:
        return bot.get_guild(int(guild_id)) is not None
    except (TypeError, ValueError):
        return False


async def dashboard_is_admin(guild_id, user_id) -> bool:
    """The authority check, answered by the bot and nobody else.

    Deliberately not derived from the `permissions` field Discord handed the
    dashboard at login: that describes the user's guilds as of their last OAuth
    round trip, and an admin demoted since then would keep working until their
    session expired.

    It also deliberately does NOT use fetch_member_cached. That cache serves
    the verification hot path at REST_TTL_SECONDS (180s by default), and
    reusing it here would mean a demoted admin — or a compromised account
    someone is busy locking out — kept dashboard access for up to three
    minutes. Revoking an admin role is the *first* thing anyone does during an
    incident, so that is exactly the window that must not be three minutes.
    BOT_API_ADMIN_TTL (15s) bounds it instead, and it is a separate cache so
    tuning one workload can never silently widen the other.

    Owner first because it needs no member at all — `guild.owner_id` is always
    cached, an owner always has Administrator, and it saves a REST call on the
    single most common case.
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False
        member_id = int(user_id)
        if member_id == guild.owner_id:
            return True

        key = (guild.id, member_id)
        cached = _admin_check_cache.get(key)
        if cached is not None:
            return bool(cached.allowed)

        # The gateway cache first: it is free, and GUILD_MEMBER_UPDATE keeps it
        # current. MemberCacheFlags is none() here so it usually misses, which
        # is why the fetch below is the path that normally answers.
        member = guild.get_member(member_id)
        if member is None:
            async with _rest_semaphore:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.NotFound:
                    member = None

        allowed = bool(member is not None and member.guild_permissions.administrator)
        # Only the verdict is cached, not the member. A stale Member object is
        # a permission answer waiting to be recomputed wrongly somewhere else.
        _admin_check_cache.set(key, SimpleNamespace(allowed=allowed))
        return allowed
    except Exception:
        # Fail closed. An unanswerable authority question is a "no".
        logger.warning(
            "Could not resolve dashboard admin rights for user %s in guild %s.",
            user_id,
            guild_id,
            exc_info=True,
        )
        return False


async def dashboard_admin_guilds(user_id, guild_ids) -> Optional[list]:
    """Narrow a caller's own guild list to the ones they administer.

    The picker's data source. Presence is checked first because it is free and
    because it throws away most of the work: an id the bot has never joined
    costs nothing and, importantly, is reported identically to a guild the
    caller simply does not administer. The caller cannot tell those two apart,
    which is the whole point — see handle_list_guilds in bot_api.py for what
    went wrong when this endpoint answered on presence alone.
    """
    try:
        member_id = int(user_id)
        candidates = []
        for guild_id in guild_ids:
            try:
                candidate = int(guild_id)
            except (TypeError, ValueError):
                continue
            if bot.get_guild(candidate) is not None:
                candidates.append(candidate)

        if not candidates:
            return []

        # Concurrently, but every fetch inside dashboard_is_admin still passes
        # through _rest_semaphore, so this cannot outrun the REST budget the
        # rest of the bot shares.
        verdicts = await asyncio.gather(
            *(dashboard_is_admin(candidate, member_id) for candidate in candidates),
            return_exceptions=True,
        )
        return [
            candidate
            for candidate, allowed in zip(candidates, verdicts)
            if allowed is True
        ]
    except Exception:
        logger.warning(
            "Could not resolve the dashboard guild list for user %s.",
            user_id,
            exc_info=True,
        )
        return None


async def read_dashboard_settings(guild_id) -> Optional[dict]:
    """Every setting in SETTINGS_FIELDS, with its plan state resolved.

    Returns None when the answer can't be trusted, which the API turns into a
    503 rather than into a page showing defaults an admin never chose.
    """
    try:
        flags = await resolve_premium_flags(guild_id)

        # Which of the two sources is live, resolved separately for this
        # payload only. The gate itself short-circuits on the Discord answer
        # and is right to -- it does not care *why* a server is premium. The
        # Subscriptions page does: without this it cannot tell "paying by card"
        # from "paying by card AND through Discord", and the double-billing
        # warning the whole feature promises would be unbuildable.
        #
        # Both reads are cache hits here, because resolve_premium_flags above
        # has just taken them, so this costs nothing on the hot path -- and
        # nothing at all when the tier is off.
        discord_premium = (
            await guild_has_premium(guild_id) if PREMIUM_ENFORCED else True
        )

        # Same guard /vrcverify_settings uses: the column post-dates some
        # deployments and a missing one must not break the whole read.
        has_auto_verify = server_has_column("auto_verify_new_members")

        with session_scope() as session:
            srv = (
                session.query(Server)
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            if srv is None:
                values = {
                    "role_id": None,
                    "unverified_role_id": None,
                    "auto_verify_new_members": True,
                    "auto_nickname_change": False,
                    "custom_verification_requested_message": None,
                    "instructions_locale": "en-US",
                }
            else:
                raw_auto_verify = getattr(srv, "auto_verify_new_members", None)
                values = {
                    "role_id": srv.role_id,
                    "unverified_role_id": getattr(srv, "unverified_role_id", None),
                    "auto_verify_new_members": (
                        True if raw_auto_verify is None else bool(raw_auto_verify)
                    ),
                    "auto_nickname_change": bool(srv.auto_nickname_change),
                    "custom_verification_requested_message": (
                        srv.custom_verification_requested_message
                    ),
                    "instructions_locale": srv.instructions_locale or "en-US",
                }

        branding = load_panel_branding(guild_id)
        if branding is BRANDING_UNREADABLE:
            # Showing "default blue, no icon" for a server that chose otherwise
            # would be a lie, and the step-6 write path would then save the lie
            # back. Refuse the read instead, exactly as restyle does.
            return None
        embed_color, show_icon = branding if isinstance(branding, tuple) else (None, False)
        values["panel_embed_color"] = embed_color
        values["panel_show_icon"] = bool(show_icon)
        values["verification_log_channel_id"] = load_log_channel_id(guild_id)

        # Raises rather than returning None on a database error, and that is
        # the point -- see load_group_invite_config. The except at the bottom
        # of this function turns it into "could not read", which the website
        # renders as an error instead of as an unconfigured group.
        group_invite = load_group_invite_config(guild_id) or {}
        # Which account THIS guild should invite -- their own if they hold a
        # seat, otherwise the one they would be assigned. Read-only: rendering
        # the settings page must never spend capacity.
        shown_account = invite_account_to_show(guild_id)
        values["vrchat_group_id"] = group_invite.get("group_id")
        values["vrchat_group_invite_enabled"] = bool(group_invite.get("enabled"))

        subscription = load_stripe_subscription(guild_id)
        if subscription is STRIPE_UNREADABLE:
            # Same refusal as unreadable branding, and for a sharper reason:
            # the Subscriptions page renders buy buttons from this block, and
            # "we could not read it" rendered as "not subscribed" is a page
            # inviting a paying customer to pay again.
            return None

        return {
            "guild_id": str(guild_id),
            "premium": {
                "enforced": PREMIUM_ENFORCED,
                "premium": flags.premium,
                "grandfathered": flags.grandfathered,
                # What the website may link to for an upgrade. Sent rather than
                # configured over there for the same reason the locale list is:
                # a second copy of the SKU id is a second thing to change, and
                # the failure mode is a purchase link pointing at the wrong
                # product. With the kill switch off this is None and the
                # dashboard offers no upgrade at all, which is correct -- there
                # is nothing to sell when every gate answers "allowed".
                "sku_id": str(PREMIUM_SKU_ID) if PREMIUM_SKU_ID is not None else None,
                # Whether the DISCORD half alone grants premium. Not a second
                # gate for the website to OR -- `premium` above stays the one
                # answer to "is this server premium". This exists so the
                # Subscriptions page can tell a card subscriber from someone
                # paying on both platforms, which is the difference between a
                # normal page and a "you are being billed twice" warning.
                "discord": discord_premium,
            },
            # Why the answer above is what it is, for the half of it Discord
            # cannot explain. `premium.premium` stays the single answer to "is
            # this server premium" -- this block never becomes a second thing
            # the website ORs together itself, or there would be two gates to
            # keep in step.
            #
            # `enabled` is the bot's own kill switch, sent for the same reason
            # sku_id is: the website must never offer a Buy button for a
            # purchase this process would refuse to record. With it false the
            # page shows the Discord path only.
            "stripe": {
                "enabled": STRIPE_ENABLED,
                "active": bool(subscription and subscription["active"]),
                "status": (subscription or {}).get("status"),
                "price_id": (subscription or {}).get("price_id"),
                "current_period_end": (subscription or {}).get("current_period_end"),
                "cancel_at_period_end": bool(
                    (subscription or {}).get("cancel_at_period_end")
                ),
                "customer_id": (subscription or {}).get("customer_id"),
                # More than one means the server is paying twice. The page
                # warns and links to the portal; nothing anywhere cancels or
                # refunds on anyone's behalf.
                "active_count": (subscription or {}).get("active_count", 0),
                # Whether a free trial may be offered. Decided here rather than
                # on the website for the same reason sku_id and the locale list
                # travel in this payload: a second copy of a rule is a second
                # thing to get wrong, and this one decides whether somebody is
                # charged for their first month. The website re-checks it
                # before creating the session -- a rendered card is not a gate,
                # because a POST is not a click.
                "trial_eligible": trial_eligible(guild_id),
            },
            # A column the deployed database is missing is reported as such
            # rather than silently rendered as a working toggle.
            "auto_verify_column_present": has_auto_verify,
            # The VRChat side of the group setup (issue #49): not settings an
            # admin typed, but what the invite worker last found when it
            # looked. Its own block rather than more `fields` entries, because
            # `fields` is the settings contract -- every entry there is
            # something with a plan gate that the website may try to save, and
            # none of this is writable by anybody.
            #
            # `claim_code` is in here rather than being generated by the
            # website: it is the proof that the admin controls the group, so
            # the side that will later check it is the side that has to mint
            # it.
            "group_invite": {
                # Expired on read, so a worker that never answered shows as
                # timed out rather than as a spinner nobody can clear.
                "state": effective_group_setup_state(group_invite),
                "error": group_invite.get("verify_error"),
                "group_name": group_invite.get("group_name"),
                "icon_url": group_invite.get("group_icon_url"),
                "can_invite": bool(group_invite.get("can_invite")),
                "can_see_members": bool(group_invite.get("can_see_members")),
                "claim_code": group_invite.get("claim_code"),
                # Two different accounts, and the difference matters. This one
                # is who the admin must invite RIGHT NOW, from this process's
                # configuration -- None means the operator has not provisioned
                # an invite account and the feature cannot be set up at all.
                "account_to_invite": (
                    shown_account.user_id if shown_account else None
                ),
                # ...and this one is who actually joined, recorded when a
                # verification succeeded. They differ the moment a second
                # invite account is added for capacity, and an admin looking at
                # a working group must not be told to invite somebody else.
                "joined_account": group_invite.get("invite_account_id"),
                "verified_at": (
                    group_invite["verified_at"].isoformat()
                    if group_invite.get("verified_at")
                    else None
                ),
                "requested_at": (
                    group_invite["verify_requested_at"].isoformat()
                    if group_invite.get("verify_requested_at")
                    else None
                ),
            },
            # The allowed values for anything the website renders as a choice.
            # Sent rather than duplicated over there: a dashboard building its
            # own language list could offer one the bot cannot render, and the
            # admin would only find out when the panel came out in English.
            "choices": {"instructions_locale": list(LANGUAGE_CODES)},
            "fields": {
                field.name: {
                    "value": values.get(field.name),
                    # Whether the save path accepts this field yet. The bot is
                    # the one phasing the write path in, so it is the one that
                    # says so -- the website drawing its own conclusion is how
                    # the two drift apart.
                    "writable": field.name in DASHBOARD_WRITABLE_FIELDS,
                    **field.state(flags),
                }
                for field in SETTINGS_FIELDS
            },
        }
    except Exception:
        logger.warning(
            "Could not read dashboard settings for guild %s.", guild_id, exc_info=True
        )
        return None


async def read_dashboard_roles(guild_id) -> Optional[list]:
    """This guild's roles, with whether the bot could actually grant each one.

    `assignable` is new information. Today an unassignable verified role is
    only discovered when assign_role catches a Forbidden mid-verification, by
    which point a member is already waiting — the point of putting it in the
    picker is that the admin finds out while choosing.

    It is None, not False, when `guild.me` is unavailable: "we cannot tell" and
    "we checked and no" are different answers, and greying out every role
    because the bot's own member object was missing would be the worse guess.
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return None
        me = guild.me
        top_role = me.top_role if me is not None else None

        roles = []
        for role in guild.roles:
            if role.is_default():
                continue  # @everyone: not a choice, never assignable
            if top_role is None:
                assignable = None
            else:
                # Managed roles belong to an integration and cannot be granted
                # by anyone, whatever the hierarchy says.
                assignable = bool(not role.managed and top_role > role)
            roles.append(
                {
                    "id": str(role.id),
                    "name": role.name,
                    "position": role.position,
                    "color": role.color.value,
                    "managed": bool(role.managed),
                    "assignable": assignable,
                }
            )
        roles.sort(key=lambda entry: entry["position"], reverse=True)
        return roles
    except Exception:
        logger.warning("Could not read roles for guild %s.", guild_id, exc_info=True)
        return None


async def read_dashboard_channels(guild_id) -> Optional[list]:
    """Text channels, flagged the way /vrcverify_logchannel judges them.

    `is_news` is surfaced because an announcement channel is refused outright
    for the verification log: other servers can *follow* it, which would
    republish an age disclosure about a named member into servers they have no
    relationship with.

    `can_send` and `can_embed` are separate because the two things this bot
    posts need different permissions. The verification log is plain text, so
    Send Messages is the whole requirement. The instructions panel is an embed,
    and a channel granting one permission but not the other looks perfectly
    writable right up until the panel is refused.
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return None
        me = guild.me

        channels = []
        for channel in guild.text_channels:
            if me is None:
                can_send = None
                can_embed = None
            else:
                perms = channel.permissions_for(me)
                can_send = bool(perms.view_channel and perms.send_messages)
                can_embed = bool(can_send and perms.embed_links)
            channels.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "category": channel.category.name if channel.category else None,
                    "position": channel.position,
                    "is_news": bool(channel.is_news()),
                    "can_send": can_send,
                    "can_embed": can_embed,
                }
            )
        channels.sort(key=lambda entry: entry["position"])
        return channels
    except Exception:
        logger.warning("Could not read channels for guild %s.", guild_id, exc_info=True)
        return None


async def read_dashboard_panel(guild_id) -> Optional[dict]:
    """Where this guild's instructions panel is, and whether it looks reachable.

    A read only. probe_instruction_panel() would answer more precisely but it
    *edits* the message to do it, and a page load must not rewrite a panel.

    Only the channel is checked, not the message: confirming the message still
    exists costs a REST call per page load. Note also that load_instruction_panel
    returns None both for "never posted" and for "the database could not be
    read", so `posted: false` is not proof no panel exists — whatever offers to
    repost a panel in step 6 has to confirm before it posts a duplicate.

    `channel_postable` answers one narrow question: could a NEW message go here.
    It is not "can this panel be kept alive", which is a different and weaker
    requirement — editing the bot's own message needs no Send Messages, and
    button clicks are interactions rather than messages. A panel sitting in a
    locked channel is the normal case, not a broken one.

    It includes Embed Links, because the panel IS an embed. A channel with Send
    Messages and no Embed Links reads as writable everywhere else and then
    refuses the panel, which is a confusing way to find that out.
    """
    try:
        entry = load_instruction_panel(guild_id)
        if entry is None or not entry.get("channel_id"):
            return {"posted": False}

        guild = bot.get_guild(int(guild_id))
        channel = None
        if guild is not None:
            try:
                channel = guild.get_channel_or_thread(int(entry["channel_id"]))
            except (TypeError, ValueError):
                channel = None

        postable = None
        if guild is not None and guild.me is not None and channel is not None:
            perms = channel.permissions_for(guild.me)
            postable = bool(
                perms.view_channel and perms.send_messages and perms.embed_links
            )

        return {
            "posted": True,
            "channel_id": str(entry["channel_id"]),
            "message_id": str(entry["message_id"]),
            "channel_name": getattr(channel, "name", None),
            "channel_exists": channel is not None,
            "channel_postable": postable,
            "locale": entry.get("locale", "en-US"),
        }
    except Exception:
        logger.warning("Could not read the panel for guild %s.", guild_id, exc_info=True)
        return None


def _stored_panel_location(guild_id):
    """This guild's recorded panel ids, distinguishing "none" from "unknown".

    load_instruction_panel returns None for both, which is fine for a refresh
    sweep and dangerous here: "no panel recorded" is the one condition under
    which posting a new one is safe, and a database hiccup must not be allowed
    to look like it. Raises rather than returning a sentinel, so a caller
    cannot forget to check.
    """
    with session_scope() as session:
        server = (
            session.query(Server)
            .filter_by(server_id=panel_view_key(guild_id))
            .first()
        )
        if server is None or not server.instructions_message_id:
            return None
        return {
            "server_id": server.server_id,
            "channel_id": server.instructions_channel_id,
            "message_id": server.instructions_message_id,
            "locale": server.instructions_locale or "en-US",
        }


async def _panel_is_webhook_owned(channel, message_id) -> Optional[bool]:
    """Whether this panel is a message the bot cannot fully edit.

    A panel posted as a slash-command reply belongs to a webhook. Discord takes
    an embed edit on one of those, answers 200, and keeps the old embed -- only
    the components change. So such a panel can never be restyled or translated,
    however many times it is refreshed, and the only repair is a replacement.

    None means the question could not be answered, which must never be read as
    "no": replacing a panel on a failed lookup is how a server ends up with two.
    A message that is simply gone answers False, because the ordinary refresh
    path already knows how to notice that and post afresh.
    """
    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return False
    except (TypeError, ValueError):
        return False
    except Exception:
        logger.warning(
            "Could not read panel message %s in guild's channel %s while "
            "checking whether it can be edited.",
            message_id,
            getattr(channel, "id", None),
            exc_info=True,
        )
        return None
    return message.webhook_id is not None


# One lock per guild that has ever used the panel button. The duplicate guard
# below reads the recorded location and then suspends three times before it
# writes a new one, so two overlapping requests -- a double click is enough --
# would both see "no panel here" and both post one. The whole promise of this
# function is that clicking twice cannot do that.
_panel_post_locks: dict[str, asyncio.Lock] = {}


def _panel_post_lock(guild_id) -> asyncio.Lock:
    key = panel_view_key(guild_id)
    lock = _panel_post_locks.get(key)
    if lock is None:
        # setdefault, not assignment: two coroutines reaching here in the same
        # tick must end up holding the same lock, or it guards nothing.
        lock = _panel_post_locks.setdefault(key, asyncio.Lock())
    return lock


async def post_dashboard_panel(guild_id, actor_id, channel_id):
    """Put this guild's instructions panel where the admin asked for it.

    Serialised per guild -- see `_panel_post_lock`. Everything below reads a
    recorded location and acts on it across several awaits, which is only safe
    if one request per guild is doing it at a time.
    """
    async with _panel_post_lock(guild_id):
        return await _post_dashboard_panel(guild_id, actor_id, channel_id)


async def _post_dashboard_panel(guild_id, actor_id, channel_id):
    """The body of the above, holding the guild's panel lock.

    The only thing the dashboard can make the bot *do* in a server, as opposed
    to store, so it is deliberately hard to do twice by accident:

    * A panel already in the requested channel is REFRESHED -- the same
      one-call edit the fleet sweep uses -- rather than posted again. A double
      click therefore costs an edit, not a second panel with live buttons that
      nothing tracks.
    * A panel recorded elsewhere is a MOVE. The new one is posted and the ids
      re-pointed, and the caller is told where the old one still is so an admin
      can remove it. Deleting it here would be this code destroying a message
      nobody pointed at.
    * If the recorded location cannot be read at all, nothing is posted. That
      is the case where "no panel exists" and "the database blinked" look
      identical, and guessing wrong leaves a duplicate.

    Returns a dict describing what happened, or None when it could not be done.
    """
    guild = bot.get_guild(int(guild_id))
    if guild is None or guild.me is None:
        return None

    channel = next(
        (c for c in guild.text_channels if str(c.id) == str(channel_id)), None
    )
    if channel is None:
        raise SettingRejected("panel_channel", "channel_not_in_guild")

    try:
        existing = _stored_panel_location(guild_id)
    except Exception:
        logger.warning(
            "Could not read the panel location for guild %s; not posting.",
            guild_id,
            exc_info=True,
        )
        return None

    # --- Already there: refresh in place, or replace it if editing cannot work ---
    # No permission precheck on this branch. Editing the bot's own message does
    # not need Send Messages, so a panel parked in a locked channel refreshes
    # fine -- the fleet sweep does exactly this on every restart without asking.
    # probe_instruction_panel's Forbidden branch is the honest answer, the same
    # reasoning its own docstring gives for /vrcverify_status.
    replacing = None
    if existing and str(existing.get("channel_id")) == str(channel.id):
        stuck = await _panel_is_webhook_owned(channel, existing.get("message_id"))
        if stuck is None:
            return None
        if stuck:
            # No edit will ever change this panel's text, so refreshing it would
            # be the silent no-op that hid this for so long. Post a replacement
            # and delete the original below. Cleared so the post path reads this
            # as a fresh post rather than a move to a different channel.
            replacing = existing
            existing = None
        else:
            # already_checked: `stuck` is False, so probe must not re-fetch the
            # message just to reach the same answer.
            outcome = await probe_instruction_panel(
                existing, rebuild_embed=True, already_checked=True
            )
            if outcome == "ok":
                _audit_panel(guild_id, actor_id, "refreshed", channel.id)
                return {"action": "refreshed", "channel_id": str(channel.id)}
            if outcome in {"gone", "missing_ids", "malformed"}:
                # The record points at a message that is not there any more, so
                # this is a first post rather than a duplicate.
                existing = None
            elif outcome == "forbidden":
                raise SettingRejected("panel_channel", "channel_not_writable")
            else:
                return None

    # --- Post a new one ---
    # Now it really is a send, so the send permissions are now the right test.
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise SettingRejected("panel_channel", "channel_not_writable")

    style = await resolve_panel_style(guild_id, guild)
    color, icon = style if style else (DEFAULT_PANEL_COLOR, None)
    # panel_view_key, like every other lookup in this function. This is the one
    # helper here that queries server_id un-normalised, and the deployed column
    # disagrees with its declared type -- see panel_view_key's docstring. A
    # mismatch there is swallowed, so the panel would silently go up in the
    # guild's Discord language and be rewritten to the configured one on the
    # next refresh.
    locale = get_server_locale_code(panel_view_key(guild_id), guild)
    embed = build_instructions_embed(locale, color, icon)

    try:
        message = await channel.send(
            embed=embed, view=VRCVerifyInstructionView(locale=locale)
        )
    except discord.Forbidden:
        raise SettingRejected("panel_channel", "channel_not_writable")
    except Exception:
        logger.exception("Failed to post the instructions panel for %s", guild_id)
        return None

    # A row with a message id but no channel id would otherwise report the
    # string "None" as the previous channel, and call this a move.
    previous = str(existing["channel_id"]) if existing and existing.get("channel_id") else None
    try:
        with session_scope() as session:
            server = (
                session.query(Server)
                .filter_by(server_id=panel_view_key(guild_id))
                .first()
            )
            if server is None:
                # Same reasoning as /vrcverify_instructions, and deliberately
                # the opposite of write_dashboard_settings, which refuses with
                # server_not_set_up rather than insert. The difference is not an
                # oversight: a settings save has nothing to record if it
                # refuses, while by this point a message is already live in
                # someone's server and something has to track it or the startup
                # refresh never sees it again. Mirroring the slash command is
                # also the rule -- /vrcverify_instructions inserts here too.
                server = Server(
                    server_id=str(guild_id),
                    owner_id=panel_row_owner_id(guild, actor_id),
                )
                session.add(server)
            server.instructions_channel_id = str(channel.id)
            server.instructions_message_id = str(message.id)
    except Exception:
        logger.exception("Posted a panel for %s but could not record it", guild_id)
        # Take it back down. An unrecorded panel has live verification buttons
        # and nothing that can ever refresh, restyle or find it again, and the
        # admin is about to see a failure and click again -- which would add
        # another. Deleting the bot's own just-sent message is the one cleanup
        # here that cannot destroy anything an admin put there.
        try:
            await message.delete()
        except Exception:
            logger.warning(
                "Could not remove the unrecorded panel %s in guild %s; it is "
                "live and untracked and has to be deleted by hand.",
                message.id,
                guild_id,
                exc_info=True,
            )
        return None

    record_panel_view_version(guild_id)
    complete_guild_onboarding(guild_id)

    # The replaced panel is deleted rather than left up, unlike the one a move
    # leaves behind. A move puts the new panel somewhere else, so the old one is
    # still the only panel in its own channel and removing it is the admin's
    # call; this one sits directly above its replacement in the same channel,
    # with live buttons and text nothing can ever correct.
    if replacing:
        try:
            await channel.get_partial_message(
                int(replacing["message_id"])
            ).delete()
        except Exception:
            logger.warning(
                "Replaced the panel for guild %s but could not delete the old "
                "message %s.",
                guild_id,
                replacing.get("message_id"),
                exc_info=True,
            )

    action = "replaced" if replacing else ("moved" if previous else "posted")
    _audit_panel(guild_id, actor_id, action, channel.id)
    return {
        "action": action,
        "channel_id": str(channel.id),
        "previous_channel_id": previous,
    }


def _audit_panel(guild_id, actor_id, action: str, channel_id) -> None:
    try:
        with session_scope() as session:
            _record_dashboard_audit(
                session,
                guild_id,
                actor_id,
                [("instructions_panel", action, str(channel_id))],
            )
    except Exception:
        logger.warning(
            "Could not record the panel action for guild %s.", guild_id, exc_info=True
        )


async def read_dashboard_audit(guild_id, limit: int = 25) -> Optional[list]:
    """This guild's recent settings changes, newest first.

    Only ever this guild's, and only the fields SETTINGS_FIELDS names -- an
    admin can see who changed their own server's configuration, which is what
    an audit trail is for, and nothing about any other server.

    Actor names are resolved from the gateway cache when the member is still
    around, and left as an id when they are not. No REST call: an admin who
    left is exactly the entry worth keeping, and paying a fetch per row to
    prettify history would make this the most expensive read on the page.
    """
    try:
        guild = bot.get_guild(int(guild_id))
        with session_scope() as session:
            rows = (
                session.query(DashboardAudit)
                .filter_by(server_id=panel_view_key(guild_id))
                .order_by(DashboardAudit.id.desc())
                .limit(max(1, min(int(limit), MAX_AUDIT_ROWS)))
                .all()
            )
            entries = []
            for row in rows:
                member = None
                if guild is not None:
                    try:
                        member = guild.get_member(int(row.actor_id))
                    except (TypeError, ValueError):
                        member = None
                entries.append(
                    {
                        "actor_id": row.actor_id,
                        "actor_name": getattr(member, "display_name", None),
                        "field": row.field,
                        "old_value": row.old_value,
                        "new_value": row.new_value,
                        "changed_at": (
                            row.changed_at.isoformat() if row.changed_at else None
                        ),
                    }
                )
            return entries
    except Exception:
        logger.warning(
            "Could not read the audit trail for guild %s.", guild_id, exc_info=True
        )
        return None


# The earliest day the rollup ever recorded, for anybody. Memoised because it
# is a scan of the whole table and it does not move: once a first row exists,
# the earliest day is fixed. Cached as None while the table is empty, which
# re-queries — an empty MIN() is instant, and the value must not stay None
# after the first verification lands.
_collecting_since: Optional[date] = None


def _collection_started() -> Optional[date]:
    """The earliest day any guild was counted on — a floor for what is knowable.

    Global, not per guild. Per guild would be the wrong question: `MIN(day)`
    for one server is the day it *first verified somebody*, so a guild whose
    first verification was yesterday would have its 30-day window reported as
    "no data", when the truthful answer is that the window is fully covered and
    the count is low. The table starting to exist is what bounds what can be
    known, and that happened once, for everyone.

    Note this is the first day anybody was counted, which is not quite the day
    collection *began* — a bot deployed on Monday whose first verification
    anywhere lands on Wednesday reports Wednesday, and windows reaching back to
    Monday or Tuesday are shown blank despite being fully covered (with zero in
    them). That is the safe direction to be wrong in: it under-claims, showing
    a blank where a truthful `0` was available, rather than presenting a number
    as measured when it was not. Recording a real start marker would fix it and
    costs another row to keep correct; the gap it closes is the handful of days
    between a deploy and the fleet's next verification.
    """
    global _collecting_since
    if _collecting_since is not None:
        return _collecting_since
    with session_scope() as session:
        _collecting_since = session.query(func.min(VerificationDaily.day)).scalar()
    return _collecting_since


DAILY_SERIES_DAYS = 30


def _verification_daily(guild_id, days: int = DAILY_SERIES_DAYS) -> list:
    """One entry per UTC day for the last `days`, oldest first.

    `[{"day": "2026-08-24", "count": 3}, ...]`, where **count is None for a day
    before collection started** and an integer for every day after it.

    THREE OUTCOMES, NOT TWO, and keeping them apart is the whole job:

      count: 3     that many verifications happened
      count: 0     the day was measured and nothing happened -- a real answer,
                   and usually the interesting one: a panel is up and nobody
                   is using it
      count: None  the day is before `_collection_started()`, so nothing was
                   measured and no number would be true

    The table cannot tell you this by itself. `VerificationDaily` writes no row
    for a day with no verifications -- deliberately, per its docstring, because
    writing zero-rows nightly would destroy the distinction and buy nothing --
    so absent and zero look identical *in the table*. The calendar is
    reconstructed here, and `_collection_started()` is what separates them:
    below that floor absent means unmeasured, above it absent means zero.

    Reuses `_collection_started()` rather than asking the same question a
    second way. Note it is global rather than per guild, for the reason given
    in its own docstring: a guild's own MIN(day) is the day it first verified
    somebody, which would report a brand-new server's fully-covered window as
    "no data".

    THIS IS STRICTLY MORE INFORMATIVE THAN THE 30-DAY WINDOW BESIDE IT.
    `_verification_windows` must blank `last_30_days` entirely when the window
    straddles the floor, because one number cannot be part-measured. A series
    can: the covered days carry counts and the rest carry None, so a server
    collecting for a week gets six real bars instead of one blank tile.

    One query, not thirty. Counts only -- nothing here identifies a member, and
    a series of daily totals cannot be turned back into one.
    """
    key = panel_view_key(guild_id)
    today = datetime.now(timezone.utc).date()
    first_day = today - timedelta(days=days - 1)
    started = _collection_started()

    rows = {}
    if started is not None:
        with session_scope() as session:
            rows = {
                row.day: int(row.count or 0)
                for row in session.query(VerificationDaily).filter(
                    VerificationDaily.server_id == key,
                    VerificationDaily.day >= first_day,
                    VerificationDaily.day <= today,
                )
            }

    series = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        # `started is None` means nothing has ever been counted anywhere, so
        # every day in the window is unmeasured rather than empty.
        measured = started is not None and day >= started
        series.append({
            "day": day.isoformat(),
            "count": rows.get(day, 0) if measured else None,
        })
    return series


def _verification_windows(guild_id) -> Optional[dict]:
    """Counts for today, 7 days and 30 days — or None if they can't be read.

    Each window is either an integer or None, and the difference is the whole
    point of this function. None means the window reaches back further than the
    rollup has been collecting, so no number would be true; the dashboard
    renders it blank. Zero means the window is fully covered and nothing
    happened in it, which is a real and useful answer and must never be
    flattened into the other one.

    "Today" rather than a rolling 24 hours, because a table of days cannot
    answer a rolling question — summing today and yesterday would double-count
    up to twice the real figure. The Overview labels it "Today (UTC)" so the
    number and its name agree.
    """
    key = panel_view_key(guild_id)
    today = datetime.now(timezone.utc).date()
    started = _collection_started()

    windows = {"today": 1, "last_7_days": 7, "last_30_days": 30}
    if started is None:
        # Nothing has ever been counted, so nothing can be claimed about any
        # window -- including today's.
        return {name: None for name in windows}

    counts = {}
    with session_scope() as session:
        for name, days in windows.items():
            first_day = today - timedelta(days=days - 1)
            if first_day < started:
                counts[name] = None
                continue
            total = (
                session.query(func.coalesce(func.sum(VerificationDaily.count), 0))
                .filter(
                    VerificationDaily.server_id == key,
                    VerificationDaily.day >= first_day,
                    VerificationDaily.day <= today,
                )
                .scalar()
            )
            counts[name] = int(total or 0)
    return counts


async def read_dashboard_overview(guild_id) -> Optional[dict]:
    """The Overview page: counts and configuration state for one guild.

    Counts only. Nothing here identifies a member, and nothing here is derived
    from a table that could — see `VerificationDaily` for why that ceiling is
    deliberate rather than provisional.

    Deliberately not answered here: how many members hold the verified role.
    The bot runs `MemberCacheFlags.none()` with `chunk_guilds_at_startup=False`,
    so `role.members` is empty and the only honest way to count it is to chunk
    the guild — a cost that scales with exactly the servers where the number
    would be most interesting. A tile that is sometimes wrong is worse than no
    tile, so there is no tile.

    Each part degrades on its own. A failed rollup read reports the counts as
    unknown while the rest of the page still renders, because "we could not
    check" and "nothing happened" must not look the same to an admin trying to
    work out whether verification is working.
    """
    try:
        guild = bot.get_guild(int(guild_id))
    except (TypeError, ValueError):
        guild = None
    if guild is None:
        # The API only routes here for a guild the bot is in, so this is the
        # gateway not being ready rather than a guild that does not exist.
        return None

    # `member_count` comes from GUILD_CREATE and stays current through the
    # gateway's member events. It needs no chunking and no REST call, which is
    # why it is the one population figure this page offers.
    member_count = guild.member_count

    total = None
    if server_has_column("verification_count"):
        try:
            with session_scope() as session:
                srv = (
                    session.query(Server)
                    .filter_by(server_id=panel_view_key(guild_id))
                    .first()
                )
                total = int(srv.verification_count or 0) if srv is not None else None
        except Exception:
            logger.warning(
                "Could not read the verification total for guild %s.",
                guild_id,
                exc_info=True,
            )

    try:
        windows = _verification_windows(guild_id)
        # Same read, same failure, so one `known` flag covers both rather than
        # the page having to reason about a series that loaded while the
        # totals beside it did not.
        daily = _verification_daily(guild_id)
        started = _collection_started()
        windows_known = True
    except Exception:
        logger.warning(
            "Could not read the verification rollup for guild %s.",
            guild_id,
            exc_info=True,
        )
        windows = {"today": None, "last_7_days": None, "last_30_days": None}
        # Not an empty list: nothing was read, and a chart drawn from [] would
        # be thirty days of confidently reported nothing.
        daily = None
        started = None
        windows_known = False

    settings = await read_dashboard_settings(guild_id)
    panel = await read_dashboard_panel(guild_id)

    return {
        "guild_id": str(guild_id),
        "member_count": member_count,
        # Straight from the settings read rather than resolved again here, so
        # the two pages can never disagree about whether a server has Premium.
        "premium": (settings or {}).get("premium") or {},
        "verifications": {
            # None when the deployment never ran the ALTER that added the
            # column. The page omits the tile rather than showing a zero it
            # cannot stand behind.
            "total": total,
            **windows,
            # Oldest first, one entry per day, count None where unmeasured.
            # None (rather than []) when the rollup could not be read at all.
            "daily": daily,
            "collecting_since": started.isoformat() if started else None,
            # False only when the rollup itself could not be read. Distinct
            # from a window being None, which is a successful read of a
            # question the data cannot answer yet.
            "known": windows_known,
        },
        "panel": panel,
        # Enough to tell an admin why nothing is happening, which is the most
        # common reason to open this page at all.
        "configured": _overview_configuration(settings, guild),
    }


def _overview_configuration(settings: Optional[dict], guild) -> Optional[dict]:
    """The handful of settings the Overview reports as set or not set.

    Booleans only, never the ids themselves. The Overview's job is to say
    whether verification is wired up; the Settings page is where the actual
    values live, and duplicating them here would be a second place for them to
    be wrong.

    `verified_role_exists` and `verified_role_assignable` are the health half
    of that same discipline: still booleans, still never an id, but "wired up"
    and "actually works" are different questions once a role can be deleted
    out from under a stored id, or the bot's own role can be moved below it.
    Both are None when the question doesn't apply yet -- no role_id set means
    there is nothing to check for existence, and no role (or no `guild.me`)
    means there is nothing to check for hierarchy -- so the caller can tell
    "not configured" apart from "configured and broken" apart from "cannot
    tell". Same `top_role > role` and `managed` check as `read_dashboard_roles`
    uses for `assignable`, run for one role instead of all of them, which is
    what keeps this cheap enough to run on every page load.
    """
    if not settings:
        return None
    fields = settings.get("fields") or {}

    def value(name):
        return (fields.get(name) or {}).get("value")

    role_id = value("role_id")
    role = None
    if role_id:
        try:
            role = guild.get_role(int(role_id))
        except (TypeError, ValueError):
            role = None

    role_assignable = None
    if role is not None:
        top_role = guild.me.top_role if guild.me is not None else None
        if top_role is not None:
            role_assignable = bool(not role.managed and top_role > role)

    return {
        "verified_role": bool(role_id),
        "verified_role_exists": (role is not None) if role_id else None,
        "verified_role_assignable": role_assignable,
        "unverified_role": bool(value("unverified_role_id")),
        "log_channel": bool(value("verification_log_channel_id")),
        "auto_verify": bool(value("auto_verify_new_members")),
    }


def _record_dashboard_audit(session, guild_id, actor_id, changed: list) -> None:
    """Append one row per field that actually moved.

    Only real changes are recorded. A form that posts every field on every save
    would otherwise bury the one line that matters under identical no-ops, and
    an audit trail nobody can read is not one.
    """
    key = panel_view_key(guild_id)
    for field, old, new in changed:
        session.add(
            DashboardAudit(
                server_id=key,
                actor_id=str(actor_id),
                field=field,
                old_value=None if old is None else str(old),
                new_value=None if new is None else str(new),
            )
        )


async def write_dashboard_settings(guild_id, actor_id, changes: dict):
    """Apply settings changes from the dashboard. The first write path here.

    Raises SettingRejected for anything the caller did wrong -- an unknown
    field, a field not yet open for writing, a bad value, or one their plan
    does not include. Returns None if the write could not be completed, which
    the API turns into a 503; returns the freshly re-read settings on success,
    so the page an admin lands on is what is actually stored rather than what
    they submitted.

    Everything is validated before anything is applied. A batch with one bad
    field changes nothing at all, rather than saving the first two and failing
    on the third.

    A save that changes something the panel displays also re-edits the panel,
    for the reason PANEL_VISIBLE_FIELDS gives: nothing else would.
    """
    if not isinstance(changes, dict) or not changes:
        raise SettingRejected("", "no_changes")

    # --- Validate the whole batch first ---
    coerced = {}
    for name, value in changes.items():
        if name not in SETTINGS_FIELDS_BY_NAME:
            raise SettingRejected(str(name), "unknown_field")
        if name not in DASHBOARD_WRITABLE_FIELDS:
            raise SettingRejected(name, "not_writable_yet")
        coerced[name] = SETTING_COERCERS[name](value)

    # --- The plan gate, decided here and never by the website ---
    try:
        flags = await resolve_premium_flags(guild_id)
    except Exception:
        logger.warning(
            "Could not resolve premium flags while saving guild %s.",
            guild_id,
            exc_info=True,
        )
        return None
    for name in coerced:
        field = SETTINGS_FIELDS_BY_NAME[name]
        state = field.state(flags)
        # Only write_locked fields are refused. The others save for anyone and
        # simply are not acted on, which is what /vrcverify_setup already does
        # -- see SettingsField.
        if state["locked"]:
            raise SettingRejected(name, "requires_premium", locked=True)

    # --- Roles have to name something that exists in THIS guild ---
    role_names = ROLE_FIELDS & set(coerced)
    if role_names:
        guild = bot.get_guild(int(guild_id))
        if guild is None or guild.me is None:
            # The guild was present when _authorize checked; if it is not
            # visible now, this is the bot failing rather than the caller
            # asking for something wrong.
            return None
        known = {str(role.id) for role in guild.roles if not role.is_default()}
        for name in role_names:
            wanted = coerced[name]
            if wanted is not None and wanted not in known:
                # Covers a deleted role, a role from another guild, and
                # @everyone -- which is excluded from `known` because assigning
                # it means nothing and read_dashboard_roles never offers it.
                raise SettingRejected(name, "role_not_in_guild")

    # --- The log channel has to be somewhere the bot may actually log ---
    for name in CHANNEL_FIELDS & set(coerced):
        wanted = coerced[name]
        if wanted is None:
            continue
        guild = bot.get_guild(int(guild_id))
        if guild is None or guild.me is None:
            return None
        channel = next(
            (c for c in guild.text_channels if str(c.id) == wanted), None
        )
        if channel is None:
            raise SettingRejected(name, "channel_not_in_guild")
        if channel.is_news():
            raise SettingRejected(name, "channel_is_announcement")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            raise SettingRejected(name, "channel_not_writable")

    # --- The VRChat group: read once here, applied further down ---
    #
    # Resolved during validation rather than at apply time so that a refused
    # claim changes nothing at all, which is what this function promises. Two
    # guilds claiming one group in the same instant still reach the UNIQUE
    # column, and one of them is refused there instead -- but that is a race,
    # not the ordinary case of an admin typing an id somebody else already has.
    #
    # It also means the read that feeds the read-modify-write happens before
    # any other table has been touched, so a database failure here cannot
    # leave half a batch applied.
    group_names = {"vrchat_group_id", "vrchat_group_invite_enabled"}
    group_plan = None
    if group_names & set(coerced):
        try:
            current_group = load_group_invite_config(guild_id) or {}
            claimed_by = (
                group_claim_holder(coerced["vrchat_group_id"])
                if coerced.get("vrchat_group_id")
                else None
            )
        except Exception:
            logger.warning(
                "Could not read the VRChat group config for guild %s.",
                guild_id,
                exc_info=True,
            )
            return None

        if claimed_by is not None and claimed_by != panel_view_key(guild_id):
            # Not "already in use by <server>": the holder is another
            # customer's guild id, and this admin has no business learning
            # which server it is. The dashboard says to contact support.
            raise SettingRejected("vrchat_group_id", "group_claimed_elsewhere")

        old_group = current_group.get("group_id")
        old_enabled = bool(current_group.get("enabled"))
        group_plan = {
            "old_group": old_group,
            "old_enabled": old_enabled,
            "new_group": coerced.get("vrchat_group_id", old_group),
            "new_enabled": bool(
                coerced.get("vrchat_group_invite_enabled", old_enabled)
            ),
        }

    # --- A column an older deployment is missing cannot be written ---
    if "auto_verify_new_members" in coerced and not server_has_column(
        "auto_verify_new_members"
    ):
        raise SettingRejected("auto_verify_new_members", "column_missing")

    try:
        changed = []

        # --- Panel branding: one row, so read-modify-write as a whole ---
        panel_names = {"panel_embed_color", "panel_show_icon"}
        if panel_names & set(coerced):
            branding = load_panel_branding(guild_id)
            if branding is BRANDING_UNREADABLE:
                # Writing a merged row on top of a value we could not read would
                # silently reset whichever half was not submitted.
                return None
            old_color, old_icon = branding if branding else (None, False)
            new_color = coerced.get("panel_embed_color", old_color)
            new_icon = coerced.get("panel_show_icon", old_icon)
            if "panel_embed_color" in coerced and new_color != old_color:
                changed.append(("panel_embed_color", old_color, new_color))
            if "panel_show_icon" in coerced and bool(new_icon) != bool(old_icon):
                changed.append(("panel_show_icon", old_icon, new_icon))
            save_panel_branding(guild_id, new_color, bool(new_icon))

        # --- The log channel has its own table, so its own write ---
        if "verification_log_channel_id" in coerced:
            new_channel = coerced["verification_log_channel_id"]
            old_channel = load_log_channel_id(guild_id)
            if (old_channel or None) != (new_channel or None):
                changed.append(
                    ("verification_log_channel_id", old_channel, new_channel)
                )
                set_log_channel(guild_id, new_channel)

        # --- The group config: one row, so written as a whole ---
        if group_plan is not None:
            if (
                "vrchat_group_id" in coerced
                and group_plan["new_group"] != group_plan["old_group"]
            ):
                changed.append(
                    (
                        "vrchat_group_id",
                        group_plan["old_group"],
                        group_plan["new_group"],
                    )
                )
            if (
                "vrchat_group_invite_enabled" in coerced
                and group_plan["new_enabled"] != group_plan["old_enabled"]
            ):
                changed.append(
                    (
                        "vrchat_group_invite_enabled",
                        group_plan["old_enabled"],
                        group_plan["new_enabled"],
                    )
                )
            displaced = save_group_invite_config(
                guild_id,
                group_id=group_plan["new_group"],
                enabled=group_plan["new_enabled"],
            )
            if displaced:
                # The admin pointed this server at a different group, or
                # cleared it. The bot is still sitting in the old one holding a
                # seat that nothing else will ever reclaim -- the lapse sweep
                # only looks at the group a guild currently has.
                #
                # The lease is deliberately NOT released here. It stays with
                # this guild, who is about to set up a new group on the same
                # account; what is being given back is the old GROUP, not the
                # seat.
                try:
                    await request_seat_release(
                        guild_id,
                        displaced,
                        invite_account_for_guild(guild_id),
                        RELEASE_REASON_DISPLACED,
                    )
                except Exception:
                    logger.warning(
                        "Could not ask the worker to leave %s after guild %s "
                        "changed its group.",
                        displaced,
                        guild_id,
                        exc_info=True,
                    )

        # --- Everything else lives on the servers row ---
        row_fields = {
            "instructions_locale": "en-US",
            "role_id": None,
            "unverified_role_id": None,
            "auto_verify_new_members": True,
            "auto_nickname_change": False,
            "custom_verification_requested_message": None,
        }
        wanted_on_row = {name: coerced[name] for name in row_fields if name in coerced}
        if wanted_on_row:
            with session_scope() as session:
                srv = (
                    session.query(Server)
                    .filter_by(server_id=panel_view_key(guild_id))
                    .first()
                )
                if srv is None:
                    # owner_id is NOT NULL and the dashboard has no honest value
                    # for it -- the acting admin is not necessarily the owner.
                    # Inserting a row here would also mint a fresh servers.id,
                    # placing the guild above the grandfather line as a side
                    # effect of changing a setting.
                    raise SettingRejected(
                        sorted(wanted_on_row)[0], "server_not_set_up"
                    )
                for name, new in wanted_on_row.items():
                    current = getattr(srv, name, None)
                    if name == "instructions_locale":
                        current = current or row_fields[name]
                    elif name == "auto_verify_new_members":
                        current = row_fields[name] if current is None else bool(current)
                    if current != new:
                        changed.append((name, current, new))
                        setattr(srv, name, new)

        if changed:
            with session_scope() as session:
                _record_dashboard_audit(session, guild_id, actor_id, changed)
            logger.info(
                "dashboard WRITE actor=%s guild=%s fields=%s",
                actor_id,
                guild_id,
                ",".join(name for name, _old, _new in changed),
            )
    except SettingRejected:
        raise
    except Exception:
        logger.warning(
            "Could not save dashboard settings for guild %s.", guild_id, exc_info=True
        )
        return None

    # The save is committed, so a panel still showing the old language or colour
    # is now merely stale -- and would stay that way, since the fleet sweep
    # rebuilds the view but not the embed. Only after the write, and never in a
    # way that can fail the save: restyle_instruction_panel swallows its own
    # errors and answers "no_panel" for the guilds that have none.
    panel_outcome = None
    if any(name in PANEL_VISIBLE_FIELDS for name, _old, _new in changed):
        panel_outcome = await restyle_instruction_panel(guild_id)

    settings = await read_dashboard_settings(guild_id)
    # The save succeeded either way -- this is about whether the admin gets to
    # know the panel did not follow it. "frozen" is the case that cost a long
    # debugging session in production: the setting stored, the page showed the
    # new value, and the panel stayed as it was with nothing saying so.
    if settings is not None and panel_outcome in PANEL_STALE_OUTCOMES:
        settings["panel_stale"] = panel_outcome
    return settings


# The actor recorded against every Stripe-driven change. A fixed, non-human,
# obviously-not-a-snowflake string, so the audit trail can never attribute a
# webhook to a person — and so a reader scanning the table can tell machine
# writes from admin writes at a glance.
STRIPE_AUDIT_ACTOR = "system:stripe"


def _parse_stripe_timestamp(raw) -> Optional[datetime]:
    """One ISO-8601 instant from the normalised payload, as aware UTC.

    Returns None for anything unparseable rather than raising, so the caller
    decides what a missing timestamp means — which differs between the two
    fields that use this.
    """
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            # Python's parser rejects the trailing "Z" Stripe and JSON both use.
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prune_stripe_events(session, now: datetime) -> None:
    """Forget event ids too old to be replayed at us.

    Called by the one function that inserts them, on the same transaction —
    the A-24 pattern. The only way to add a row here is also the thing that
    removes them, so this cannot become another `purge_expired` that exists and
    is never called, and nothing needs a scheduler to stay correct.

    Deliberately not allowed to fail the write it rides along with: losing a
    subscription event because a housekeeping DELETE went wrong would be a far
    worse trade than a table that stays large for another day.
    """
    try:
        cutoff = now - timedelta(days=STRIPE_EVENT_RETENTION_DAYS)
        session.query(StripeEvent).filter(StripeEvent.received_at < cutoff).delete(
            synchronize_session=False
        )
    except Exception:
        logger.warning("Could not prune the Stripe event ledger.", exc_info=True)


def _stripe_event_recorded(event_id: str) -> bool:
    """Has this event id already been written? Used only to read an IntegrityError.

    False on a failed read, which sends the caller down the "retry" path rather
    than the "already done" one. Being wrong in that direction costs a
    redelivery; being wrong the other way loses a subscription event.
    """
    try:
        with session_scope() as session:
            return (
                session.query(StripeEvent.event_id)
                .filter_by(event_id=event_id)
                .first()
                is not None
            )
    except Exception:
        logger.warning(
            "Could not check whether Stripe event %s was already recorded.",
            event_id,
            exc_info=True,
        )
        return False


def _stripe_audit_value(row) -> str:
    """One subscription's meaningful state, as the audit trail records it."""
    period_end = row.current_period_end
    if isinstance(period_end, datetime) and period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    return (
        f"{row.status} {row.stripe_subscription_id} {row.price_id} "
        f"ends={period_end.isoformat() if period_end else 'unknown'} "
        f"cancel_at_period_end={bool(row.cancel_at_period_end)}"
    )


async def write_dashboard_stripe_subscription(guild_id, payload: dict):
    """Mirror one Stripe subscription event into the database.

    The only caller is the bot API's system route, which the dashboard reaches
    after verifying a webhook's signature. Nothing here talks to Stripe: this
    process holds no Stripe credential and never will, so everything it knows
    arrives in `payload`, already normalised on the other side of the wire.

    Three guards, in this order, and all three matter:

    1. **Idempotency.** The event id is inserted first, in the same transaction
       as the change. Stripe retries for up to three days, so duplicates are
       expected traffic — and putting the insert in the same transaction means
       a failure part-way through rolls back the ledger entry too, rather than
       marking an event processed that never was.
    2. **Ordering, per subscription.** Stripe does not promise events arrive in
       order. An event no newer than the one already applied to *that
       subscription* is recorded and dropped, because a delayed
       `subscription.updated` overwriting a newer `subscription.deleted` would
       silently restore premium to a server that cancelled.

       **A constraint on whoever builds the forwarding half (#88 step 3):**
       Stripe's `event.created` has one-second resolution, so two events for
       the same subscription in the same second are indistinguishable in age
       and the second one is dropped here — silently, because we answer 200 and
       Stripe never retries. A purchase is safe (no row exists yet, so no
       ordering check runs); it is follow-up events that can be lost. The fix
       is on that side, and it is what Stripe recommends anyway: on any
       subscription event, fetch the current subscription object and forward
       *that*, rather than forwarding the event's own snapshot. Then every
       delivery carries current truth and ordering stops mattering much.
    3. **Ownership.** One Stripe subscription belongs to one guild. An event
       naming a different one is an anomaly that needs a human, not a retry.

    Returns a small dict saying whether the change was applied, or None if the
    write could not be completed — which the API turns into a 503, which the
    dashboard turns into a non-2xx, which makes Stripe retry. Losing an event
    quietly is the one outcome this must never produce.

    **It never touches the `servers` table.** A webhook can arrive for a guild
    that has no row there, and inserting one would mint a fresh `servers.id`
    above the captured grandfather line — silently costing that server the
    features it was promised for free, forever, as a side effect of paying us.
    There is a test for exactly this.
    """
    key = panel_view_key(guild_id)
    event_id = payload.get("event_id")
    subscription_id = payload.get("subscription_id")
    customer_id = payload.get("customer_id")
    price_id = payload.get("price_id")
    status = payload.get("status")
    cancel_at_period_end = payload.get("cancel_at_period_end", False)
    period_end = _parse_stripe_timestamp(payload.get("current_period_end"))
    event_created = _parse_stripe_timestamp(payload.get("event_created"))

    for name, value in (
        ("event_id", event_id),
        ("subscription_id", subscription_id),
        ("customer_id", customer_id),
        ("price_id", price_id),
        ("status", status),
    ):
        if not isinstance(value, str) or not value.strip():
            raise SettingRejected(name, f"bad_{name}")
    if period_end is None:
        raise SettingRejected("current_period_end", "bad_current_period_end")
    if event_created is None:
        raise SettingRejected("event_created", "bad_event_created")
    # Checked here as well as at the envelope, because bool("false") is True
    # and this is the field that decides whether the page says "renews" or
    # "ends". Coercing it would make a wrong statement about someone's money
    # out of a normalisation slip.
    if not isinstance(cancel_at_period_end, bool):
        raise SettingRejected("cancel_at_period_end", "bad_cancel_at_period_end")

    now = datetime.now(timezone.utc)
    grants_premium = _stripe_row_is_paid(status, period_end, now)

    try:
        with session_scope() as session:
            # Read-then-insert rather than insert-and-catch, because catching
            # here would mean a SAVEPOINT, and SAVEPOINT under pysqlite does not
            # behave the way it does under Postgres. A guard that is only
            # correct on the production database is a guard nothing can test.
            #
            # The genuine race — two concurrent deliveries of the same event —
            # is still caught, by the primary key, at commit. See the
            # IntegrityError branch below, which tells that apart from the
            # other constraint in this table.
            already_seen = (
                session.query(StripeEvent.event_id)
                .filter_by(event_id=event_id)
                .first()
            )
            if already_seen is not None:
                logger.info(
                    "Stripe event %s for guild %s already processed; ignoring the "
                    "retry.",
                    event_id,
                    key,
                )
                return {"applied": False, "reason": "duplicate_event"}
            # In the same transaction as the change it authorises: a failure
            # part-way through must roll back the ledger entry too, or the
            # event would be marked processed without ever having been applied
            # and Stripe's retry would be answered "already done".
            session.add(StripeEvent(event_id=event_id))
            _prune_stripe_events(session, now)

            # Keyed by subscription, so an event only ever touches the
            # subscription it is about. This is what removes the whole class of
            # bug where a renewal of an older subscription overwrote a newer
            # one and its later cancellation then switched off a server still
            # being billed for the other.
            row = (
                session.query(StripeSubscription)
                .filter_by(stripe_subscription_id=subscription_id)
                .first()
            )

            if row is not None:
                if row.server_id != key:
                    # One Stripe subscription cannot belong to two guilds. This
                    # is an anomaly needing a human, not a transient failure —
                    # so it is loud, and it is not applied.
                    logger.error(
                        "Stripe subscription %s is recorded against guild %s but "
                        "this event names %s; refusing to move it. This needs "
                        "looking at in Stripe.",
                        subscription_id,
                        row.server_id,
                        key,
                    )
                    return {"applied": False, "reason": "guild_mismatch"}
                stored_created = row.last_event_created
                if stored_created is not None and stored_created.tzinfo is None:
                    stored_created = stored_created.replace(tzinfo=timezone.utc)
                if stored_created is not None and event_created <= stored_created:
                    logger.info(
                        "Stripe event %s for subscription %s is older than the "
                        "state on file; recorded but not applied.",
                        event_id,
                        subscription_id,
                    )
                    return {"applied": False, "reason": "out_of_order"}

            # Everything an admin might later need to account for, in one
            # string. The renewal date and the cancel flag are in here on
            # purpose: "when did this server cancel" and "when did it last
            # renew" are the two questions this trail exists to answer, and a
            # descriptor of only the status would answer neither.
            before = None if row is None else _stripe_audit_value(row)
            after = _stripe_audit_value(
                SimpleNamespace(
                    status=status,
                    stripe_subscription_id=subscription_id,
                    price_id=price_id,
                    current_period_end=period_end,
                    cancel_at_period_end=cancel_at_period_end,
                )
            )

            if row is None:
                session.add(
                    StripeSubscription(
                        server_id=key,
                        stripe_customer_id=customer_id,
                        stripe_subscription_id=subscription_id,
                        price_id=price_id,
                        status=status,
                        current_period_end=period_end,
                        cancel_at_period_end=cancel_at_period_end,
                        last_event_created=event_created,
                        updated_at=now,
                    )
                )
            else:
                row.stripe_customer_id = customer_id
                row.stripe_subscription_id = subscription_id
                row.price_id = price_id
                row.status = status
                row.current_period_end = period_end
                row.cancel_at_period_end = cancel_at_period_end
                row.last_event_created = event_created
                row.updated_at = now

            if before != after:
                _record_dashboard_audit(
                    session,
                    guild_id,
                    STRIPE_AUDIT_ACTOR,
                    [("stripe_subscription", before, after)],
                )
    except SettingRejected:
        raise
    except IntegrityError:
        # A concurrent delivery of this same event won the race to the ledger's
        # primary key. Theirs is as good as ours — the same reasoning
        # capture_grandfather_line uses when another writer gets there first.
        if _stripe_event_recorded(event_id):
            logger.info(
                "Stripe event %s for guild %s was recorded concurrently; "
                "treating this delivery as the duplicate it is.",
                event_id,
                key,
            )
            return {"applied": False, "reason": "duplicate_event"}
        # Anything else is unexplained. Let Stripe retry rather than pretending
        # it landed.
        logger.error(
            "Unexpected integrity error recording Stripe event %s for guild %s.",
            event_id,
            key,
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "Could not record the Stripe subscription for guild %s; the webhook "
            "will be retried.",
            key,
            exc_info=True,
        )
        return None

    # In-process, so a purchase takes effect on the very next verification
    # rather than waiting out the TTL. This is the whole reason the write lands
    # in the bot rather than being made by the dashboard against the database.
    stripe_status_cache.invalidate(key)
    logger.info(
        "stripe WRITE guild=%s subscription=%s status=%s premium=%s event=%s",
        key,
        subscription_id,
        status,
        grants_premium,
        event_id,
    )
    return {"applied": True, "status": status, "premium": grants_premium}


def build_bot_api_deps() -> bot_api.BotAPIDeps:
    """Hand the API its complete set of capabilities.

    Every writer here is named for exactly what it writes. `write_settings` can
    only change the fields DASHBOARD_WRITABLE_FIELDS names, and
    `write_stripe_subscription` can only touch the `stripe_subscription` table
    -- so widening what the website may touch is an edit to this file,
    reviewable on its own, rather than a query string nobody noticed.
    """
    return bot_api.BotAPIDeps(
        is_ready=bot.is_ready,
        guild_present=dashboard_guild_present,
        is_admin=dashboard_is_admin,
        read_admin_guilds=dashboard_admin_guilds,
        read_settings=read_dashboard_settings,
        read_roles=read_dashboard_roles,
        read_channels=read_dashboard_channels,
        read_panel=read_dashboard_panel,
        read_audit=read_dashboard_audit,
        read_overview=read_dashboard_overview,
        write_settings=write_dashboard_settings,
        write_stripe_subscription=write_dashboard_stripe_subscription,
        post_panel=post_dashboard_panel,
        verify_group=request_group_verification,
    )


_bot_api_server: Optional[bot_api.BotAPIServer] = None


async def start_bot_api() -> None:
    """Bring the API up, if it is switched on and correctly configured.

    A bad configuration stops the API, not the bot. Verification is the product
    and it must keep working through an expired dashboard certificate — but the
    listener never comes up in a weaker shape than intended, and the log says so
    at ERROR rather than leaving an operator to wonder.
    """
    global _bot_api_server
    if _bot_api_server is not None:
        return  # on_ready fires again on every reconnect.

    try:
        config = bot_api.BotAPIConfig.from_env()
    except bot_api.BotAPIConfigError as error:
        logger.error("⚠️ Bot API is enabled but misconfigured; not starting it: %s", error)
        return

    if config is None:
        logger.info(
            "Bot API is disabled (BOT_API_ENABLED unset); nothing is listening."
        )
        return

    try:
        server = bot_api.BotAPIServer(config, build_bot_api_deps())
        await server.start()
        _bot_api_server = server
    except Exception:
        logger.error("⚠️ Bot API failed to start; continuing without it.", exc_info=True)


async def stop_bot_api() -> None:
    global _bot_api_server
    if _bot_api_server is None:
        return
    try:
        await _bot_api_server.stop()
    except Exception:
        logger.warning("Bot API did not shut down cleanly.", exc_info=True)
    finally:
        _bot_api_server = None


def start_background_task(name: str, coro, run_once: bool = False):
    """Schedule `coro` under `name`, unless that task is already accounted for.

    A long-lived task is (re)started only when it is missing or has died, so a
    crashed consumer recovers on the next reconnect. `run_once` work — the
    startup panel refresh — never runs a second time in the same process, since
    its views stay registered with discord.py across reconnects.
    """
    existing = background_tasks.get(name)
    if existing is not None and (run_once or not existing.done()):
        # We never awaited this one; close it so Python doesn't warn about it.
        coro.close()
        return existing

    task = asyncio.create_task(coro, name=name)

    def report_exit(finished):
        if finished.cancelled():
            logger.info(f"Background task '{name}' was cancelled.")
            return
        error = finished.exception()
        if error is not None:
            logger.error(
                f"Background task '{name}' died; it will restart on the next reconnect.",
                exc_info=error,
            )

    task.add_done_callback(report_exit)
    background_tasks[name] = task
    return task


# -------------------------------------------------------------------
# Bot Events
# -------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Bot is ready. Logged in as {bot.user} (ID: {bot.user.id})")
    _record_server_membership_day()
    start_background_task(
        "server_membership_snapshot", server_membership_snapshot_task()
    )
    start_background_task("results_consumer", consume_results_queue())
    # The invite worker's answers. Started unconditionally: the queue is
    # declared by whichever end connects first, so a deployment without the
    # worker simply has nothing arrive, and one added later needs no restart
    # here.
    start_background_task(
        "group_invite_results_consumer", consume_group_invite_results()
    )
    # Start periodic cleanup of expired pending verifications
    start_background_task("expired_pending_cleanup", expired_pending_cleanup_task())

    # Follow up with admins who ran /vrcverify_setup but never posted a panel.
    start_background_task("panel_nudge_sweep", panel_nudge_sweep_task())
    # Issue #49 phase 6. The one part of the group-invite feature that needs a
    # scheduler: nothing reads a lapsed guild's row, so nothing else would ever
    # notice that its seat should go back in the pool.
    start_background_task("seat_sweep", seat_sweep_task())

    # Drains buffered verification log entries into each guild's log channel.
    start_background_task(
        "verification_log_flush", verification_log_flush_task()
    )

    # Draws the grandfather line on the first boot after the tier goes live,
    # then never again. Must happen before the campaign watcher, which uses
    # the line to pick its audience.
    capture_grandfather_line()

    # Backfills the ever-paid ledger from Discord's own entitlement list,
    # ended ones included, so a server that subscribed and cancelled before
    # this shipped is not offered a free trial as though it were new. Runs in
    # the background: it is an API walk, nothing else on this path waits on it,
    # and trial_eligible fails closed while it is still running.
    start_background_task(
        "entitlement_history_sweep", sweep_entitlement_history(), run_once=True
    )

    # Waits for its trigger file; sends nothing on its own.
    start_background_task(
        "premium_cutover_watcher", watch_premium_cutover_trigger()
    )

    # A once-per-process reminder if the announcement is still outstanding.
    warn_if_cutover_incomplete()

    # Panels already carrying the current custom_ids are handled by the
    # persistent view registered in setup_hook, so this only has to re-edit the
    # stragglers: panels posted before this version, and ones that failed
    # earlier (revoked permissions, archived threads) and are retried each boot.
    start_background_task(
        "instruction_panel_refresh",
        refresh_all_instruction_panels(
            rebuild_embed=False, reason="startup", stale_only=True
        ),
        run_once=True,
    )

    # Start watching for a trigger file so you can update instructions at runtime
    # To trigger an instruction panel update type "touch /tmp/update_instructions.trigger" into a terminal
    trigger_path = os.getenv("INSTRUCTIONS_TRIGGER_PATH", "/tmp/update_instructions.trigger")
    poll = int(os.getenv("INSTRUCTIONS_TRIGGER_POLL", "5"))
    start_background_task(
        "instructions_trigger_watcher", watch_update_trigger_file(trigger_path, poll)
    )

    # The dashboard's door. Does nothing at all unless BOT_API_ENABLED is set,
    # and awaited rather than backgrounded so a refusal to bind is reported
    # here, in startup order, instead of surfacing later as a dead task.
    await start_bot_api()

    # Panel refresh logs its own completion summary once it finishes.
    logger.info("Bot startup tasks launched and ready to go!")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """DM the server owner a quick heads-up on finishing setup.

    Fires once, only for a genuine new join (discord.py does not re-dispatch
    this for guilds already in the cache on reconnect), so no idempotency
    guard is needed the way on_ready's background tasks require one.
    """
    logger.info(f"Joined guild {guild.id} ({guild.name})")
    try:
        owner = guild.owner or await fetch_member_cached(guild, guild.owner_id)
        if owner:
            await dm_localized(owner, guild, "guild_join_welcome_dm", server=guild.name)
    except Exception:
        logger.exception(f"Failed to send welcome DM for guild {guild.id}")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Drop the state that can only ever be wrong once we're out of a guild.

    The saved panel points at a message in a channel we can no longer touch,
    and the onboarding nudge has nobody left to nudge. Neither becomes valid
    again on its own, so keeping them means retrying them forever.

    The `servers` row and its role configuration are deliberately kept. Being
    removed is frequently temporary — a permissions cleanup, a server rebuild,
    an accidental kick — and forcing an admin back through /vrcverify_setup on
    re-invite costs them far more than a stale row costs us.

    The VRChat group seat is kept too, and that is a decision rather than an
    omission (issue #49, phase 6). Leaving the group here would free scarce
    capacity, but an accidental kick would then cost the admin a manual
    re-invite in VRChat on top of re-adding the bot -- the same trade this
    docstring already makes, with a higher price.

    The seat is not held forever either: a server the bot is not in stops
    paying sooner or later, and the lapse sweep reclaims it after the usual
    grace period. A server that keeps paying while the bot is out keeps its
    seat, which is the right answer -- they are paying for it.
    """
    logger.info(f"Removed from guild {guild.id} ({guild.name})")
    try:
        forget_instruction_panel(str(guild.id))
        forget_guild_onboarding(str(guild.id))
    except Exception:
        logger.exception(f"Failed to clean up state for departed guild {guild.id}")


def _note_entitlement_change(entitlement: discord.Entitlement, event: str) -> None:
    """Re-check a guild's plan on the next read, rather than waiting out the TTL.

    Purchases and refunds are exactly the moments where a stale cached value is
    most visible to the person who just paid, so these three events exist to
    make the change take effect immediately.
    """
    guild_id = getattr(entitlement, "guild_id", None)
    if guild_id is None:
        # A user-scoped entitlement for some other SKU; nothing guild-gated
        # depends on it.
        return
    premium_status_cache.invalidate(str(guild_id))
    logger.info("Entitlement %s for guild %s; premium status will re-resolve.", event, guild_id)

    # Write the guild into the ever-paid ledger, which is what makes a free
    # trial a once-per-server thing (issue #88). Recorded on `deleted` as well
    # as the other two, deliberately: the point is that this server HAS paid,
    # and a cancellation is the single event most likely to be the last one we
    # ever see for it. Recording only purchases would mean an entitlement that
    # lapsed while the bot was down is remembered by nobody.
    if getattr(entitlement, "sku_id", None) == PREMIUM_SKU_ID:
        record_entitlement_seen(guild_id)

    # A branded panel has to be re-edited for its styling to change, so a lapse
    # would otherwise leave premium styling in place until an operator ran a
    # fleet refresh. Only guilds that configured branding are touched; for
    # everyone else there is nothing to change and no reason to spend an edit.
    #
    # Deliberately fired on renewals and cancellations alike: the event only
    # says "re-resolve", and resolve_panel_style decides the outcome. A
    # subscription cancelled but not yet expired therefore keeps its styling.
    # isinstance rather than `is not None`: an unreadable table must not buy a
    # panel edit that resolve_panel_style would then decline to apply anyway.
    if isinstance(load_panel_branding(guild_id), tuple):
        schedule_panel_restyle(guild_id)


@bot.event
async def on_entitlement_create(entitlement: discord.Entitlement):
    _note_entitlement_change(entitlement, "created")


@bot.event
async def on_entitlement_update(entitlement: discord.Entitlement):
    # Fires when a subscription is cancelled (gaining an ends_at) as well as
    # when it renews, so this is not only a downgrade signal.
    _note_entitlement_change(entitlement, "updated")


@bot.event
async def on_entitlement_delete(entitlement: discord.Entitlement):
    _note_entitlement_change(entitlement, "deleted")


@bot.event
async def on_member_join(member: discord.Member):
    """Auto-verify users who are already verified in our database when they join a server."""
    try:
        guild_id = str(member.guild.id)
        discord_id = str(member.id)

        with session_scope() as session:
            server = session.query(Server).filter_by(server_id=guild_id).first()
            if not server or not server.role_id:
                return

            # Respect setting: treat None or missing column as enabled by default
            raw_av = getattr(server, "auto_verify_new_members", None)
            enabled = True if raw_av is None else bool(raw_av)
            if not enabled:
                return

            user = session.query(User).filter_by(discord_id=discord_id).first()
            if not user:
                return
            # Record a verification attempt timestamp on join for any existing user
            user.last_verification_attempt = datetime.now(timezone.utc)
            already_verified = bool(user.verification_status)

        if already_verified:
            await assign_role(discord_id, True, guild_id)
            logger.info(f"Auto-verified user {discord_id} in guild {guild_id} on join.")
    except Exception:
        logger.error("❌ Exception in on_member_join", exc_info=True)


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def _status_probe() -> dict[str, tuple[bool, str | None]]:
    """What this process can honestly answer for (issue #170 phase 2).

    Three parts, and the bot is the only one of the three services that can
    speak for two of them:

      discord-bot  ready, not merely running. A process holding a bad token
                   sits in a reconnect loop forever looking perfectly alive,
                   which is precisely the outage a heartbeat based on "the
                   interpreter is still executing" would miss.
      database     a real SELECT. The checker and the inviter never touch the
                   database, and the dashboard deliberately holds no credential
                   for it, so if this process does not ask, nothing does.
      bot-api      whether the listener the dashboard talks to actually came
                   up. It is allowed to fail without stopping the bot, which
                   makes it exactly the kind of thing that fails quietly.

    Never raises: every answer is a state, including "the check itself broke".
    The detail strings name internals on purpose. They are private, and reach
    the status page's alerting rather than its public page.
    """
    parts: dict[str, tuple[bool, str | None]] = {}

    latency = bot.latency
    if bot.is_ready() and latency == latency and latency != float("inf"):
        parts["discord-bot"] = (True, f"gateway ready, {int(latency * 1000)}ms")
    else:
        parts["discord-bot"] = (False, "gateway not ready")

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        parts["database"] = (True, None)
    except Exception as error:
        parts["database"] = (False, type(error).__name__)

    if os.getenv("BOT_API_ENABLED", "").strip():
        parts["bot-api"] = (
            _bot_api_server is not None,
            None if _bot_api_server is not None else "enabled but not listening",
        )

    return parts


if __name__ == "__main__":
    # Started before bot.run rather than in on_ready, so that a bot which never
    # becomes ready -- a revoked token, a Discord outage -- is reported as down
    # instead of reported as nothing at all. The status page needs two consecutive
    # bad reports before it publishes an outage, so an ordinary restart passes
    # through this state without ever appearing on the page.
    heartbeat.start_heartbeat("discord-bot", _status_probe)

    # log_handler=None or discord.py calls its own setup_logging, which adds
    # a SECOND StreamHandler to the root logger -- after install_log_scrubbing
    # ran, so without this filter. Every discord.* record then goes out
    # unescaped (and twice), and Docker merges the streams, so a forged line
    # lands in the same place the escaped one does.
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)