"""VRChat group-invite worker (issue #49).

Runs the second VRChat account -- the one that joins each server's VRChat
group and, from a later phase, sends invites to members who ask for one. It is
a separate process from vrc_online_checker for two reasons: the account is
deliberately separate so moderation action against one cannot take the other
down, and two consumers on one queue would split messages round-robin, so the
age checker would swallow invite jobs.

SECURITY -- the rule this file exists to enforce:

    This worker NEVER joins a group it was not explicitly told to join.

There is no background loop that polls for or accepts pending group invites,
and there must never be one. join_group is called only while handling a
verify_group_setup job naming one specific group id, and the bot builds that
job from the guild's stored settings -- typed in by a Discord admin on the
dashboard -- rather than from anything a browser sent. Anyone can invite this
account to their group; that alone must never put it in one.

Two jobs live here. `verify_group_setup` is the admin-facing one described
above. `send_group_invite` is the member-facing one: a verified member presses
a button in their post-verification DM, and this worker checks whether they
are already in the group before inviting them. It never invites anyone who did
not ask, and it never overrides a block -- see send_group_invite.
"""

import json
import logging
import os
import random
import time

import pika
from dotenv import load_dotenv
from pika.exceptions import AMQPError

from log_safety import install_log_scrubbing
from vrchatapi.api.groups_api import GroupsApi
from vrchatapi.exceptions import ApiException, UnauthorizedException
from vrchatapi.models.create_group_invite_request import CreateGroupInviteRequest

from vrc_session import (
    TRANSIENT_HTTP_STATUSES,
    VRChatAccount,
    VRChatSession,
    backoff_delay,
    check_session_store_writable,
    classify_api_error,
    default_session_error,
    request_timeout,
)

load_dotenv()


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """int(os.getenv(...)) that survives a blank or malformed value.

    Same shape as bot.py's helper of the same name, and here for the same
    reason it is there: `INVITE_MIN_SPACING_SECONDS=` in a .env file -- a
    setting somebody started to write and left empty -- would otherwise raise
    ValueError at import, before any logging is configured, and take the whole
    worker down with a traceback that names neither the variable nor the file.
    """
    try:
        return max(minimum, int(os.getenv(name) or default))
    except ValueError:
        return default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name) or default))
    except ValueError:
        return default


RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT") or 5672)
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")

# Its own queues, not the verification ones. See the module docstring.
REQUEST_QUEUE_NAME = os.getenv("RABBITMQ_GROUP_INVITE_QUEUE", "vrcverify_group_invites")
RESULT_QUEUE_NAME = os.getenv(
    "RABBITMQ_GROUP_INVITE_RESULT_QUEUE", "vrcverify_group_invite_results"
)

# Identifies this service to VRChat. The Creator Guidelines require the
# "applicationName/Version contactInfo" form and treat improper identification
# as grounds for moderation action, so the contact address must be one a VRChat
# moderator can actually reach us at. Distinct from the checker's so their
# traffic can be told apart.
INVITE_USER_AGENT = "VRCVerifyGroupInvite/1.0 contact@esattotech.com"

INVITE_ACCOUNT = VRChatAccount.from_env(
    user_agent=INVITE_USER_AGENT, prefix="INVITE_", label="inviter"
)
vrchat_session = VRChatSession(INVITE_ACCOUNT)

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level_str, logging.INFO),
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("pika").setLevel(logging.WARNING)
# Group and user ids off the queue reach these logs. See log_safety.
install_log_scrubbing()

# -------------------------------------------------------------------
# What the account must be able to do, confirmed against a live group
# on 2026-08-19 (see issue #49).
# -------------------------------------------------------------------
# The permission the whole feature rests on. It is its OWN checkbox in the
# group's role settings: an admin role carrying group-members-manage,
# group-members-viewall and group-roles-assign still got 403 from
# create_group_invite until this one was ticked.
PERMISSION_INVITE = "group-invites-manage"

# Optional. Lets us ask whether someone is already in the group, so a member
# who is can be told that instead of being handed a button that would fail.
# Without it the feature still works and falls back to attempting the invite.
PERMISSION_VIEW_MEMBERS = "group-members-viewall"

# Owners may report a wildcard rather than the enumerated set. Unconfirmed --
# treated as sufficient because refusing an owner would be the worse mistake.
PERMISSION_WILDCARD = "*"

JOB_VERIFY_SETUP = "verify_group_setup"
JOB_SEND_INVITE = "send_group_invite"
JOB_LEAVE_GROUP = "leave_group"

# Outcomes the dashboard renders. Each names one specific thing an admin can
# act on, because "setup failed" tells them nothing about what to do next.
STATE_READY = "ready"
STATE_JOIN_REQUESTED = "join_requested"
STATE_NOT_INVITED = "not_invited"
STATE_NO_INVITE_PERMISSION = "no_invite_permission"
STATE_GROUP_NOT_FOUND = "group_not_found"
# The claim code is not in the group description, so nothing here proves
# the person who typed this group id into the dashboard has anything to do
# with the group. Refused BEFORE joining -- see claim_code_present.
STATE_CODE_MISSING = "code_missing"
# Terminal: no amount of re-inviting fixes being banned, so it must not be
# reported as "we have not been invited yet".
STATE_BANNED = "banned"
STATE_BAD_JOB = "bad_job"
STATE_VRCHAT_UNAVAILABLE = "vrchat_unavailable"

