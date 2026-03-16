import time
import imaplib
import re
import os
import json
import pika
import logging
import random
import threading
from urllib import request as urllib_request, error as urllib_error
from dotenv import load_dotenv
from pika.exceptions import AMQPError

# VRChat API imports
import vrchatapi
from vrchatapi.api import authentication_api, users_api
from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode
from vrchatapi.exceptions import UnauthorizedException, ApiException

# -------------------------------------------------------------------
# Load environment variables
# -------------------------------------------------------------------
load_dotenv()

VRCHAT_USERNAME = os.getenv("VRCHAT_USERNAME")
VRCHAT_PASSWORD = os.getenv("VRCHAT_PASSWORD")

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")
RABBITMQ_QUEUE_NAME = os.getenv("RABBITMQ_QUEUE_NAME")
RESULT_QUEUE_NAME = os.getenv("RABBITMQ_RESULT_QUEUE")

# Gmail IMAP Credentials
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

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
# Function to Fetch 2FA Code from Gmail
# -------------------------------------------------------------------
def fetch_latest_2fa_code():
    """Waits for VRChat's 2FA email and retrieves the code from the subject line."""
    logging.info("Waiting 10 seconds for 2FA email to arrive...")
    time.sleep(10)  # Initial wait to allow the email to arrive

    retries = 3  # Number of times to retry
    wait_time = 5  # Seconds between retries

    for attempt in range(retries):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("inbox")

            logging.info("Checking for VRChat 2FA email (Attempt %d/%d)...", attempt + 1, retries)

            # Search for the latest VRChat 2FA email
            status, messages = mail.search(None, 'FROM "noreply@vrchat.com"')

            if not messages[0]:
                logging.warning("No VRChat 2FA emails found yet.")
                time.sleep(wait_time)  # Wait before retrying
                continue

            latest_email_id = messages[0].split()[-1]  # Get the most recent email
            status, data = mail.fetch(latest_email_id, "(BODY[HEADER.FIELDS (SUBJECT)])")

            # Extract subject
            raw_subject = data[0][1].decode()
            subject_match = re.search(r"Your One-Time Code is (\d{6})", raw_subject)

            if subject_match:
                vrchat_2fa_code = subject_match.group(1)
                logging.info("Found VRChat 2FA Code: %s", vrchat_2fa_code)
                return vrchat_2fa_code

            logging.warning("No 2FA code found in email subject. Retrying...")
            time.sleep(wait_time)  # Wait before retrying

        except Exception as e:
            logging.error("Error fetching 2FA code from Gmail: %s", e)

        finally:
            try:
                mail.logout()
            except Exception:
                pass  # Ignore errors when logging out

    logging.error("Failed to retrieve VRChat 2FA code after multiple attempts.")
    return None


