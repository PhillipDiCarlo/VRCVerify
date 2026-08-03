import os
import json
import asyncio
import logging
import secrets
import string
import re
import time
from typing import Optional
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
    DateTime,
    text,
    inspect,
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from locales import localizations, LANGUAGE_CODES


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
logger = logging.getLogger(__name__)

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

    Note this is only the DM ledger. Whether a server is *grandfathered* is not
    stored anywhere: it is `servers.id <= PREMIUM_GRANDFATHER_MAX_ID`, which
    needs no backfill, no marker, and survives a database restore unchanged.
    """

    __tablename__ = "premium_cutover_notice"
    server_id = Column(String, primary_key=True)
    sent_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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


# Creates any missing tables. Note this does NOT add columns to tables that
# already exist — those still need a manual ALTER.
Base.metadata.create_all(engine)


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


_member_fetch_cache = _TTLCache(REST_CACHE_MAX, REST_TTL_SECONDS)
_rest_semaphore = asyncio.Semaphore(REST_CONCURRENCY)

async def fetch_member_cached(guild: discord.Guild, user_id: int) -> discord.Member | None:
    if not guild:
        return None
    key = (guild.id, user_id)
    cached = _member_fetch_cache.get(key)
    if cached:
        return cached  # type: ignore
    async with _rest_semaphore:
        try:
            member = await guild.fetch_member(user_id)
            _member_fetch_cache.set(key, member)
            return member
        except discord.NotFound:
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
        # Sync slash commands to the server
        await self.tree.sync()


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
# Premium (Discord App Subscriptions)
# -------------------------------------------------------------------
# A guild-scoped subscription SKU: one purchase by a server owner entitles the
# whole guild. Gating reads Discord entitlements and nothing else — the
# subscription_status / email / last_renewal_date columns on `servers` are dead
# Stripe leftovers and are deliberately never consulted.


def _optional_int_env(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ %s is set but is not an integer; ignoring it.", name)
        return None


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

# Servers configured before the cutover keep these three for free, forever.
# The reduced cooldown and the activity log are new, so nobody is losing them.
GRANDFATHERED_FEATURES = frozenset(
    {FEATURE_UNVERIFIED_ROLE_REMOVAL, FEATURE_NICKNAME_SYNC, FEATURE_CUSTOM_DM}
)

# The grandfather line, drawn on the servers table's autoincrementing primary
# key. 820 is where it stood at the start of July 2026.
#
# The id is the cheapest honest answer to "was this server here first": it is
# already recorded, strictly increasing, and needs no backfill, no marker table
# and no date column that `servers` does not have. It also means the answer is
# identical on a restored database — a snapshot-at-first-boot approach would
# quietly re-draw the line wherever the restore happened to land.
PREMIUM_GRANDFATHER_MAX_ID = _int_env("PREMIUM_GRANDFATHER_MAX_ID", 820, minimum=0)

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


def is_grandfathered(guild_id) -> bool:
    """Was this guild configured before the premium cutover?

    Read straight off the `servers` primary key, so nothing has to be written
    at cutover time and the answer can never drift.
    """
    if guild_id is None:
        return False
    try:
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
            return row.id <= PREMIUM_GRANDFATHER_MAX_ID
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


async def resolve_premium_flags(guild_id) -> PremiumFlags:
    """Resolve every gate for a guild in one pass (no interaction available)."""
    if not PREMIUM_ENFORCED:
        return PremiumFlags(premium=True, grandfathered=True)
    premium = await guild_has_premium(guild_id)
    # Only worth a DB hit when the answer could still change the outcome.
    grandfathered = False if premium else is_grandfathered(guild_id)
    return PremiumFlags(premium=premium, grandfathered=grandfathered)


def resolve_premium_flags_from_interaction(
    interaction: discord.Interaction,
) -> PremiumFlags:
    """Same, for a command or button where the payload already has the answer."""
    if not PREMIUM_ENFORCED:
        return PremiumFlags(premium=True, grandfathered=True)
    premium = premium_from_interaction(interaction)
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

        remaining = check_verification_cooldown(
            user_id,
            window_seconds=resolve_premium_flags_from_interaction(
                interaction
            ).cooldown_window(),
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
            update_nickname=True
        )
        # localized confirmation
        await interaction.response.send_message(
            get_message("nickname_update_requested", interaction), ephemeral=True
        )


# -------------------------------------------------------------------
# Show Settings View (Paged)
# -------------------------------------------------------------------
# Which premium feature each settings page controls. Pages 1 (auto-verify) and
# 2 (language) are free, so they are absent.
SETTINGS_PAGE_FEATURE = {
    0: FEATURE_NICKNAME_SYNC,
}


class PagedSettingsView(View):
    def __init__(
        self,
        auto_nick: bool,
        instr_locale: str,
        auto_verify: bool,
        auto_verify_available: bool = True,
        page_index: int = 0,
        premium: Optional["PremiumFlags"] = None,
    ):
        super().__init__(timeout=None)
        # Current values (mutated by selects)
        self.auto_nick: bool = auto_nick
        self.instr_locale: str = instr_locale
        self.auto_verify: bool = auto_verify
        self.auto_verify_available: bool = auto_verify_available
        self.page: int = page_index  # 0: nick, 1: auto-verify, 2: locale
        # Resolved once by the command and threaded through every Back/Next
        # rebuild, so paging around can't re-ask Discord on each click.
        self.premium: PremiumFlags = premium or PremiumFlags(
            premium=True, grandfathered=True
        )

        # Build the initial controls for the current page
        self._add_controls_for_page()

    # ----- Rendering helpers -----
    def _page_locked(self) -> bool:
        """Is the current page's feature unavailable on this server's plan?"""
        feature = SETTINGS_PAGE_FEATURE.get(self.page)
        return feature is not None and not self.premium.allows(feature)

    def _rebuilt(self, page_index: int) -> "PagedSettingsView":
        """A copy of this view on another page, carrying every current value."""
        return PagedSettingsView(
            self.auto_nick,
            self.instr_locale,
            self.auto_verify,
            self.auto_verify_available,
            page_index=page_index,
            premium=self.premium,
        )

    def _page_title_and_desc(self) -> tuple[str, str, str]:
        """Return (title, description, current_str) for the active page."""
        locked = self._page_locked()
        if self.page == 0:
            title = "1.) Enable auto nickname change"
            desc = "Automatically update users’ Discord nicknames to match their VRChat display names."
            current = f"Current: {'Yes' if self.auto_nick else 'No'}"
        elif self.page == 1:
            title = "2.) Auto verify new members on join"
            desc = "If enabled, members already verified will automatically receive the role when they join."
            if not self.auto_verify_available:
                current = "Current: Yes (unavailable - DB column missing)"
            else:
                current = f"Current: {'Yes' if self.auto_verify else 'No'}"
        else:
            title = "3.) Instructions message language"
            desc = "Choose the language used for the instructions message/buttons."
            current = f"Current: {self.instr_locale}"

        if locked:
            current += "\n(Premium feature — not active on this server.)"
            desc += "\n\n🔒 Core 18+ verification stays free. This particular automation is part of VRCVerify Premium — use the button below to unlock it for this server."
        return title, desc, current

    def render_content(self) -> str:
        title, desc, current = self._page_title_and_desc()
        return (
            "⚙️ VRChat Verify Settings\n\n"
            f"{title}\n"
            f"{desc}\n"
            f"{current}"
        )

    def _add_controls_for_page(self):
        # Add the appropriate select for the current page, then nav + save
        if self.page == 0:
            nick_options = [
                discord.SelectOption(label="Yes", value="yes", default=self.auto_nick),
                discord.SelectOption(label="No",  value="no",  default=not self.auto_nick),
            ]
            nick_locked = self._page_locked()
            nick_dropdown = Select(
                placeholder=(
                    "Premium feature — unlock below"
                    if nick_locked
                    else "Choose Yes or No"
                ),
                min_values=1,
                max_values=1,
                options=nick_options,
                disabled=nick_locked,
            )

            async def on_nick_select(interaction: discord.Interaction):
                if not nick_locked:
                    self.auto_nick = (interaction.data["values"][0] == "yes")
                await interaction.response.defer(ephemeral=True)

            nick_dropdown.callback = on_nick_select
            self.add_item(nick_dropdown)

        elif self.page == 1:
            av_options = [
                discord.SelectOption(label="Yes", value="yes", default=self.auto_verify),
                discord.SelectOption(label="No",  value="no",  default=not self.auto_verify),
            ]
            av_dropdown = Select(
                placeholder=(
                    "Choose Yes or No"
                    if self.auto_verify_available
                    else "DB column missing; cannot change"
                ),
                min_values=1,
                max_values=1,
                options=av_options,
                disabled=not self.auto_verify_available,
            )

            async def on_auto_verify_select(interaction: discord.Interaction):
                # Only mutate if available
                if self.auto_verify_available:
                    self.auto_verify = (interaction.data["values"][0] == "yes")
                await interaction.response.defer(ephemeral=True)

            av_dropdown.callback = on_auto_verify_select
            self.add_item(av_dropdown)

        else:
            locale_options = [
                discord.SelectOption(label=code, value=code, default=(code == self.instr_locale))
                for code in LANGUAGE_CODES
            ]
            locale_dropdown = Select(
                placeholder="Choose a language",
                min_values=1,
                max_values=1,
                options=locale_options
            )

            async def on_locale_select(interaction: discord.Interaction):
                self.instr_locale = interaction.data["values"][0]
                await interaction.response.defer(ephemeral=True)

            locale_dropdown.callback = on_locale_select
            self.add_item(locale_dropdown)

        # Nav buttons
        back_btn = Button(label="Back", style=discord.ButtonStyle.secondary, disabled=(self.page == 0))
        next_btn = Button(label="Next", style=discord.ButtonStyle.secondary, disabled=(self.page == 2))
        save_btn = Button(label="Save", style=discord.ButtonStyle.primary)

        async def on_back(interaction: discord.Interaction):
            new_view = self._rebuilt(self.page - 1)
            await interaction.response.edit_message(
                content=new_view.render_content(), view=new_view
            )

        async def on_next(interaction: discord.Interaction):
            new_view = self._rebuilt(self.page + 1)
            await interaction.response.edit_message(
                content=new_view.render_content(), view=new_view
            )

        async def on_save(interaction: discord.Interaction):
            # A locked page's control is disabled, so its value can't have been
            # changed here — but it must not be written back either. Leaving the
            # stored preference untouched means an admin who subscribes later
            # gets their original choice back instead of whatever this view
            # happened to be holding.
            nick_allowed = self.premium.allows(FEATURE_NICKNAME_SYNC)

            # persist into your servers table
            with session_scope() as session:
                srv = session.query(Server).filter_by(server_id=str(interaction.guild.id)).first()
                if not srv:
                    srv = Server(server_id=str(interaction.guild.id), owner_id=str(interaction.user.id))
                    session.add(srv)
                if nick_allowed:
                    srv.auto_nickname_change = bool(self.auto_nick)
                srv.instructions_locale = str(self.instr_locale)
                if self.auto_verify_available:
                    setattr(srv, "auto_verify_new_members", bool(self.auto_verify))

            notes = ""
            if not self.auto_verify_available:
                notes += "\n(Note: 'Auto verify new members' not saved; DB column missing.)"
            if not nick_allowed:
                notes += "\n(Note: 'Auto nickname change' is a premium feature and was not changed.)"

            msg = (
                get_message(
                    "settings_saved",
                    interaction,
                    nickname="Yes" if self.auto_nick else "No",
                    locale=self.instr_locale,
                )
                + notes
            )
            await interaction.response.edit_message(content=msg, view=None)

        back_btn.callback = on_back
        next_btn.callback = on_next
        save_btn.callback = on_save

        self.add_item(back_btn)
        self.add_item(next_btn)
        self.add_item(save_btn)

        # Discord renders the label and price on a premium button itself, so it
        # always shows the live price rather than one hardcoded here.
        if self._page_locked() and PREMIUM_SKU_ID is not None:
            self.add_item(Button(sku_id=PREMIUM_SKU_ID))


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


async def record_guild_verification(guild_id: str, guild: Optional[discord.Guild]):
    """
    Count a completed 18+ verification for a guild. When the guild crosses
    MILESTONE_VERIFICATION_COUNT, DM the admin who configured the bot
    (fallback: the guild owner) a one-time thank-you with the donation link.
    """
    if not guild_id:
        return
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

        remaining = check_verification_cooldown(
            user_id,
            window_seconds=resolve_premium_flags_from_interaction(
                interaction
            ).cooldown_window(),
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
            code=None  # No-code re-check
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
    update_nickname: bool = False
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
        )

        max_publish_tries = int(os.getenv("RABBITMQ_PUBLISH_TRIES", "3"))
        last_exc: Exception | None = None
        for attempt in range(1, max_publish_tries + 1):
            conn = None
            try:
                conn = _rabbitmq_connect_with_retry(max_tries=1)
                channel = conn.channel()
                channel.queue_declare(queue=RABBITMQ_REQUEST_QUEUE, durable=True)
                channel.basic_publish(
                    exchange="",
                    routing_key=RABBITMQ_REQUEST_QUEUE,
                    body=json.dumps(message),
                    properties=properties,
                )
                logger.info("📤 Sent to vrc_online_checker: %s", message)
                return
            except AMQPError as e:
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

        # Own scope: a prior re-check/nickname request must never block Verify.
        remaining = check_verification_cooldown(
            discord_id,
            window_seconds=resolve_premium_flags_from_interaction(
                interaction
            ).cooldown_window(),
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
            discord_id, vrc_user_id, self.guild_id, verification_code
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
    donate_hint = get_message("setup_donate_hint", interaction, kofi_link=KOFI_URL)
    # Donate hint stays last so it reads as a footer under everything else.
    await interaction.response.send_message(
        base + extra_local + panel_nudge + donate_hint, ephemeral=True
    )


# -------------------------------------------------------------------
# Slash Command: /vrcverify_subscription
# -------------------------------------------------------------------
class PremiumUpgradeView(View):
    """Carries Discord's own purchase button for the premium SKU.

    Discord renders the label and the current price itself, so nothing here
    needs to know what the tier costs.
    """

    def __init__(self):
        super().__init__(timeout=None)
        if PREMIUM_SKU_ID is not None:
            self.add_item(Button(sku_id=PREMIUM_SKU_ID))


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

    flags = resolve_premium_flags_from_interaction(interaction)
    server_name = interaction.guild.name if interaction.guild else "this server"

    if flags.premium:
        message = get_message("premium_status_active", interaction, server=server_name)
        # No purchase button: they already bought it. send_message() calls
        # view.is_finished(), so an absent view has to be MISSING, not None.
        extra = {}
    else:
        key = (
            "premium_status_grandfathered"
            if flags.grandfathered
            else "premium_status_inactive"
        )
        message = get_message(key, interaction, server=server_name)
        extra = {"view": PremiumUpgradeView()}

    await interaction.response.send_message(message, ephemeral=True, **extra)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_support
# -------------------------------------------------------------------
@bot.tree.command(
    name="vrcverify_support",
    description="Get help with the VRChat 18+ verification process."
)
async def vrcverify_support(interaction: discord.Interaction):
    """
    Sends an ephemeral message to the user with instructions on how to get support,
    whether that’s contacting an admin or visiting an external support link.
    """
    # Customize the text below however you like
    # localized support info
    await interaction.response.send_message(
        get_message("support_info", interaction), ephemeral=True
    )


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
    # Same builder the refresh path uses, so a posted panel and a refreshed one match.
    embed = build_instructions_embed(instr_locale)

    view = VRCVerifyInstructionView(locale=instr_locale)
    # Send the initial response and then fetch the message
    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()

    # Save the channel and message IDs to your database for reinitialization.
    guild_id = str(interaction.guild.id)
    channel_id = str(interaction.channel.id)
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server:
            # Posting the panel before running /vrcverify_setup used to drop the
            # ids on the floor: the panel went up but nothing tracked it, so the
            # startup refresh never saw it and /vrcverify_status would call it
            # missing. Create the row like the settings view does instead.
            server = Server(server_id=guild_id, owner_id=str(interaction.user.id))
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
    name="vrcverify_settings", description="Admin: Configure VRChat-Verify settings"
)
@app_commands.checks.has_permissions(administrator=True)
async def vrcverify_settings(interaction: discord.Interaction):
    """Show a paged settings view with one control per page and a title above it."""
    has_av_col = server_has_column("auto_verify_new_members")
    with session_scope() as session:
        srv = session.query(Server).filter_by(server_id=str(interaction.guild.id)).first()
        if srv:
            current_nick = bool(srv.auto_nickname_change)
            raw_locale = getattr(srv, "instructions_locale", None)
            current_locale = raw_locale or "en-US"
            raw_av = getattr(srv, "auto_verify_new_members", None)
            current_auto_verify = True if raw_av is None else bool(raw_av)
        else:
            current_nick = False
            current_locale = "en-US"
            current_auto_verify = True

    view = PagedSettingsView(
        current_nick,
        current_locale,
        current_auto_verify,
        auto_verify_available=has_av_col,
        page_index=0,
        premium=resolve_premium_flags_from_interaction(interaction),
    )
    await interaction.response.send_message(
        content=view.render_content(), view=view, ephemeral=True
    )


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

    await interaction.followup.send("\n".join(lines), ephemeral=True)


class SetRequestMessageModal(discord.ui.Modal, title="Set Custom Verification Message"):
    custom_message: discord.ui.TextInput = discord.ui.TextInput(
        label="Custom message (leave blank to clear)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="Enter message shown after successful verification. Only discord.com / vrchat.com links allowed."
    )

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self._orig_interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.custom_message.value or "").strip()
        clearing = raw == "" or raw.lower() in {"clear", "reset", "none", "default"}

        # Sanitize & validate only if not clearing
        if not clearing:
            raw, invalid = sanitize_custom_message(raw)
            if invalid:
                pretty_invalid = "\n".join(f"- {u}" for u in invalid[:10])
                return await interaction.response.send_message(
                    get_message("custom_msg_invalid_links", interaction, invalid_list=pretty_invalid),
                    ephemeral=True,
                )

        guild_id = str(interaction.guild.id)
        with session_scope() as session:
            server = session.query(Server).filter_by(server_id=guild_id).first()
            if not server:
                server = Server(server_id=guild_id, owner_id=str(interaction.user.id))
                session.add(server)

            if clearing:
                server.custom_verification_requested_message = None
                result = get_message("custom_msg_cleared", interaction)
            else:
                server.custom_verification_requested_message = raw
                result = get_message("custom_msg_saved", interaction)

        await interaction.response.send_message(result, ephemeral=True)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_setrequestmessage (modal-based)
# -------------------------------------------------------------------
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="vrcverify_logchannel",
    description="Admin: Choose where verification activity is logged (premium).",
)
@app_commands.describe(
    channel="Channel to post verification activity in. Leave empty to turn logging off."
)
async def vrcverify_logchannel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
):
    """Set or clear this server's verification log channel."""
    guild_id = str(interaction.guild.id)

    if channel is None:
        set_log_channel(guild_id, None)
        await interaction.response.send_message(
            get_message("log_channel_cleared", interaction), ephemeral=True
        )
        return

    # Announcement channels can be *followed* by other servers, which mirrors
    # published messages into them via webhook. Every entry here pairs a
    # Discord user with their 18+ status, so allowing that would let an age
    # disclosure about a named member be republished into servers they have no
    # relationship with. There is no good reason to want a verification log in
    # an announcement channel, and one genuinely bad outcome, so it is refused.
    if channel.is_news():
        await interaction.response.send_message(
            get_message(
                "log_channel_announcement", interaction, channel=channel.mention
            ),
            ephemeral=True,
        )
        return

    flags = resolve_premium_flags_from_interaction(interaction)
    if not flags.allows(FEATURE_ACTIVITY_LOG):
        # send_message calls view.is_finished(), so an absent view has to be
        # omitted entirely rather than passed as None.
        extra = {"view": PremiumUpgradeView()} if PREMIUM_SKU_ID is not None else {}
        await interaction.response.send_message(
            get_message("log_channel_premium_only", interaction),
            ephemeral=True,
            **extra,
        )
        return

    # Post the confirmation into the target channel rather than only replying
    # ephemerally. It doubles as a permissions check: if the bot cannot post
    # there, the admin finds out now instead of discovering an empty log later.
    try:
        await channel.send(
            get_message(
                "log_channel_ready",
                SimpleNamespace(
                    locale=get_server_locale_code(guild_id, interaction.guild)
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            get_message(
                "log_channel_no_permission", interaction, channel=channel.mention
            ),
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.warning(
            "Could not post the log channel confirmation for guild %s.",
            guild_id,
            exc_info=True,
        )
        await interaction.response.send_message(
            get_message(
                "log_channel_no_permission", interaction, channel=channel.mention
            ),
            ephemeral=True,
        )
        return

    set_log_channel(guild_id, str(channel.id))
    await interaction.response.send_message(
        get_message("log_channel_set", interaction, channel=channel.mention),
        ephemeral=True,
    )


# -------------------------------------------------------------------
# Slash Command: /vrcverify_setrequestmessage (modal-based)
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="vrcverify_setrequestmessage",
    description="Admin: Open a modal to set/clear the post-Verify success message."
)
async def vrcverify_setrequestmessage(interaction: discord.Interaction):
    await interaction.response.send_modal(SetRequestMessageModal(interaction))


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
    """
    with session_scope() as session:
        notified = {
            panel_view_key(row.server_id)
            for row in session.query(PremiumCutoverNotice.server_id).all()
        }
        rows = (
            session.query(Server)
            .filter(Server.id <= PREMIUM_GRANDFATHER_MAX_ID)
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


def panel_view_key(server_id) -> str:
    """Normalise a guild id for the instruction_panel_views table.

    `servers.server_id` is declared String but can come back as an int, because
    the deployed column is an integer type and SQLAlchemy returns whatever the
    driver gives. instruction_panel_views.server_id really is text, so an
    un-normalised id makes Postgres reject `character varying = bigint` — and,
    worse, makes the in-memory version lookup silently never match.
    """
    return str(server_id)


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


def build_instructions_embed(locale: str) -> Embed:
    """Build the localized instruction panel embed."""
    strings = localizations.get(locale, localizations["en-US"])
    embed = Embed(
        title=strings.get("instructions_title", ""),
        description=strings.get("instructions_desc", ""),
        color=discord.Color.blue(),
    )
    usage_example = "**Example Usage**:\n" "```bash\n" "/vrcverify\n" "```"
    embed.add_field(name="Example Command", value=usage_example, inline=False)
    return embed


async def probe_instruction_panel(entry, rebuild_embed: bool) -> str:
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
        message = bot.get_partial_messageable(int(channel_id)).get_partial_message(
            int(message_id)
        )
    except (TypeError, ValueError):
        logger.warning(f"⚠️ Malformed channel/message id for guild {entry['server_id']}; skipping.")
        return "malformed"

    try:
        # Built inside the try on purpose: a bad locale row must not escape and
        # abort the whole fleet pass, it only costs this one guild.
        payload = {"view": VRCVerifyInstructionView(locale=entry["locale"])}
        if rebuild_embed:
            payload["embed"] = build_instructions_embed(entry["locale"])
        await message.edit(**payload)
        if entry.get("view_version") != INSTRUCTIONS_VIEW_VERSION:
            record_panel_view_version(entry["server_id"])
        logger.debug(f"Refreshed instructions message for guild {entry['server_id']}")
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
    start_background_task("results_consumer", consume_results_queue())
    # Start periodic cleanup of expired pending verifications
    start_background_task("expired_pending_cleanup", expired_pending_cleanup_task())

    # Follow up with admins who ran /vrcverify_setup but never posted a panel.
    start_background_task("panel_nudge_sweep", panel_nudge_sweep_task())

    # Drains buffered verification log entries into each guild's log channel.
    start_background_task(
        "verification_log_flush", verification_log_flush_task()
    )

    # Waits for its trigger file; sends nothing on its own.
    start_background_task(
        "premium_cutover_watcher", watch_premium_cutover_trigger()
    )

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
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)