# Outcomes of trying to invite ONE member (issue #49, phase 5). A separate
# vocabulary from the setup states above, because they answer a different
# question and are read by a different audience: a setup state is a sentence
# for the server's admin on the dashboard, an invite state is a sentence for
# the member in a DM. Sharing one set would mean the member reading
# "no_invite_permission" as though it were something they could fix.
INVITE_SENT = "sent"
# The member is already in the group, so there is nothing to send. Not a
# failure, and the bot stores it so the offer is never made to them again.
INVITE_ALREADY_MEMBER = "already_member"
# An invite is already sitting in their VRChat notifications. Sending a second
# would be the "unsolicited invite" pattern the Creator Guidelines call abuse,
# and it would not help them either.
INVITE_ALREADY_INVITED = "already_invited"
# 403 "You can't invite that user" -- the member has group invites switched
# off, or has blocked the account. Terminal on purpose: the whole compliance
# argument for this feature is that a member's "no" is honoured, so this is
# recorded and never retried.
INVITE_BLOCKED = "blocked"
# The member is banned from the group. Nothing the bot or the member can do.
INVITE_BANNED = "banned"
# The group has gone, or the account has lost its invite permission since the
# setup check passed. The server's problem, not the member's -- so the member
# is told something honest and the offer stays available for a later attempt.
INVITE_GROUP_NOT_FOUND = "group_not_found"
# The member's stored VRChat account does not resolve. Their problem to fix by
# re-verifying, and emphatically NOT the server's -- reported separately
# because create_group_invite answers 404 for this and for a missing group
# alike, and telling the member their admin broke something sends them to
# complain about a setup that is working.
INVITE_USER_NOT_FOUND = "user_not_found"
INVITE_NO_PERMISSION = "no_invite_permission"
INVITE_VRCHAT_UNAVAILABLE = "vrchat_unavailable"
INVITE_BAD_JOB = "bad_job"

# Enumerated rather than reflected over, unlike the STATE_* set above: the
# INVITE_ prefix is already carrying the account's user agent and user id, so
# a test that swept the module by prefix would pick up two strings that are
# not states at all. The bot mirrors this set and a test asserts they agree.
# Outcomes of giving a group's seat back (issue #49, phase 6). Deliberately
# few: the only question the bot needs answered is "is this account out of that
# group", because that is what decides whether the seat may be handed to
# somebody else. Everything that is not a definite yes is a retry.
LEAVE_DONE = "left"
LEAVE_FAILED = "leave_failed"

LEAVE_STATES = frozenset({LEAVE_DONE, LEAVE_FAILED})

INVITE_STATES = frozenset(
    {
        INVITE_SENT,
        INVITE_ALREADY_MEMBER,
        INVITE_ALREADY_INVITED,
        INVITE_BLOCKED,
        INVITE_BANNED,
        INVITE_GROUP_NOT_FOUND,
        INVITE_USER_NOT_FOUND,
        INVITE_NO_PERMISSION,
        INVITE_VRCHAT_UNAVAILABLE,
        INVITE_BAD_JOB,
    }
)

VRCHAT_CALL_RETRIES = _int_env("INVITE_CALL_RETRIES", 3)

# Minimum gap between two create_group_invite calls, with jitter on top.
#
# One account issues invites for every guild, so throughput is a shared budget
# and a server running a verification drive must not be able to spend all of
# it in a burst. 429 plus backoff would eventually shape this, but only after
# VRChat has already seen the burst -- and "a lot of group invites very fast
# from one account" is precisely the pattern the Creator Guidelines describe
# as abuse. Cheaper to never send it.
#
# No lock guards the timestamp below because none is needed: this worker
# consumes with prefetch_count=1 and runs VRChat calls inline on the consumer
# thread, so exactly one invite is ever in flight.
# Capped as well as floored. time.sleep(inf) raises OverflowError inside the
# consumer callback, and any value past the broker heartbeat drops the
# connection mid-job -- so a mistyped setting must degrade to "slow", never to
# "the worker stops answering".
INVITE_MIN_SPACING_SECONDS = min(
    60.0, _float_env("INVITE_MIN_SPACING_SECONDS", 3.0)
)
_last_invite_call = 0.0

# Which account this worker is. Reported back with every result so the bot can
# record WHICH invite account joined a group rather than assuming there is only
# one -- there is exactly one today, and the 100-group seat cap (200 with VRC+)
# guarantees there will not always be.
INVITE_ACCOUNT_USER_ID = (os.getenv("INVITE_VRCHAT_USER_ID") or "").strip() or None


def _rabbitmq_parameters() -> pika.ConnectionParameters:
    """Connection parameters, matching the other two services.

    A third copy of this, and it should become one shared helper the next time
    any of them changes -- bot.py and vrc_online_checker.py carry the other two.
    """
    return pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD),
        # Deliberately longer than the other two services. VRChat work runs
        # inline on this thread, and pika cannot send heartbeats while
        # start_consuming() is inside the callback. A job doing three
        # retried call chains under 429/5xx can outlast the usual 2x60s
        # window, at which point the broker drops the connection, the ack
        # fails, and the job is redelivered and re-run.
        heartbeat=int(os.getenv("RABBITMQ_HEARTBEAT_INVITER", "300")),
        blocked_connection_timeout=int(os.getenv("RABBITMQ_BLOCKED_TIMEOUT", "30")),
        connection_attempts=int(os.getenv("RABBITMQ_CONN_ATTEMPTS", "3")),
        retry_delay=int(os.getenv("RABBITMQ_RETRY_DELAY", "2")),
        socket_timeout=int(os.getenv("RABBITMQ_SOCKET_TIMEOUT", "10")),
    )