# -------------------------------------------------------------------
# VRChat Login with Auto 2FA
# -------------------------------------------------------------------
def login_to_vrchat():
    """Logs into VRChat and handles possible 2FA prompts automatically.

    Returns:
        tuple[vrchatapi.ApiClient | None, dict | None]:
            (client, error_meta). When login succeeds, error_meta is None.
            When login fails, client is None and error_meta contains structured
            outage/auth metadata that can be sent back to users.
    """
    configuration = vrchatapi.Configuration(
        username=VRCHAT_USERNAME, password=VRCHAT_PASSWORD
    )

    api_client = vrchatapi.ApiClient(configuration)
    api_client.user_agent = "VRCVerifyBot/1.0 (contact@yourdomain.com)"
    auth_api = authentication_api.AuthenticationApi(api_client)

    try:
        current_user = auth_api.get_current_user()
        logging.info("Successfully logged in as %s", current_user.display_name)
        return api_client, None

    except UnauthorizedException as e:
        if e.status == 200:
            logging.info("2FA Required! Fetching code from email...")

            # Auto-fetch the 2FA code
            two_factor_code = fetch_latest_2fa_code()
            if not two_factor_code:
                logging.error("2FA required but no valid code found.")
                return None, {
                    "lookup_ok": False,
                    "error_type": "vrchat_auth_error",
                    "error_message": "2FA required but no valid code found",
                    "vrchat_outage": False,
                    "vrchat_outage_confirmed": False,
                    "vrchat_status_message": None,
                    "vrchat_status_indicator": None,
                }

            if "Email 2 Factor Authentication" in e.reason:
                auth_api.verify2_fa_email_code(TwoFactorEmailCode(two_factor_code))
            else:
                auth_api.verify2_fa(TwoFactorAuthCode(two_factor_code))

            current_user = auth_api.get_current_user()
            logging.info("Successfully logged in as %s", current_user.display_name)
            return api_client, None

        logging.error("VRChat login failed: %s", e)
        return None, {
            "lookup_ok": False,
            "error_type": "vrchat_auth_error",
            "error_message": str(e),
            "vrchat_outage": False,
            "vrchat_outage_confirmed": False,
            "vrchat_status_message": None,
            "vrchat_status_indicator": None,
        }

    except ApiException as e:
        logging.error("VRChat API error during login: %s", e)
        return None, _classify_vrchat_api_error(e)
    except Exception as e:
        logging.error("Unexpected VRChat login error: %s", e, exc_info=True)
        return None, _classify_vrchat_api_error(e)


VRCHAT_RELOGIN_INTERVAL_SECONDS = int(os.getenv("VRCHAT_RELOGIN_INTERVAL_SECONDS", "600"))

vrchat_api_client: vrchatapi.ApiClient | None = None
vrchat_login_error_meta: dict | None = None
_vrchat_session_lock = threading.Lock()
_vrchat_next_login_attempt_at = 0.0


def _default_vrchat_session_error(message: str = "VRChat session not active") -> dict:
    return {
        "lookup_ok": False,
        "error_type": "vrchat_session_unavailable",
        "error_message": message,
        "vrchat_outage": False,
        "vrchat_outage_confirmed": False,
        "vrchat_status_message": None,
        "vrchat_status_indicator": None,
    }


def _set_vrchat_session_state(
    client: vrchatapi.ApiClient | None,
    error_meta: dict | None,
    next_retry_delay_seconds: float | None = None,
):
    global vrchat_api_client, vrchat_login_error_meta, _vrchat_next_login_attempt_at
    with _vrchat_session_lock:
        vrchat_api_client = client
        vrchat_login_error_meta = error_meta
        if client is not None:
            _vrchat_next_login_attempt_at = 0.0
        else:
            delay = VRCHAT_RELOGIN_INTERVAL_SECONDS if next_retry_delay_seconds is None else next_retry_delay_seconds
            _vrchat_next_login_attempt_at = time.monotonic() + max(0.0, float(delay))


def attempt_vrchat_login(force: bool = False) -> tuple[vrchatapi.ApiClient | None, dict | None]:
    """Attempt VRChat login, optionally respecting the scheduled retry window."""
    with _vrchat_session_lock:
        current_client = vrchat_api_client
        current_error = vrchat_login_error_meta
        next_attempt_at = _vrchat_next_login_attempt_at

    if current_client is not None:
        return current_client, None

    if not force and next_attempt_at and time.monotonic() < next_attempt_at:
        return None, current_error or _default_vrchat_session_error()

    client, error_meta = login_to_vrchat()
    if client is not None:
        _set_vrchat_session_state(client, None)
        return client, None

    _set_vrchat_session_state(None, error_meta or _default_vrchat_session_error())
    return None, error_meta or _default_vrchat_session_error()


def get_vrchat_session() -> tuple[vrchatapi.ApiClient | None, dict | None]:
    """Return the current VRChat session without triggering a relogin attempt."""
    with _vrchat_session_lock:
        if vrchat_api_client is not None:
            return vrchat_api_client, None
        return None, vrchat_login_error_meta or _default_vrchat_session_error()


