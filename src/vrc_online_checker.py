import time
import os
import json
import pika
import logging
from dotenv import load_dotenv
from pika.exceptions import AMQPError

# VRChat API imports
import vrchatapi
from vrchatapi.api import users_api
from vrchatapi.exceptions import UnauthorizedException, ApiException

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

# -------------------------------------------------------------------
# Load environment variables
# -------------------------------------------------------------------
load_dotenv()

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")
RABBITMQ_QUEUE_NAME = os.getenv("RABBITMQ_QUEUE_NAME")
RESULT_QUEUE_NAME = os.getenv("RABBITMQ_RESULT_QUEUE")

# How this bot identifies itself to VRChat on every request.
#
# VRChat's Creator Guidelines require the "applicationName/Version contactInfo"
# form and state that failing to identify yourself, or identifying yourself
# improperly, results in moderation action — so the contact address has to be
# one a VRChat moderator can actually reach us at. It was a placeholder
# (contact@yourdomain.com) until 2026-08-18; do not let it become one again.
VRCHAT_USER_AGENT = "VRCVerifyBot/1.0 contact@esattotech.com"

# This service's VRChat login. The session machinery itself lives in
# vrc_session so the group-invite bot (issue #49) can run a second account
# without a second copy of it; everything here still talks to this one.
CHECKER_ACCOUNT = VRChatAccount.from_env(user_agent=VRCHAT_USER_AGENT, label="age-check")
vrchat_session = VRChatSession(CHECKER_ACCOUNT)

# Priority levels on the request queue, so premium servers are served ahead of
# free ones when a backlog forms.
#
# NOT an env var, deliberately, and it MUST stay identical to bot.py's
# QUEUE_MAX_PRIORITY. Both services declare this queue, and RabbitMQ rejects a
# declare whose arguments differ from the existing queue's with 406
# PRECONDITION_FAILED — which would take down publishing and consuming at the
# same time. Changing the value is a migration (delete and recreate the queue),
# not a config tweak. tests/test_priority_queue.py pins that they agree.
QUEUE_MAX_PRIORITY = 5

# RabbitMQ's reply code for "this queue already exists with other arguments".
QUEUE_ARGUMENT_MISMATCH_CODE = 406


def request_queue_arguments() -> dict:
    """Declaration arguments for the verification request queue.

    Must return exactly what bot.py's function of the same name returns.
    """
    return {"x-max-priority": QUEUE_MAX_PRIORITY}


def is_queue_argument_mismatch(error: Exception) -> bool:
    """Is this the 406 you get from re-declaring a queue with new arguments?"""
    return getattr(error, "reply_code", None) == QUEUE_ARGUMENT_MISMATCH_CODE

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level_str, logging.INFO)