def _rabbitmq_connect_with_retry(max_tries: int = 0) -> pika.BlockingConnection:
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
            logging.warning("RabbitMQ connection failed; retrying in %.1fs (attempt %s)", delay, attempt)
            time.sleep(delay)


def is_configured() -> bool:
    return bool(INVITE_ACCOUNT.username and INVITE_ACCOUNT.password)


def _call_with_retry(func, *args, **kwargs):
    """Run one VRChat call, retrying only transient upstream failures.

    Backoff is required of every caller by VRChat's guidelines, and the delay
    carries jitter so many workers cannot synchronise into a spike.
    """
    last_exc = None
    for attempt in range(1, VRCHAT_CALL_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except UnauthorizedException as e:
            # Handled here rather than at each call site: only the first call
            # used to do this, so a 401 from any later call left a known-dead
            # client installed until some unrelated job happened to hit the
            # first one.
            vrchat_session.invalidate(classify_api_error(e))
            raise
        except ApiException as e:
            last_exc = e
            if getattr(e, "status", None) == 401:
                vrchat_session.invalidate(classify_api_error(e))
                raise
            if getattr(e, "status", None) not in TRANSIENT_HTTP_STATUSES or attempt >= VRCHAT_CALL_RETRIES:
                raise
            delay = backoff_delay(attempt)
            logging.warning(
                "Transient VRChat failure (status=%s); retrying in %.2fs (attempt %s/%s)",
                getattr(e, "status", None), delay, attempt, VRCHAT_CALL_RETRIES,
            )
            time.sleep(delay)
    raise last_exc or RuntimeError("VRChat retry loop exited without a result")


def _membership_status(group) -> str | None:
    """`membership_status` as a plain string, whether it arrives as an enum."""
    status = getattr(group, "membership_status", None)
    if status is None:
        return None
    return str(getattr(status, "value", status))


def _permissions(group) -> set[str]:
    member = getattr(group, "my_member", None)
    return set(getattr(member, "permissions", None) or []) if member else set()


def _capabilities(perms: set[str]) -> tuple[bool, bool]:
    """(can_invite, can_see_members) from one permission set."""
    if PERMISSION_WILDCARD in perms:
        return True, True
    return PERMISSION_INVITE in perms, PERMISSION_VIEW_MEMBERS in perms


def _result(job: dict, state: str, **extra) -> dict:
    payload = {
        "type": JOB_VERIFY_SETUP,
        "jobID": job.get("jobID"),
        "guildID": job.get("guildID"),
        "groupID": job.get("groupID"),
        "ok": state == STATE_READY,
        "state": state,
        "can_invite": False,
        "can_see_members": False,
        "group_name": None,
        "icon_url": None,
        "error_message": None,
        "accountID": INVITE_ACCOUNT_USER_ID,
    }
    payload.update(extra)
    return payload


def _display(group) -> dict:
    """The group's name and icon, as the dashboard shows them.

    One helper rather than two arguments at eight call sites: they are always
    reported together, and a result that carried the name without the icon
    would render a nameplate with a hole in it.

    `icon_url`, never `banner_url`. They are separate fields on the group and
    the banner is a wide header image -- correct in VRChat's own UI, wrong
    beside a one-line status.
    """
    return {
        "group_name": getattr(group, "name", None),
        "icon_url": getattr(group, "icon_url", None),
    }


def _api_detail(exc) -> str:
    """The most useful short description of an ApiException, for a log line.

    VRChat answers with a JSON envelope whose `error.message` is the sentence a
    human wrote; everything around it is noise. Falls back to the raw body.

    This exists because its absence cost three deploys. Every unexpected status
    from this worker was being classified into one of our own state names and
    the body dropped on the floor, so the logs recorded what WE concluded and
    never what VRChat said -- which is the only way to find out that a guess
    about an endpoint was wrong.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if body:
        try:
            parsed = json.loads(body)
            message = (parsed.get("error") or {}).get("message")
            if message:
                # Capped like the fallbacks below. This is the branch that
                # actually gets taken, and it was the one without a limit.
                return str(message)[:300]
        except Exception:
            pass
        return str(body)[:300]
    return str(getattr(exc, "reason", "") or "")[:300]


# What a probe of the group found. `unknown` is a first-class answer: not
# knowing must never be reported as either of the other two.
GROUP_PRESENT = "present"
GROUP_GONE = "gone"
GROUP_UNKNOWN = "unknown"


def _probe_group(groups, group_id) -> tuple[str, bool]:
    """Ask what is actually true about the group, to read an error correctly.

    Returns (presence, can_invite).

    Only ever called after create_group_invite has already failed, because both
    statuses it disambiguates are genuinely ambiguous:

      * 404 means "no such group" OR "no such user"
      * 403 means "you may not invite" OR "that user will not be invited"

    The alternative is matching English substrings in an error body, which is
    what this replaced. That approach breaks on rewording or localisation, and
    it broke asymmetrically: a permission error mentioning "...cannot invite
    that user" would have been recorded as the MEMBER's refusal, which is
    permanent and never retried. This asks a question with an answer instead.

    UnauthorizedException is caught first and deliberately. It subclasses
    ApiException, so a dead session used to be read as "not a 404, therefore
    the group is present, therefore the member's account is at fault" -- and
    the member was told to re-verify an account that was never the problem.
    That is the exact wrong-attribution bug this branch already fixed once.
    """
    try:
        group = _call_with_retry(
            groups.get_group, group_id, _request_timeout=request_timeout()
        )
    except UnauthorizedException:
        return GROUP_UNKNOWN, False
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            return GROUP_GONE, False
        return GROUP_UNKNOWN, False
    except Exception:
        return GROUP_UNKNOWN, False
    can_invite, _ = _capabilities(_permissions(group))
    return GROUP_PRESENT, can_invite


def claim_code_present(group, claim_code) -> bool:
    """Is the guild's one-time code in this group's description?

    The group-level analogue of the bio code members already paste into their
    VRChat profile, and it exists to answer one question: does the person who
    typed this group id into the dashboard actually run the group? Without it,
    anyone could name a stranger's group and -- if the account happened to be
    invited, or the group were open -- have the bot join it.

    Substring, not whole-line. A group description is prose an admin drops a
    code into, unlike a bio where the code goes on a line of its own.

    Safe against VRChat rewriting the text, which it does: confirmed
    2026-08-19 that `.` `,` and `!` come back as U+2024, U+201A and U+01C3,
    while the hyphen and every letter and digit stay plain ASCII. The code's
    alphabet is exactly those, so it survives the round trip byte for byte.
    That is a constraint on the code, not a coincidence -- a code containing
    punctuation could never match, and the failure would look like the admin
    not having pasted it.

    A missing code is refused rather than waved through: a job with no code is
    a job carrying no proof, which is the case this function exists to stop.
    """
    if not isinstance(claim_code, str) or not claim_code.strip():
        return False
    description = getattr(group, "description", None)
    if not isinstance(description, str):
        return False
    return claim_code.strip() in description


def verify_group_setup(job: dict) -> dict:
    """Join the named group if needed, then report what the account can do.

    The join is the only write this phase performs, and it happens only for
    the group id carried by this job -- see the module docstring.
    """
    group_id = job.get("groupID")
    # Checked before any API call: passing None to the client raises
    # ApiValueError, which is NOT an ApiException (it subclasses ValueError),
    # so it would sail past every handler below and be dropped with no result
    # published -- leaving whoever asked waiting forever.
    if not isinstance(group_id, str) or not group_id.startswith("grp_"):
        return _result(
            job, STATE_BAD_JOB, error_message="That is not a VRChat group ID (they start with grp_)"
        )

    client, session_error = vrchat_session.get()
    if client is None:
        meta = session_error or default_session_error()
        return _result(job, STATE_VRCHAT_UNAVAILABLE, error_message=meta.get("error_message"))

    groups = GroupsApi(client)

    try:
        group = _call_with_retry(groups.get_group, group_id, _request_timeout=request_timeout())
    except UnauthorizedException as e:
        vrchat_session.invalidate(classify_api_error(e))
        return _result(job, STATE_VRCHAT_UNAVAILABLE, error_message="VRChat session expired")
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            return _result(job, STATE_GROUP_NOT_FOUND, error_message="No VRChat group with that ID")
        if getattr(e, "status", None) in {401, 403}:
            # 403 here is "this group will not show itself to us", which for an
            # admin who just typed an ID means the same thing as not found:
            # nothing they can do except check the ID.
            return _result(job, STATE_GROUP_NOT_FOUND, error_message="That VRChat group is not visible to the bot")
        meta = classify_api_error(e)
        return _result(job, STATE_VRCHAT_UNAVAILABLE, error_message=meta.get("error_message"))

    status = _membership_status(group)

    if status in {"banned", "userblocked"}:
        # Terminal. join_group cannot fix it and re-inviting cannot either, so
        # say what actually has to happen.
        return _result(
            job,
            STATE_BANNED,
            **_display(group),
            error_message=(
                "The bot is banned or blocked from this group. A group moderator has to "
                "lift that before setup can continue."
            ),
        )

    if job.get("requireCode", True) and not claim_code_present(
        group, job.get("claimCode")
    ):
        # The ownership proof, checked before this worker joins anything.
        # Confirmed 2026-08-19 that get_group() returns `description` to a
        # non-member, which is what makes a pre-join check possible at all.
        #
        # WHETHER to require it is the bot's call, not a membership test here.
        # "Skip it if we are already a member" looks equivalent and is not:
        # when a guild releases a group and a second guild claims the same id,
        # the account is still in that group, and the shortcut would hand the
        # newcomer a verified setup for somebody else's group without their
        # ever proving anything. The bot requires the code until THIS guild
        # has verified THIS group, which is the question that actually matters.
        #
        # Absent means required. A job that lost the field somewhere must fail
        # closed, because the field's whole purpose is to be hard to bypass.
        return _result(
            job,
            STATE_CODE_MISSING,
            **_display(group),
            error_message=(
                "The setup code is not in the group's description yet. Add it, "
                "then check again."
            ),
        )

    if status != "member":
        # Accepts a pending invite, files a request on a request-to-join group,
        # or joins an open one outright. A closed/invite-only group with no
        # invite waiting refuses, which is the "nobody has invited us yet" case.
        try:
            _call_with_retry(groups.join_group, group_id, _request_timeout=request_timeout())
        except ApiException as e:
            if getattr(e, "status", None) in {400, 403}:
                return _result(
                    job,
                    STATE_NOT_INVITED,
                    **_display(group),
                    error_message="The bot has not been invited to this group yet",
                )
            meta = classify_api_error(e)
            return _result(job, STATE_VRCHAT_UNAVAILABLE, error_message=meta.get("error_message"))

        # Re-read rather than trusting join_group's return: permissions are
        # what we actually need, and get_group is the call documented to carry
        # them for the authenticated account.
        try:
            group = _call_with_retry(groups.get_group, group_id, _request_timeout=request_timeout())
        except ApiException as e:
            meta = classify_api_error(e)
            return _result(job, STATE_VRCHAT_UNAVAILABLE, error_message=meta.get("error_message"))
        status = _membership_status(group)

    if status in {"banned", "userblocked"}:
        return _result(
            job,
            STATE_BANNED,
            **_display(group),
            error_message=(
                "The bot is banned or blocked from this group. A group moderator has to "
                "lift that before setup can continue."
            ),
        )

    if status == "requested":
        # Request-to-join group: a moderator has to approve before anything
        # else can be checked, so this is a wait, not a failure.
        return _result(
            job,
            STATE_JOIN_REQUESTED,
            **_display(group),
            error_message="Join request sent; a group moderator has to approve it",
        )

    if status != "member":
        return _result(
            job,
            STATE_NOT_INVITED,
            **_display(group),
            error_message="The bot has not been invited to this group yet",
        )

    can_invite, can_see_members = _capabilities(_permissions(group))

    if not can_invite:
        return _result(
            job,
            STATE_NO_INVITE_PERMISSION,
            **_display(group),
            can_see_members=can_see_members,
            error_message=(
                "The bot is in the group but cannot send invites. Give its role the "
                "'Manage Group Invites' permission -- being an admin does not include it."
            ),
        )

    return _result(
        job,
        STATE_READY,
        **_display(group),
        can_invite=True,
        can_see_members=can_see_members,
    )


def _leave_result(job: dict, state: str, **extra) -> dict:
    """One leave outcome. Carries `type` so the bot can route it.

    `reason` is echoed straight back, unread. This worker has no opinion about
    why it was asked to leave -- it leaves the group either way -- but the bot
    needs it to tell "the guild lapsed, free its seat" from "the admin changed
    group, leave the old one and change nothing else". Echoing rather than
    storing keeps that decision entirely on the side that made it.
    """
    payload = {
        "type": JOB_LEAVE_GROUP,
        "jobID": job.get("jobID"),
        "guildID": job.get("guildID"),
        "groupID": job.get("groupID"),
        "reason": job.get("reason"),
        "ok": state == LEAVE_DONE,
        "state": state,
        "accountID": INVITE_ACCOUNT_USER_ID,
        "error_message": None,
    }
    payload.update(extra)
    return payload


def _invite_result(job: dict, state: str, **extra) -> dict:
    """One invite outcome, shaped for the bot's result consumer.

    Carries `type` so the consumer can tell it from a setup verdict without
    guessing from which fields happen to be present -- the two travel on the
    same result queue.
    """
    payload = {
        "type": JOB_SEND_INVITE,
        "jobID": job.get("jobID"),
        "guildID": job.get("guildID"),
        "groupID": job.get("groupID"),
        "ok": state == INVITE_SENT,
        "state": state,
        "accountID": INVITE_ACCOUNT_USER_ID,
    }
    payload.update(extra)
    return payload


def _space_invite_calls() -> None:
    """Sleep until enough time has passed since the last invite went out.

    See INVITE_MIN_SPACING_SECONDS. The jitter is on the same reasoning as
    backoff_delay's: a fixed interval is exactly what makes many callers
    synchronise, and VRChat's guidelines call that out by name.
    """
    wait = INVITE_MIN_SPACING_SECONDS + random.uniform(0.0, 0.5)
    elapsed = time.monotonic() - _last_invite_call
    if elapsed < wait:
        time.sleep(wait - elapsed)


def _throttled_invite(groups, group_id, request):
    """One create_group_invite call, spaced from the previous one.

    Passed to _call_with_retry rather than wrapped around it, so that RETRIES
    are spaced too. Spacing the retry loop from the outside only threw the
    throttle away at the exact moment it exists for: _call_with_retry's own
    backoff is shorter than the configured gap, so a 429 -- VRChat telling us
    to slow down -- used to make us speed up.

    The timestamp is taken AFTER the call returns, not before. Stamped before,
    a job whose retries took twelve seconds left the next job measuring its gap
    from the start of that chain, and firing immediately.
    """
    global _last_invite_call
    _space_invite_calls()
    try:
        return groups.create_group_invite(
            group_id, request, _request_timeout=request_timeout()
        )
    finally:
        _last_invite_call = time.monotonic()


def send_group_invite(job: dict) -> dict:
    """Invite one member to one group, if they are not already in it.

    Called only for a member who pressed the opt-in button in their own DM.
    The bot builds this job from the guild's stored, verified configuration --
    the group id never comes from anything a browser or a client sent.

    Membership is checked first rather than inferred from create_group_invite's
    400, because the two "nothing to do" cases need different sentences: being
    a member is finished, and having an invite already waiting is a nudge to go
    look at their notifications. Answering both with "already a member" would
    send people hunting through a group they have not joined.

    That check is an OPTIMISATION, not a gate. Every failure of it falls
    through to the invite, and it is never reported. This is the one part of
    this function that was rewritten twice in production, both times because a
    fix gave get_group_member's 403 a precise meaning from a single
    observation -- first "the target is not in the group", then "the bot is not
    in the group". Neither held: the 403 that prompted them came from a userId
    that resolved to no VRChat account at all, and what else it means is still
    unmeasured.

    So it deliberately does not need to be known. Every authoritative answer
    comes from create_group_invite regardless -- 400 for an existing member,
    403 for a recipient who will not take invites, 404 for a group that has
    gone or a member whose account does not resolve. The check only buys a
    better sentence in the cases it can recognise, and buying nothing is an
    acceptable outcome.

    confirm_override_block is passed as False, EXPLICITLY. It exists to push an
    invite past a user who has blocked the group, which is the exact thing this
    feature must not do -- the member-initiated design is the compliance
    argument for the whole feature, and overriding a block would throw it away.

    The explicitness is not decoration. Confirmed against vrchatapi 1.0.0 on
    2026-08-20: CreateGroupInviteRequest(user_id=...) leaves the field at its
    generated default, which is True. Omitting it therefore opts IN to
    overriding blocks, silently, in the one call where that matters most.
    """
    group_id = job.get("groupID")
    user_id = job.get("vrcUserID")
    # Both checked before any API call, for the reason verify_group_setup
    # gives: the client raises ApiValueError on None, which subclasses
    # ValueError rather than ApiException and would sail past every handler
    # below -- leaving the member watching a message that never updates.
    if not isinstance(group_id, str) or not group_id.startswith("grp_"):
        return _invite_result(job, INVITE_BAD_JOB)
    if not isinstance(user_id, str) or not user_id.startswith("usr_"):
        return _invite_result(job, INVITE_BAD_JOB)

    client, session_error = vrchat_session.get()
    if client is None:
        meta = session_error or default_session_error()
        return _invite_result(
            job, INVITE_VRCHAT_UNAVAILABLE, error_message=meta.get("error_message")
        )

    groups = GroupsApi(client)

    # 1) Where do they stand today? Best effort only -- see the docstring.
    #    Every failure here leaves status as None, which means "go and try the
    #    invite".
    status = None
    try:
        member = _call_with_retry(
            groups.get_group_member,
            group_id,
            user_id,
            _request_timeout=request_timeout(),
        )
        status = _membership_status(member) if member is not None else None
    except UnauthorizedException as e:
        # The one failure here that is NOT best effort. A dead session means
        # the invite cannot work either, and pressing on would spend a second
        # call finding that out.
        vrchat_session.invalidate(classify_api_error(e))
        return _invite_result(
            job, INVITE_VRCHAT_UNAVAILABLE, error_message="VRChat session expired"
        )
    except ApiException as e:
        logging.info(
            "Could not read %s's membership of %s (status=%s: %s); attempting "
            "the invite anyway",
            user_id,
            group_id,
            getattr(e, "status", None),
            _api_detail(e),
        )

    if status == "member":
        return _invite_result(job, INVITE_ALREADY_MEMBER)
    if status == "invited":
        return _invite_result(job, INVITE_ALREADY_INVITED)
    if status == "banned":
        return _invite_result(job, INVITE_BANNED)
    if status == "userblocked":
        # A distinct value in GroupMemberStatus, and not a moderator ban. The
        # banned copy tells the member "only a group moderator can change
        # that", which for a block they placed themselves is simply untrue --
        # the blocked copy already names the case correctly.
        return _invite_result(job, INVITE_BLOCKED)
    if status == "requested":
        # They have asked to join under their own steam. An invite would cut
        # across a moderator's pending decision, so leave it alone and say so.
        return _invite_result(job, INVITE_ALREADY_INVITED)

    # 2) Nothing waiting for them, so send one.
    try:
        _call_with_retry(
            _throttled_invite,
            groups,
            group_id,
            # See the docstring: False is NOT this field's default.
            CreateGroupInviteRequest(user_id=user_id, confirm_override_block=False),
        )
    except UnauthorizedException as e:
        vrchat_session.invalidate(classify_api_error(e))
        return _invite_result(
            job, INVITE_VRCHAT_UNAVAILABLE, error_message="VRChat session expired"
        )
    except ApiException as e:
        code = getattr(e, "status", None)
        detail = _api_detail(e)
        # Logged before it is classified, and at WARNING, because every state
        # below is OUR reading of somebody else's error. When the reading is
        # wrong -- and it has been, twice -- this line is the only record of
        # what VRChat actually said.
        logging.warning(
            "create_group_invite for %s into %s failed (status=%s: %s)",
            user_id,
            group_id,
            code,
            detail,
        )
        if code == 400:
            # "User X is already a member of this group" -- they joined in the
            # seconds since the check above. Reported as what it is.
            return _invite_result(job, INVITE_ALREADY_MEMBER, error_message=detail)
        if code in {403, 404}:
            # Both are ambiguous, and both are resolved the same way: ask what
            # is actually true about the group. See _probe_group. One extra
            # call, only on a path that has already failed, and only where the
            # readings need opposite advice -- "re-verify" versus "go and find
            # an admin" -- or differ in whether they are permanent.
            presence, can_invite = _probe_group(groups, group_id)
            if presence == GROUP_UNKNOWN:
                # Not knowing is reported as not knowing. Every other answer
                # here blames somebody, and two of them stick.
                return _invite_result(
                    job, INVITE_VRCHAT_UNAVAILABLE, error_message=detail
                )
            if presence == GROUP_GONE:
                return _invite_result(
                    job, INVITE_GROUP_NOT_FOUND, error_message=detail
                )
            # The group is there and we can see it.
            if code == 404:
                # So the thing that could not be found is the member.
                return _invite_result(
                    job, INVITE_USER_NOT_FOUND, error_message=detail
                )
            if can_invite:
                # We still hold the permission, so the refusal is about the
                # recipient: they have group invites off, or have blocked us.
                # Their answer, recorded, never retried and never overridden.
                return _invite_result(job, INVITE_BLOCKED, error_message=detail)
            return _invite_result(
                job,
                INVITE_NO_PERMISSION,
                error_message=detail
                or "The bot is not allowed to invite people to this group",
            )
        meta = classify_api_error(e)
        return _invite_result(
            job, INVITE_VRCHAT_UNAVAILABLE, error_message=meta.get("error_message")
        )

    return _invite_result(job, INVITE_SENT)


def leave_group(job: dict) -> dict:
    """Take this account out of one group, freeing the seat it was holding.

    The mirror of the join in verify_group_setup, and the only other write this
    worker performs. Same rule applies: it leaves the group this job names and
    nothing else, and the bot builds the job from its own stored lease rather
    than from anything a browser sent.

    Already being out of the group counts as success. A 404 is "no such group"
    or "not a member of it", and in both cases the thing the caller wanted --
    this account not holding a seat there -- is already true. Reporting a
    failure would leave the seat pinned by a group that does not exist.
    """
    group_id = job.get("groupID")
    if not isinstance(group_id, str) or not group_id.startswith("grp_"):
        # Same reasoning as the other handlers: the client raises
        # ApiValueError on None, which is not an ApiException and would escape
        # every handler here.
        return _leave_result(job, LEAVE_FAILED, error_message="Not a group ID")

    client, session_error = vrchat_session.get()
    if client is None:
        meta = session_error or default_session_error()
        return _leave_result(
            job, LEAVE_FAILED, error_message=meta.get("error_message")
        )

    groups = GroupsApi(client)
    try:
        _call_with_retry(
            groups.leave_group, group_id, _request_timeout=request_timeout()
        )
    except UnauthorizedException as e:
        vrchat_session.invalidate(classify_api_error(e))
        return _leave_result(job, LEAVE_FAILED, error_message="VRChat session expired")
    except ApiException as e:
        code = getattr(e, "status", None)
        detail = _api_detail(e)
        if code == 404:
            logging.info(
                "Nothing to leave for %s (404: %s); treating as done",
                group_id,
                detail,
            )
            return _leave_result(job, LEAVE_DONE)
        logging.warning(
            "Could not leave %s (status=%s: %s)", group_id, code, detail
        )
        return _leave_result(job, LEAVE_FAILED, error_message=detail)

    logging.info("Left group %s; its seat is free", group_id)
    return _leave_result(job, LEAVE_DONE)


HANDLERS = {
    JOB_VERIFY_SETUP: verify_group_setup,
    JOB_SEND_INVITE: send_group_invite,
    JOB_LEAVE_GROUP: leave_group,
}


def publish_result(result: dict):
    """Publish one job outcome back for the bot to persist."""
    body = json.dumps(result)
    properties = pika.BasicProperties(content_type="application/json", delivery_mode=2)

    max_publish_tries = int(os.getenv("RABBITMQ_PUBLISH_TRIES", "3"))
    last_exc = None
    for attempt in range(1, max_publish_tries + 1):
        connection = None
        try:
            connection = _rabbitmq_connect_with_retry(max_tries=1)
            channel = connection.channel()
            channel.queue_declare(queue=RESULT_QUEUE_NAME, durable=True)
            channel.basic_publish(
                exchange="", routing_key=RESULT_QUEUE_NAME, body=body, properties=properties
            )
            # Summarised, not dumped. error_message carries VRChat's own prose,
            # and VRChat names the user in it -- "User usr_... is already a
            # member of this group". Logging the whole payload put a usr_ id
            # and a guild id on one line, which is most of the Discord-to-
            # VRChat mapping this project deliberately refuses to store.
            #
            # Also no longer says "group-setup" for an invite. Two job types
            # share this queue now.
            logging.info(
                "Sent %s result to '%s': job=%s guild=%s state=%s",
                result.get("type"),
                RESULT_QUEUE_NAME,
                result.get("jobID"),
                result.get("guildID"),
                result.get("state"),
            )
            return
        except AMQPError as e:
            last_exc = e
            logging.warning(
                "RabbitMQ result publish failed (attempt %s/%s); retrying...",
                attempt, max_publish_tries, exc_info=True,
            )
            time.sleep(min(10.0, 1.5 * attempt))
        finally:
            try:
                if connection and connection.is_open:
                    connection.close()
            except Exception:
                pass
    # Raised, not swallowed. Returning here ACKed the job with no result ever
    # delivered: the join had already happened and whoever asked waited
    # forever. Raising lets process_job requeue it, and re-running a
    # verification is harmless -- it re-reads state it already established.
    logging.error("RabbitMQ result publish failed after retries; requeueing the job", exc_info=last_exc)
    raise last_exc or AMQPError("result publish failed")


def process_job(ch, method, properties, body):
    """RabbitMQ callback: handle one job, publish the outcome, then ACK/NACK."""
    try:
        job = json.loads(body)
    except Exception:
        logging.exception("Invalid JSON body; dropping message")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    # `5`, `"text"`, `[1, 2]` and `null` are all valid JSON and none of them
    # has .get(). Without this the AttributeError escapes process_job entirely
    # -- outside both try blocks below -- so the message is neither acked nor
    # nacked, start_consuming unwinds, listen_for_jobs reconnects, and the
    # broker redelivers the same message for ever with every invite and setup
    # job in the queue stuck behind it.
    if not isinstance(job, dict):
        logging.error("Job body is %s, not an object; dropping", type(job).__name__)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    handler = HANDLERS.get(job.get("type"))
    if handler is None:
        # An unknown type is a message we will never understand, so requeueing
        # it would wedge the queue behind something no version of this worker
        # can process.
        logging.error("Unknown job type %r; dropping message", job.get("type"))
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    # Redelivery is capped for BOTH failure kinds below, and that cap is not
    # tidiness -- it is the only thing bounding how many invites one button
    # press can send.
    #
    # handler(job) runs before publish_result, so every redelivery re-runs the
    # handler. For verify_group_setup that is harmless: it re-reads state it
    # already established, which is what publish_result's comment argues and
    # was true when this file had one job type. send_group_invite is a WRITE to
    # somebody else's platform. A persistent publish failure -- a durable-flag
    # mismatch on the result queue, a vhost permission change -- used to loop
    # here forever, sending a real invite on every pass, which is precisely the
    # spam pattern this whole feature is designed not to produce.
    already_retried = bool(getattr(method, "redelivered", False))
    try:
        publish_result(handler(job))
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except AMQPError:
        # The broker is the thing that is broken, so there is nowhere to report
        # this. Dropping leaves the bot's row pending; its read-side timeout
        # expires it and the member is told it did not work. That is the right
        # trade against an unbounded invite loop.
        logging.exception(
            "RabbitMQ publish failed; %s",
            "dropping (already retried once)" if already_retried else "requeueing once",
        )
        ch.basic_nack(
            delivery_tag=method.delivery_tag, requeue=not already_retried
        )
    except Exception:
        # Requeue at most once, for the same reason the checker does: an
        # unconditional requeue turns any unhandled bug into an unbounded
        # redelivery loop that stalls every other guild's setup.
        logging.exception(
            "Unexpected error processing job; %s",
            "dropping (already retried once)" if already_retried else "requeueing once",
        )
        if already_retried:
            # About to drop this for good, so say so rather than leaving
            # whoever asked waiting on an answer that is never coming. Best
            # effort: if this publish fails too there is nothing left to try.
            #
            # Shaped as the same KIND of result the job asked for. Both types
            # travel on one result queue and the bot routes on `type`, so a
            # setup-shaped apology for a failed invite would be filed against
            # the guild's setup row and the member's DM would hang forever.
            failed = job if isinstance(job, dict) else {}
            try:
                if failed.get("type") == JOB_LEAVE_GROUP:
                    publish_result(
                        _leave_result(
                            failed,
                            LEAVE_FAILED,
                            error_message="The leave failed unexpectedly.",
                        )
                    )
                elif failed.get("type") == JOB_SEND_INVITE:
                    publish_result(
                        _invite_result(
                            failed,
                            INVITE_VRCHAT_UNAVAILABLE,
                            error_message="The invite failed unexpectedly.",
                        )
                    )
                else:
                    publish_result(
                        _result(
                            failed,
                            STATE_VRCHAT_UNAVAILABLE,
                            error_message="The setup check failed unexpectedly. Please try again.",
                        )
                    )
            except Exception:
                logging.exception("Could not report the failure either; dropping silently")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=not already_retried)


def listen_for_jobs():
    """Blocking consume loop, reconnecting on broker failures."""
    while True:
        connection = None
        try:
            connection = _rabbitmq_connect_with_retry(max_tries=0)
            channel = connection.channel()
            channel.queue_declare(queue=REQUEST_QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=REQUEST_QUEUE_NAME, on_message_callback=process_job)
            logging.info("Listening for group-invite jobs on '%s'...", REQUEST_QUEUE_NAME)
            channel.start_consuming()
        except (pika.exceptions.AMQPConnectionError, pika.exceptions.StreamLostError, OSError):
            logging.warning("RabbitMQ consumer disconnected; reconnecting soon...", exc_info=True)
            time.sleep(3)
        except Exception:
            logging.exception("Unexpected error in RabbitMQ consume loop; restarting")
            time.sleep(3)
        finally:
            try:
                if connection and connection.is_open:
                    connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    if not is_configured():
        # Exits 0 rather than looping: the feature simply is not provisioned
        # yet, which is not a crash. Compose runs this service with
        # restart: on-failure so a clean exit stays exited instead of
        # restart-looping a container that has nothing to do.
        logging.error(
            "INVITE_VRCHAT_USERNAME/PASSWORD are unset, so the group-invite worker "
            "has no account to run as. Exiting; nothing else is affected."
        )
        raise SystemExit(0)

    check_session_store_writable(INVITE_ACCOUNT)

    logging.info("Attempting initial VRChat login as the invite account")
    vrchat_session.attempt_login(force=True)
    if not vrchat_session.client:
        logging.error("Initial VRChat login failed. Jobs will report vrchat_unavailable until it recovers.")

    vrchat_session.start_relogin_thread()

    listen_for_jobs()