def invalidate_vrchat_session(error_meta: dict | None = None):
    """Clear the current session and schedule the next background relogin attempt."""
    _set_vrchat_session_state(
        None,
        error_meta or _default_vrchat_session_error("VRChat session expired"),
        next_retry_delay_seconds=VRCHAT_RELOGIN_INTERVAL_SECONDS,
    )


def _vrchat_relogin_loop():
    """Retry VRChat login in the background on a fixed cadence when logged out."""
    while True:
        with _vrchat_session_lock:
            has_client = vrchat_api_client is not None
            next_attempt_at = _vrchat_next_login_attempt_at

        if has_client:
            time.sleep(5)
            continue

        now = time.monotonic()
        if next_attempt_at and now < next_attempt_at:
            time.sleep(min(5.0, max(1.0, next_attempt_at - now)))
            continue

        logging.info("Attempting scheduled VRChat login retry")
        attempt_vrchat_login(force=True)
        time.sleep(5)


# One-time startup login, then background retry on a fixed cadence.
attempt_vrchat_login(force=True)
if not get_vrchat_session()[0]:
    logging.error("Initial VRChat login failed. Continuing to serve queue with outage-aware responses.")

threading.Thread(target=_vrchat_relogin_loop, name="vrchat-relogin", daemon=True).start()

# -------------------------------------------------------------------
# Small TTL cache for VRChat user lookups (dedupe repeated requests)
# -------------------------------------------------------------------
VRCHAT_TTL_SECONDS = int(os.getenv("VRCHAT_TTL_SECONDS", "180"))
VRCHAT_CACHE_MAX = int(os.getenv("VRCHAT_CACHE_MAX", "10000"))


class _TTLCache:
    def __init__(self, maxsize: int, ttl_seconds: int):
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: object):
        if len(self._store) >= self.maxsize:
            try:
                self._store.pop(next(iter(self._store)))
            except StopIteration:
                pass
        self._store[key] = (time.monotonic() + self.ttl, value)


_vrc_cache = _TTLCache(VRCHAT_CACHE_MAX, VRCHAT_TTL_SECONDS)


# -------------------------------------------------------------------
# VRChat status page / outage helpers
# -------------------------------------------------------------------
VRCHAT_STATUS_SUMMARY_URL = os.getenv("VRCHAT_STATUS_SUMMARY_URL", "https://status.vrchat.com/api/v2/summary.json")
VRCHAT_STATUS_CACHE_SECONDS = int(os.getenv("VRCHAT_STATUS_CACHE_SECONDS", "120"))
VRCHAT_LOOKUP_RETRIES = int(os.getenv("VRCHAT_LOOKUP_RETRIES", "3"))
VRCHAT_LOOKUP_BACKOFF_BASE = float(os.getenv("VRCHAT_LOOKUP_BACKOFF_BASE", "1.5"))

_vrchat_status_cache: dict[str, object] = {
    "expires_at": 0.0,
    "value": None,
}


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


def _fetch_vrchat_status_summary(force_refresh: bool = False) -> dict | None:
    now = time.monotonic()
    cached = _vrchat_status_cache.get("value")
    expires_at = float(_vrchat_status_cache.get("expires_at") or 0.0)
    if cached is not None and not force_refresh and expires_at > now:
        return cached  # type: ignore[return-value]

    try:
        req = urllib_request.Request(
            VRCHAT_STATUS_SUMMARY_URL,
            headers={
                "User-Agent": "VRCVerifyBot/1.0 (+https://status.vrchat.com/)"
            },
        )
        with urllib_request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        _vrchat_status_cache["value"] = data
        _vrchat_status_cache["expires_at"] = now + VRCHAT_STATUS_CACHE_SECONDS
        return data
    except Exception:
        logging.warning("Failed to fetch VRChat status summary", exc_info=True)
        if cached is not None:
            return cached  # type: ignore[return-value]
        return None


