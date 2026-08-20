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

This phase handles setup verification only. Sending invites arrives with the
member-facing opt-in button.
"""

import json
import logging
import os
import time

import pika
from dotenv import load_dotenv
from pika.exceptions import AMQPError
from vrchatapi.api.groups_api import GroupsApi
from vrchatapi.exceptions import ApiException, UnauthorizedException

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

VRCHAT_CALL_RETRIES = max(1, int(os.getenv("INVITE_CALL_RETRIES", "3")))

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


HANDLERS = {JOB_VERIFY_SETUP: verify_group_setup}


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
            logging.info("Sent group-setup result to '%s': %s", RESULT_QUEUE_NAME, body)
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

    handler = HANDLERS.get(job.get("type"))
    if handler is None:
        # An unknown type is a message we will never understand, so requeueing
        # it would wedge the queue behind something no version of this worker
        # can process.
        logging.error("Unknown job type %r; dropping message", job.get("type"))
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    try:
        publish_result(handler(job))
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except AMQPError:
        logging.exception("RabbitMQ publish failed; requeueing job")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    except Exception:
        # Requeue at most once, for the same reason the checker does: an
        # unconditional requeue turns any unhandled bug into an unbounded
        # redelivery loop that stalls every other guild's setup.
        already_retried = bool(getattr(method, "redelivered", False))
        logging.exception(
            "Unexpected error processing job; %s",
            "dropping (already retried once)" if already_retried else "requeueing once",
        )
        if already_retried:
            # About to drop this for good, so say so rather than leaving the
            # dashboard waiting on an answer that is never coming. Best
            # effort: if this publish fails too there is nothing left to try.
            try:
                publish_result(
                    _result(
                        job if isinstance(job, dict) else {},
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