# -------------------------------------------------------------------
# Logging configuration
# -------------------------------------------------------------------
logging.basicConfig(
    level=numeric_level,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("pika").setLevel(logging.WARNING)

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
            logging.warning("RabbitMQ connection failed; retrying in %.1fs (attempt %s)", delay, attempt)
            time.sleep(delay)


# -------------------------------------------------------------------
# VRChat status page / outage helpers
# -------------------------------------------------------------------
# Floor of 1: this counts total attempts, not extra retries. At 0 the retry
# loops would fall through without ever calling VRChat, reporting every user
# as "code not found" with lookup_ok=True -- a silent, misdiagnosable failure.
VRCHAT_LOOKUP_RETRIES = max(1, int(os.getenv("VRCHAT_LOOKUP_RETRIES", "3")))
def _result_payload(discord_id, vrc_user_id, guild_id, verification_code, **extra):
    payload = {
        "discordID": discord_id,
        "vrcUserID": vrc_user_id,
        "guildID": guild_id,
        "is_18_plus": False,
        "verificationCode": verification_code,
        "code_found": False,
        "display_name": None,
        "lookup_ok": True,
        "error_type": None,
        "error_message": None,
        "vrchat_outage": False,
        "vrchat_outage_confirmed": False,
        "vrchat_status_message": None,
        "vrchat_status_indicator": None,
        "vrchat_status_page": "https://status.vrchat.com/",
    }
    payload.update(extra)
    return payload


def get_vrchat_session() -> tuple[vrchatapi.ApiClient | None, dict | None]:
    """The live session for this service's account."""
    return vrchat_session.get()


def invalidate_vrchat_session(error_meta: dict | None = None):
    """Drop the session and let the background thread log back in."""
    vrchat_session.invalidate(error_meta)


def _get_vrchat_user_with_retry(users_api_instance, vrc_user_id: str):
    last_exc = None
    timeout = request_timeout()
    attempts = max(1, VRCHAT_LOOKUP_RETRIES)  # never skip the call entirely
    for attempt in range(1, attempts + 1):
        try:
            return users_api_instance.get_user(vrc_user_id, _request_timeout=timeout)
        except ApiException as e:
            last_exc = e
            status = getattr(e, "status", None)
            if status not in TRANSIENT_HTTP_STATUSES:
                raise
            if attempt >= attempts:
                raise
            delay = backoff_delay(attempt)
            logging.warning(
                "Transient VRChat get_user failure for %s (status=%s). Retrying in %.2fs (attempt %s/%s)",
                vrc_user_id,
                status,
                delay,
                attempt,
                attempts,
            )
            time.sleep(delay)
        except Exception as e:
            last_exc = e
            if attempt >= attempts:
                raise
            delay = backoff_delay(attempt)
            logging.warning(
                "Transient VRChat get_user failure for %s. Retrying in %.2fs (attempt %s/%s)",
                vrc_user_id, delay, attempt, attempts, exc_info=True
            )
            time.sleep(delay)
    # Returning None here would silently become bio="" / age="unknown", i.e.
    # a false "code not found" with lookup_ok=True. Fail loudly instead.
    raise last_exc or RuntimeError("VRChat get_user retry loop exited without a result")


# -------------------------------------------------------------------
# Profile lookup
#
# As of 2026-07-25 VRChat split the profile read path: GET /users/{id} can
# serve a bio that is hours out of date (measured: a bio edit still missing
# after 20+ minutes, across 15 distinct API backends, with no caching in
# play) while GET /profile/{id} reflects the edit immediately. Verification
# reads the bio, so we must use /profile/{id}.
#
# That endpoint is not in the OpenAPI spec yet, so vrchatapi has no model for
# it and we call it raw. Because it is undocumented it could change shape or
# disappear without notice, so every failure falls back to /users/{id}: a
# stale bio only costs a retry, but a hard failure blocks verification.
# Set VRCHAT_USE_PROFILE_ENDPOINT=false to force the old behaviour.
# -------------------------------------------------------------------
VRCHAT_USE_PROFILE_ENDPOINT = os.getenv(
    "VRCHAT_USE_PROFILE_ENDPOINT", "true"
).strip().lower() not in {"0", "false", "no", "off"}


def _fetch_vrchat_profile(client, vrc_user_id: str) -> dict:
    """GET /profile/{userId} and return the decoded JSON body."""
    raw = client.call_api(
        "/profile/{userId}", "GET",
        {"userId": vrc_user_id},
        [],
        {"Accept": "application/json"},
        body=None,
        post_params=[],
        files={},
        response_types_map={},
        auth_settings=["authCookie"],
        async_req=False,
        _return_http_data_only=True,
        _preload_content=False,  # no generated model exists; decode by hand
        _request_timeout=request_timeout(),
        collection_formats={},
    )
    payload = json.loads(raw.data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected /profile payload: {type(payload).__name__}")
    return payload


def _get_vrchat_profile_with_retry(client, vrc_user_id: str) -> dict:
    """Fetch /profile/{id}, retrying only transient upstream failures.

    Anything else raises immediately so the caller can fall back to
    /users/{id} without first stacking retry delays on a permanent error.
    """
    attempts = max(1, VRCHAT_LOOKUP_RETRIES)  # never skip the call entirely
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_vrchat_profile(client, vrc_user_id)
        except ApiException as e:
            status = getattr(e, "status", None)
            if status not in TRANSIENT_HTTP_STATUSES or attempt >= attempts:
                raise
            delay = backoff_delay(attempt)
            logging.warning(
                "Transient VRChat /profile failure for %s (status=%s). Retrying in %.2fs (attempt %s/%s)",
                vrc_user_id,
                status,
                delay,
                attempt,
                attempts,
            )
            time.sleep(delay)
    # Only reachable if the loop body never ran, which max(1, ...) prevents.
    raise RuntimeError("VRChat /profile retry loop exited without a result")


def fetch_profile_snapshot(client, vrc_user_id: str) -> tuple[str, str, str | None, str]:
    """Return (bio, age_status, display_name, source) for a VRChat user.

    Reads /profile/{id} first since it is the only endpoint currently
    reflecting recent bio edits, falling back to /users/{id} when the newer
    endpoint fails or omits a field we need. UnauthorizedException is never
    swallowed: it means the session is dead and the caller must handle it.
    """
    if VRCHAT_USE_PROFILE_ENDPOINT:
        profile = None
        try:
            profile = _get_vrchat_profile_with_retry(client, vrc_user_id)
        except UnauthorizedException:
            raise
        except Exception:
            logging.warning(
                "VRChat /profile lookup failed for %s; falling back to /users",
                vrc_user_id,
                exc_info=True,
            )

        if profile is not None:
            bio = profile.get("bio")
            age_status = profile.get("ageVerificationStatus")
            display_name = profile.get("displayName")
            # /profile is undocumented and parsed as raw JSON, so validate
            # TYPES, not just presence. A non-str bio would otherwise reach
            # bio_contains_code and raise, and that exception is turned into
            # nack(requeue=True) upstream -- an infinite redelivery loop that
            # wedges the whole queue. Falling back is always cheaper.
            if isinstance(bio, str) and isinstance(age_status, str):
                return (
                    bio,
                    age_status,
                    display_name if isinstance(display_name, str) else None,
                    "profile",
                )
            logging.warning(
                "VRChat /profile for %s has unusable fields "
                "(bio=%s, ageVerificationStatus=%s); falling back to /users",
                vrc_user_id,
                type(bio).__name__,
                type(age_status).__name__,
            )

    vrc_user = _get_vrchat_user_with_retry(users_api.UsersApi(client), vrc_user_id)
    return (
        getattr(vrc_user, "bio", "") or "",
        getattr(vrc_user, "age_verification_status", "unknown"),
        getattr(vrc_user, "display_name", None),
        "users",
    )


# -------------------------------------------------------------------
# Verification Logic
# -------------------------------------------------------------------
def bio_contains_code(bio: str | None, verification_code: str) -> bool:
    """True if the verification code appears on its own line in the bio.

    Defensive about both arguments: the bio can come from an undocumented
    endpoint parsed as raw JSON, and the code arrives over RabbitMQ, so
    neither is guaranteed to be a string. Returning False (fail-closed)
    beats raising, because callers turn exceptions into requeued messages.
    """
    if not isinstance(bio, str) or not isinstance(verification_code, str):
        return False
    stripped_code = verification_code.strip()
    for line in bio.splitlines():
        if stripped_code == line.strip():
            return True
    return False


def process_verification_request(ch, method, properties, body):
    """RabbitMQ callback: verify, publish result, then ACK/NACK."""
    try:
        data = json.loads(body)
    except Exception:
        logging.exception("Invalid JSON body; dropping message")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    discord_id = data.get("discordID")
    vrc_user_id = data.get("vrcUserID")
    guild_id = data.get("guildID")
    verification_code = data.get("verificationCode")
    update_nickname = data.get("updateNickname", False)

    logging.info("Received verification request: %s", data)

    try:
        # If verification_code is None => "re-check"
        # If not None => "new code" approach
        result = verify_and_build_result(
            discord_id=discord_id,
            vrc_user_id=vrc_user_id,
            guild_id=guild_id,
            verification_code=verification_code,
        )

        if update_nickname:
            result["updateNickname"] = True

        send_verification_result(result)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except pika.exceptions.AMQPError:
        # Broker/network issue while publishing; retry later.
        logging.exception("RabbitMQ publish failed; requeueing request")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    except Exception:
        # Requeue at most once. An unconditional requeue turns any unhandled
        # bug into an unbounded redelivery loop, and with prefetch_count=1
        # that single message stalls verification for every server. Retrying
        # once covers transient faults; a second identical failure means the
        # message itself is poison, so drop it rather than wedge the queue.
        # (A dead-letter queue would be the better home for these.)
        already_retried = bool(getattr(method, "redelivered", False))
        logging.exception(
            "Unexpected error processing request; %s",
            "dropping (already retried once)" if already_retried else "requeueing once",
        )
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=not already_retried)


def verify_and_build_result(discord_id, vrc_user_id, guild_id, verification_code):
    """
    Queries VRChat to determine if the user is 18+ and/or if the verification code is present in their bio.
    Returns a dictionary with:
      - "is_18_plus"
      - "code_found" (bool)
      - "verificationCode"
      - outage / lookup metadata when VRChat is unhealthy
    """
    client, session_error = get_vrchat_session()
    if not client:
        logging.error("VRChat session not active. Failing verification.")
        meta = session_error or default_session_error()
        return _result_payload(
            discord_id,
            vrc_user_id,
            guild_id,
            verification_code,
            **meta,
        )

    try:
        bio, age_status, display_name, source = fetch_profile_snapshot(client, vrc_user_id)
    except UnauthorizedException as e:
        logging.warning("VRChat session unauthorized; deferring relogin to background worker")
        meta = classify_api_error(e)
        invalidate_vrchat_session(meta)
        return _result_payload(
            discord_id,
            vrc_user_id,
            guild_id,
            verification_code,
            **meta,
        )
    except ApiException as e:
        logging.error("Failed to fetch VRChat user %s. Error: %s", vrc_user_id, e)
        if getattr(e, "status", None) in {401, 403}:
            invalidate_vrchat_session(classify_api_error(e))
        return _result_payload(
            discord_id,
            vrc_user_id,
            guild_id,
            verification_code,
            **classify_api_error(e),
        )
    except Exception as e:
        logging.error("Unexpected failure while fetching VRChat user %s. Error: %s", vrc_user_id, e)
        return _result_payload(
            discord_id,
            vrc_user_id,
            guild_id,
            verification_code,
            **classify_api_error(e),
        )

    # Do NOT swap this for /profile's `ageVerified` boolean: they disagree.
    # A live user was observed with ageVerificationStatus="hidden" but
    # ageVerified=true, so trusting the boolean would verify users VRChat
    # has not age-verified. Both endpoints use this same status vocabulary
    # ("18+", "hidden", "none"), verified across users of each kind.
    is_18_plus = age_status == "18+"

    code_found = False
    if verification_code is not None:
        code_found = bio_contains_code(bio, verification_code)

    # Bios are third-party PII; keep them out of INFO logs (full bio at DEBUG only).
    # 'source' shows which endpoint answered, so a silent slide back onto the
    # stale /users/ bio is visible in the logs rather than mysterious.
    logging.info(
        "[verify_and_build_result] user=%s, age_status=%s, code_expected=%s, code_found=%s, source=%s",
        vrc_user_id,
        age_status,
        verification_code is not None,
        code_found,
        source,
    )
    logging.debug("[verify_and_build_result] user=%s bio=%r", vrc_user_id, bio)

    return _result_payload(
        discord_id,
        vrc_user_id,
        guild_id,
        verification_code,
        is_18_plus=is_18_plus,
        code_found=code_found,
        display_name=display_name,
    )


def send_verification_result(result: dict):
    """Publish the verification result to the bot's queue."""
    message_str = json.dumps(result)
    properties = pika.BasicProperties(
        content_type="application/json",
        delivery_mode=2,  # persistent
    )

    max_publish_tries = int(os.getenv("RABBITMQ_PUBLISH_TRIES", "3"))
    last_exc: Exception | None = None
    for attempt in range(1, max_publish_tries + 1):
        connection = None
        try:
            connection = _rabbitmq_connect_with_retry(max_tries=1)
            channel = connection.channel()
            channel.queue_declare(queue=RESULT_QUEUE_NAME, durable=True)
            channel.basic_publish(
                exchange="",
                routing_key=RESULT_QUEUE_NAME,
                body=message_str,
                properties=properties,
            )
            logging.info("Sent verification result to '%s': %s", RESULT_QUEUE_NAME, message_str)
            return
        except AMQPError as e:
            last_exc = e
            logging.warning(
                "RabbitMQ result publish failed (attempt %s/%s); retrying...",
                attempt,
                max_publish_tries,
                exc_info=True,
            )
            time.sleep(min(10.0, 1.5 * attempt))
        finally:
            try:
                if connection and connection.is_open:
                    connection.close()
            except Exception:
                pass

    logging.error("RabbitMQ result publish failed after retries; giving up", exc_info=last_exc)


def listen_for_verifications():
    """Blocking function that listens for new requests from the bot."""
    if not vrchat_session.client:
        logging.warning("VRChat login was not successful. We might fail all requests.")
    while True:
        connection = None
        try:
            connection = _rabbitmq_connect_with_retry(max_tries=0)
            channel = connection.channel()
            channel.queue_declare(
                queue=RABBITMQ_QUEUE_NAME,
                durable=True,
                arguments=request_queue_arguments(),
            )
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(
                queue=RABBITMQ_QUEUE_NAME,
                on_message_callback=process_verification_request,
                auto_ack=False,
            )
            logging.info("Listening for verification requests on '%s'...", RABBITMQ_QUEUE_NAME)
            channel.start_consuming()
        except pika.exceptions.ChannelClosedByBroker as error:
            if not is_queue_argument_mismatch(error):
                raise
            # Reconnecting cannot fix this — the queue's arguments will not
            # change on their own — and the generic handler below would retry
            # forever without ever explaining why. Say what is wrong and how to
            # fix it, then back off hard so the log is readable.
            logging.error(
                "Queue '%s' already exists with different arguments, so it cannot be "
                "declared with priority support (x-max-priority=%s). Verification is "
                "STOPPED until this is fixed. Stop the bot, let this service drain the "
                "queue to zero, stop it, delete the '%s' queue in RabbitMQ, then start "
                "both again.",
                RABBITMQ_QUEUE_NAME,
                QUEUE_MAX_PRIORITY,
                RABBITMQ_QUEUE_NAME,
            )
            time.sleep(60)
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


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    check_session_store_writable(CHECKER_ACCOUNT)

    logging.info("Attempting initial VRChat login")
    vrchat_session.attempt_login(force=True)
    if not get_vrchat_session()[0]:
        logging.error("Initial VRChat login failed. Continuing to serve queue with outage-aware responses.")

    vrchat_session.start_relogin_thread()

    listen_for_verifications()