def _extract_relevant_vrchat_status() -> dict:
    summary = _fetch_vrchat_status_summary()
    if not summary:
        return {
            "vrchat_outage": False,
            "vrchat_outage_confirmed": False,
            "vrchat_status_message": None,
            "vrchat_status_indicator": None,
        }

    overall = (summary.get("status") or {})
    incidents = summary.get("incidents") or []
    components = summary.get("components") or []

    keywords = ("api", "authentication", "login", "website")
    relevant_components = []
    for component in components:
        name = str(component.get("name") or "")
        lower = name.lower()
        if any(k in lower for k in keywords):
            relevant_components.append(component)

    degraded_statuses = {"degraded_performance", "partial_outage", "major_outage", "under_maintenance"}
    confirmed = any((c.get("status") in degraded_statuses) for c in relevant_components)

    active_incidents = []
    for incident in incidents:
        incident_status = str(incident.get("status") or "")
        if incident_status not in {"resolved", "completed", "postmortem", "none"}:
            active_incidents.append(incident)

    if not confirmed and active_incidents:
        for incident in active_incidents:
            name = str(incident.get("name") or "").lower()
            body = str((incident.get("incident_updates") or [{}])[0].get("body") or "").lower()
            if any(k in name or k in body for k in keywords):
                confirmed = True
                break

    message = None
    indicator = overall.get("indicator")
    if active_incidents:
        first = active_incidents[0]
        updates = first.get("incident_updates") or []
        latest_update = updates[0] if updates else {}
        incident_name = first.get("name")
        update_body = latest_update.get("body")
        message = incident_name or update_body
        if incident_name and update_body:
            message = f"{incident_name}: {update_body}"
    elif relevant_components:
        degraded = [c for c in relevant_components if c.get("status") in degraded_statuses]
        if degraded:
            message = ", ".join(f"{c.get('name')}: {c.get('status')}" for c in degraded)

    return {
        "vrchat_outage": confirmed,
        "vrchat_outage_confirmed": confirmed,
        "vrchat_status_message": message,
        "vrchat_status_indicator": indicator,
    }


def _classify_vrchat_api_error(exc: Exception) -> dict:
    status = getattr(exc, "status", None)
    reason = str(getattr(exc, "reason", exc) or exc)
    body = str(getattr(exc, "body", "") or "")
    text = f"{reason} {body}".lower()

    error_type = "vrchat_error"
    likely_outage = False

    if status in {500, 502, 503, 504}:
        error_type = "vrchat_upstream_error"
        likely_outage = True
    elif status == 429:
        error_type = "vrchat_rate_limited"
    elif status in {401, 403}:
        error_type = "vrchat_auth_error"
    elif status == 404:
        error_type = "vrchat_user_not_found"
    elif "timed out" in text or "timeout" in text:
        error_type = "vrchat_timeout"
        likely_outage = True
    elif "application error" in text or "internal server error" in text or "bad gateway" in text or "service unavailable" in text:
        error_type = "vrchat_upstream_error"
        likely_outage = True

    status_meta = _extract_relevant_vrchat_status() if likely_outage else {
        "vrchat_outage": False,
        "vrchat_outage_confirmed": False,
        "vrchat_status_message": None,
        "vrchat_status_indicator": None,
    }

    return {
        "lookup_ok": False,
        "error_type": error_type,
        "error_message": reason if len(reason) < 500 else reason[:500],
        "vrchat_outage": bool(likely_outage or status_meta.get("vrchat_outage")),
        "vrchat_outage_confirmed": bool(status_meta.get("vrchat_outage_confirmed")),
        "vrchat_status_message": status_meta.get("vrchat_status_message"),
        "vrchat_status_indicator": status_meta.get("vrchat_status_indicator"),
    }


def _get_vrchat_user_with_retry(users_api_instance, vrc_user_id: str):
    last_exc = None
    for attempt in range(1, VRCHAT_LOOKUP_RETRIES + 1):
        try:
            return users_api_instance.get_user(vrc_user_id)
        except ApiException as e:
            last_exc = e
            status = getattr(e, "status", None)
            if status not in {500, 502, 503, 504, 429}:
                raise
            if attempt >= VRCHAT_LOOKUP_RETRIES:
                raise
            delay = min(8.0, VRCHAT_LOOKUP_BACKOFF_BASE * attempt) + random.uniform(0.0, 0.35)
            logging.warning(
                "Transient VRChat get_user failure for %s (status=%s). Retrying in %.2fs (attempt %s/%s)",
                vrc_user_id,
                status,
                delay,
                attempt,
                VRCHAT_LOOKUP_RETRIES,
            )
            time.sleep(delay)
        except Exception as e:
            last_exc = e
            if attempt >= VRCHAT_LOOKUP_RETRIES:
                raise
            delay = min(8.0, VRCHAT_LOOKUP_BACKOFF_BASE * attempt) + random.uniform(0.0, 0.35)
            logging.warning(
                "Transient VRChat get_user failure for %s. Retrying in %.2fs (attempt %s/%s)",
                vrc_user_id, delay, attempt, VRCHAT_LOOKUP_RETRIES, exc_info=True
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc


# -------------------------------------------------------------------
# Verification Logic
# -------------------------------------------------------------------
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
        # Unknown failure; requeue once (may need DLQ in production).
        logging.exception("Unexpected error processing request; requeueing")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


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
        meta = session_error or _default_vrchat_session_error()
        return _result_payload(
            discord_id,
            vrc_user_id,
            guild_id,
            verification_code,
            **meta,
        )

    users_api_instance = users_api.UsersApi(client)

    # Try cache first
    cached = _vrc_cache.get(vrc_user_id)
    if cached is not None:
        vrc_user = cached
    else:
        try:
            vrc_user = _get_vrchat_user_with_retry(users_api_instance, vrc_user_id)
            _vrc_cache.set(vrc_user_id, vrc_user)
        except UnauthorizedException as e:
            logging.warning("VRChat session unauthorized; deferring relogin to background worker")
            meta = _classify_vrchat_api_error(e)
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
                invalidate_vrchat_session(_classify_vrchat_api_error(e))
            return _result_payload(
                discord_id,
                vrc_user_id,
                guild_id,
                verification_code,
                **_classify_vrchat_api_error(e),
            )
        except Exception as e:
            logging.error("Unexpected failure while fetching VRChat user %s. Error: %s", vrc_user_id, e)
            return _result_payload(
                discord_id,
                vrc_user_id,
                guild_id,
                verification_code,
                **_classify_vrchat_api_error(e),
            )

    age_status = getattr(vrc_user, "age_verification_status", "unknown")
    bio = getattr(vrc_user, "bio", "")
    logging.info(
        "[verify_and_build_result] user=%s, age_status=%s, bio=%s",
        vrc_user_id,
        age_status,
        bio,
    )

    is_18_plus = age_status == "18+"

    code_found = False
    if verification_code is not None:
        stripped_code = verification_code.strip()
        for line in bio.splitlines():
            if stripped_code == line.strip():
                code_found = True
                break

    display_name = getattr(vrc_user, "display_name", None)

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
    if not vrchat_api_client:
        logging.warning("VRChat login was not successful. We might fail all requests.")
    while True:
        connection = None
        try:
            connection = _rabbitmq_connect_with_retry(max_tries=0)
            channel = connection.channel()
            channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(
                queue=RABBITMQ_QUEUE_NAME,
                on_message_callback=process_verification_request,
                auto_ack=False,
            )
            logging.info("Listening for verification requests on '%s'...", RABBITMQ_QUEUE_NAME)
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


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    listen_for_verifications()
