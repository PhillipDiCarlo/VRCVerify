"""Shared VRChat session machinery: login, 2FA, cookie persistence, health.

Extracted from vrc_online_checker.py so that a second VRChat account can hold
its own session without a second copy of this code. The group-invite bot
(issue #49) runs on a deliberately separate account from the age checker, so
that platform moderation against one cannot take the other down.

Everything account-specific arrives in a `VRChatAccount`: credentials, cookie
file, 2FA mailbox and User-Agent. Each `VRChatSession` owns its own client,
its own lock and its own relogin schedule, so two sessions in one process
never share state. The only genuinely process-wide thing here is the VRChat
status-page cache, which describes VRChat rather than any one account.
"""

import imaplib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from urllib import request as urllib_request

import vrchatapi
from vrchatapi.api import authentication_api
from vrchatapi.exceptions import ApiException, UnauthorizedException
from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode

# -------------------------------------------------------------------
# Shared configuration
#
# These describe how we talk to VRChat, not which account is talking, so they
# stay module-level rather than moving onto VRChatAccount.
# -------------------------------------------------------------------
VRCHAT_API_CONNECT_TIMEOUT_SECONDS = float(os.getenv("VRCHAT_API_CONNECT_TIMEOUT_SECONDS", "10"))
VRCHAT_API_READ_TIMEOUT_SECONDS = float(os.getenv("VRCHAT_API_READ_TIMEOUT_SECONDS", "20"))
VRCHAT_RELOGIN_INTERVAL_SECONDS = int(os.getenv("VRCHAT_RELOGIN_INTERVAL_SECONDS", "600"))

VRCHAT_STATUS_SUMMARY_URL = os.getenv("VRCHAT_STATUS_SUMMARY_URL", "https://status.vrchat.com/api/v2/summary.json")
VRCHAT_STATUS_CACHE_SECONDS = int(os.getenv("VRCHAT_STATUS_CACHE_SECONDS", "120"))

# Statuses worth retrying, and the delay schedule for doing so. Shared because
# VRChat's Creator Guidelines require backoff from every caller, not just the
# age checker: whatever calls the API next must retry the same way.
TRANSIENT_HTTP_STATUSES = frozenset({500, 502, 503, 504, 429})
VRCHAT_LOOKUP_BACKOFF_BASE = float(os.getenv("VRCHAT_LOOKUP_BACKOFF_BASE", "1.5"))


def request_timeout() -> tuple[float, float]:
    return (VRCHAT_API_CONNECT_TIMEOUT_SECONDS, VRCHAT_API_READ_TIMEOUT_SECONDS)


def backoff_delay(attempt: int) -> float:
    """Delay before retry number `attempt`, with jitter.

    The jitter is not decoration. VRChat's guidelines call out that fixed
    intervals from many callers synchronise into traffic spikes, so every
    retry schedule here is deliberately uneven.
    """
    import random

    return min(8.0, VRCHAT_LOOKUP_BACKOFF_BASE * attempt) + random.uniform(0.0, 0.35)


# -------------------------------------------------------------------
# Account identity
# -------------------------------------------------------------------
@dataclass(frozen=True)
class VRChatAccount:
    """One VRChat login, and everything that is specific to it.

    `label` exists purely so logs from two sessions in one process can be told
    apart; it is never sent to VRChat.
    """

    username: str | None
    password: str | None
    user_agent: str
    session_file: str = ""
    gmail_user: str | None = None
    gmail_app_password: str | None = None
    imap_host: str = "imap.gmail.com"
    label: str = "vrchat"

    @classmethod
    def from_env(cls, user_agent: str, prefix: str = "", label: str = "vrchat") -> "VRChatAccount":
        """Build an account from `{prefix}VRCHAT_USERNAME` and friends.

        The empty prefix reads the original variable names, so the age
        checker's configuration is untouched by this refactor. A second
        account sets its own prefixed variables instead of inheriting any of
        the checker's.
        """
        return cls(
            username=os.getenv(f"{prefix}VRCHAT_USERNAME"),
            password=os.getenv(f"{prefix}VRCHAT_PASSWORD"),
            user_agent=user_agent,
            session_file=os.getenv(f"{prefix}VRCHAT_SESSION_FILE", "").strip(),
            gmail_user=os.getenv(f"{prefix}GMAIL_USER"),
            gmail_app_password=os.getenv(f"{prefix}GMAIL_APP_PASSWORD"),
            label=label,
        )


# -------------------------------------------------------------------
# VRChat status page / outage helpers
#
# Process-wide on purpose: status.vrchat.com describes VRChat, not an account,
# so two sessions asking at once should share one cached answer.
# -------------------------------------------------------------------
_status_cache: dict[str, object] = {
    "expires_at": 0.0,
    "value": None,
}

NO_OUTAGE_META = {
    "vrchat_outage": False,
    "vrchat_outage_confirmed": False,
    "vrchat_status_message": None,
    "vrchat_status_indicator": None,
}


def fetch_status_summary(force_refresh: bool = False) -> dict | None:
    now = time.monotonic()
    cached = _status_cache.get("value")
    expires_at = float(_status_cache.get("expires_at") or 0.0)
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
        _status_cache["value"] = data
        _status_cache["expires_at"] = now + VRCHAT_STATUS_CACHE_SECONDS
        return data
    except Exception:
        logging.warning("Failed to fetch VRChat status summary", exc_info=True)
        if cached is not None:
            return cached  # type: ignore[return-value]
        return None


def extract_relevant_status() -> dict:
    summary = fetch_status_summary()
    if not summary:
        return dict(NO_OUTAGE_META)

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


def classify_api_error(exc: Exception) -> dict:
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

    status_meta = extract_relevant_status() if likely_outage else dict(NO_OUTAGE_META)

    return {
        "lookup_ok": False,
        "error_type": error_type,
        "error_message": reason if len(reason) < 500 else reason[:500],
        "vrchat_outage": bool(likely_outage or status_meta.get("vrchat_outage")),
        "vrchat_outage_confirmed": bool(status_meta.get("vrchat_outage_confirmed")),
        "vrchat_status_message": status_meta.get("vrchat_status_message"),
        "vrchat_status_indicator": status_meta.get("vrchat_status_indicator"),
    }


def default_session_error(message: str = "VRChat session not active") -> dict:
    return {
        "lookup_ok": False,
        "error_type": "vrchat_session_unavailable",
        "error_message": message,
        **NO_OUTAGE_META,
    }


def auth_error(message: str) -> dict:
    return {
        "lookup_ok": False,
        "error_type": "vrchat_auth_error",
        "error_message": message,
        **NO_OUTAGE_META,
    }


# -------------------------------------------------------------------
# Function to Fetch 2FA Code from Gmail
# -------------------------------------------------------------------
def fetch_latest_2fa_code(account: VRChatAccount):
    """Waits for VRChat's 2FA email and retrieves the code from the subject line."""
    logging.info("Waiting 10 seconds for 2FA email to arrive...")
    time.sleep(10)  # Initial wait to allow the email to arrive

    retries = 3  # Number of times to retry
    wait_time = 5  # Seconds between retries

    for attempt in range(retries):
        try:
            mail = imaplib.IMAP4_SSL(account.imap_host)
            mail.login(account.gmail_user, account.gmail_app_password)
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
                # Never log the code itself: log files shouldn't hold auth secrets.
                logging.info("Found VRChat 2FA code in email subject.")
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
# Session persistence
#
# Every fresh login consumes a 2FA email and VRChat rate-limits the 2FA
# endpoint (429). A couple of redeploys in quick succession can therefore
# lock the bot account out of logging in at all, which takes verification
# down completely -- restarting is exactly when that must not happen.
# Storing the auth cookie lets a restarted service resume its session
# without re-authenticating.
#
# The cookie is an auth credential, so the file is written 0600 and should
# live on a volume that is not world-readable. Leave the session file unset
# to disable persistence entirely.
#
# Two accounts must never share a session file: the second login would
# overwrite the first account's cookie and both would then thrash between
# each other's sessions.
# -------------------------------------------------------------------
def attach_session_store(
    account: VRChatAccount, api_client, load_existing: bool = True
) -> MozillaCookieJar | None:
    """Back the client's cookie jar with the account's session file, if set.

    The jar is attached even when load_existing is False, so cookies issued
    by a fresh login still get persisted -- otherwise a login that follows a
    rejected session would leave nothing on disk, defeating the feature in
    exactly the case it exists for.

    Returns the jar so a caller can persist it after a successful login, or
    None when persistence is disabled. Never raises: failing to reuse a
    stored session must never prevent logging in normally.
    """
    if not account.session_file:
        return None

    jar = MozillaCookieJar(account.session_file)
    if load_existing and os.path.exists(account.session_file):
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            logging.info("Loaded stored VRChat session from %s", account.session_file)
        except Exception:
            # load() inserts every line it parsed before raising, so a torn
            # file (save is not atomic) leaves a TRUNCATED auth cookie behind.
            # Sending that would fail auth in a confusing way -- drop it all.
            jar.clear()
            logging.warning(
                "Stored VRChat session at %s is unreadable; discarding it",
                account.session_file,
                exc_info=True,
            )
    try:
        api_client.rest_client.cookie_jar = jar
    except Exception:
        logging.warning("Could not attach session store to client", exc_info=True)
        return None
    return jar


def jar_sends_cookies(jar: MozillaCookieJar | None, host: str) -> bool:
    """True only if the jar would actually send cookies to `host`.

    len(jar) is the wrong test: we load with ignore_expires=True, so expired
    cookies inflate the count even though add_cookie_header filters them out
    at request time. A fully expired store must not look like a live session.
    """
    if not jar:
        return False
    try:
        probe = urllib_request.Request(host)
        jar.add_cookie_header(probe)
        return bool(probe.get_header("Cookie"))
    except Exception:
        logging.warning("Could not evaluate stored session cookies", exc_info=True)
        return False


def persist_session(account: VRChatAccount, jar: MozillaCookieJar | None) -> None:
    """Write the auth cookie to disk atomically, owner-readable only."""
    if jar is None:
        return
    try:
        path = os.path.abspath(account.session_file)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # MozillaCookieJar.save() truncates in place, so being killed mid-write
        # leaves a half-written file that loads as a truncated cookie. Write a
        # temp file and rename, which is atomic on the same filesystem.
        tmp = f"{path}.tmp"
        jar.save(filename=tmp, ignore_discard=True, ignore_expires=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        logging.info("Stored VRChat session to %s", path)
    except Exception:
        logging.warning(
            "Could not persist VRChat session to %s (continuing without it)",
            account.session_file,
            exc_info=True,
        )


def discard_stored_session(account: VRChatAccount) -> None:
    """Delete a stored session that failed to authenticate."""
    if not account.session_file:
        return
    try:
        os.remove(account.session_file)
        logging.info("Discarded stale VRChat session file %s", account.session_file)
    except FileNotFoundError:
        pass
    except Exception:
        logging.warning("Could not remove %s", account.session_file, exc_info=True)


def check_session_store_writable(account: VRChatAccount) -> bool:
    """Log at ERROR if session persistence is configured but unusable.

    A named volume created before the image gained /data mounts root-owned,
    so uid 10001 cannot write there. persist_session only warns, which means
    persistence would stay silently off forever and every restart would burn
    a 2FA email. Surface it loudly at startup instead.
    """
    if not account.session_file:
        logging.info("Session file unset for %s; VRChat session will not persist across restarts", account.label)
        return False
    path = os.path.abspath(account.session_file)
    probe = f"{path}.probe"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        logging.info("Session store at %s is writable", path)
        return True
    except Exception as exc:
        logging.error(
            "Session store %s is NOT writable (%s). Restarts will re-authenticate "
            "and consume a 2FA email each time, risking a VRChat 429 lockout. "
            "If running in Docker, check that the volume mounted at the parent "
            "directory is owned by the container user.",
            path,
            exc,
        )
        return False


# -------------------------------------------------------------------
# VRChat Login with Auto 2FA
# -------------------------------------------------------------------
def login(account: VRChatAccount, load_stored_session: bool = True):
    """Logs into VRChat and handles possible 2FA prompts automatically.

    Reuses a stored session cookie when one is available so that restarts do
    not burn a 2FA email (see the session file). A stored session that is
    outright rejected is discarded and the login retried from scratch -- but
    the retry still persists whatever session it establishes.

    Returns:
        tuple[vrchatapi.ApiClient | None, dict | None]:
            (client, error_meta). When login succeeds, error_meta is None.
            When login fails, client is None and error_meta contains structured
            outage/auth metadata that can be sent back to users.
    """
    configuration = vrchatapi.Configuration(
        username=account.username, password=account.password
    )

    api_client = vrchatapi.ApiClient(configuration)
    api_client.user_agent = account.user_agent
    # Always attach the store; only loading is conditional. Newly issued
    # cookies must land on disk even on a retry after a rejected session.
    session_jar = attach_session_store(account, api_client, load_existing=load_stored_session)
    reused_session = jar_sends_cookies(session_jar, configuration.host)
    auth_api = authentication_api.AuthenticationApi(api_client)
    timeout = request_timeout()

    try:
        current_user = auth_api.get_current_user(_request_timeout=timeout)
        if reused_session:
            logging.info("Reused stored VRChat session (no 2FA needed)")
        logging.info("Successfully logged in as %s", current_user.display_name)
        persist_session(account, session_jar)
        return api_client, None

    except UnauthorizedException as e:
        # "2FA required" (status 200) is the ordinary cold-login signal and
        # must be handled in place -- it is NOT evidence the stored cookie is
        # bad. Completing 2FA here refreshes the jar and persists it, instead
        # of throwing the session away and paying for a second round trip.
        if e.status != 200 and reused_session:
            logging.warning("Stored VRChat session rejected; retrying with a fresh login")
            discard_stored_session(account)
            return login(account, load_stored_session=False)

        if e.status == 200:
            logging.info("2FA Required! Fetching code from email...")

            # Auto-fetch the 2FA code
            two_factor_code = fetch_latest_2fa_code(account)
            if not two_factor_code:
                logging.error("2FA required but no valid code found.")
                return None, auth_error("2FA required but no valid code found")

            # e.reason can be None; `in None` would raise TypeError from inside
            # this handler, escape login(), and kill the relogin thread.
            if "Email 2 Factor Authentication" in (e.reason or ""):
                auth_api.verify2_fa_email_code(
                    TwoFactorEmailCode(two_factor_code),
                    _request_timeout=timeout,
                )
            else:
                auth_api.verify2_fa(
                    TwoFactorAuthCode(two_factor_code),
                    _request_timeout=timeout,
                )

            current_user = auth_api.get_current_user(_request_timeout=timeout)
            logging.info("Successfully logged in as %s", current_user.display_name)
            persist_session(account, session_jar)
            return api_client, None

        logging.error("VRChat login failed: %s", e)
        return None, auth_error(str(e))

    except ApiException as e:
        logging.error(
            "VRChat API error during login (timeout=%ss/%ss): %s",
            VRCHAT_API_CONNECT_TIMEOUT_SECONDS,
            VRCHAT_API_READ_TIMEOUT_SECONDS,
            e,
        )
        return None, classify_api_error(e)
    except Exception as e:
        logging.error(
            "Unexpected VRChat login error (timeout=%ss/%ss): %s",
            VRCHAT_API_CONNECT_TIMEOUT_SECONDS,
            VRCHAT_API_READ_TIMEOUT_SECONDS,
            e,
            exc_info=True,
        )
        return None, classify_api_error(e)


# -------------------------------------------------------------------
# Live session state
# -------------------------------------------------------------------
class VRChatSession:
    """One account's live session, and the retry schedule behind it.

    State lives on the instance rather than in module globals so that two
    accounts in one process cannot clobber each other's client or relogin
    timer -- which is the whole reason this module exists.
    """

    def __init__(self, account: VRChatAccount):
        self.account = account
        self._client: vrchatapi.ApiClient | None = None
        self._error_meta: dict | None = None
        self._lock = threading.Lock()
        self._next_login_attempt_at = 0.0

    @property
    def client(self) -> vrchatapi.ApiClient | None:
        with self._lock:
            return self._client

    def _set_state(
        self,
        client: vrchatapi.ApiClient | None,
        error_meta: dict | None,
        next_retry_delay_seconds: float | None = None,
    ):
        with self._lock:
            self._client = client
            self._error_meta = error_meta
            if client is not None:
                self._next_login_attempt_at = 0.0
            else:
                delay = VRCHAT_RELOGIN_INTERVAL_SECONDS if next_retry_delay_seconds is None else next_retry_delay_seconds
                self._next_login_attempt_at = time.monotonic() + max(0.0, float(delay))

    def attempt_login(self, force: bool = False) -> tuple[vrchatapi.ApiClient | None, dict | None]:
        """Attempt VRChat login, optionally respecting the scheduled retry window."""
        with self._lock:
            current_client = self._client
            current_error = self._error_meta
            next_attempt_at = self._next_login_attempt_at

        if current_client is not None:
            return current_client, None

        if not force and next_attempt_at and time.monotonic() < next_attempt_at:
            return None, current_error or default_session_error()

        client, error_meta = login(self.account)
        if client is not None:
            self._set_state(client, None)
            return client, None

        self._set_state(None, error_meta or default_session_error())
        return None, error_meta or default_session_error()

    def get(self) -> tuple[vrchatapi.ApiClient | None, dict | None]:
        """Return the current VRChat session without triggering a relogin attempt."""
        with self._lock:
            if self._client is not None:
                return self._client, None
            return None, self._error_meta or default_session_error()

    def invalidate(self, error_meta: dict | None = None):
        """Clear the current session and schedule the next background relogin attempt."""
        self._set_state(
            None,
            error_meta or default_session_error("VRChat session expired"),
            next_retry_delay_seconds=VRCHAT_RELOGIN_INTERVAL_SECONDS,
        )

    def relogin_loop(self):
        """Retry VRChat login in the background on a fixed cadence when logged out."""
        while True:
            with self._lock:
                has_client = self._client is not None
                next_attempt_at = self._next_login_attempt_at

            if has_client:
                time.sleep(5)
                continue

            now = time.monotonic()
            if next_attempt_at and now < next_attempt_at:
                time.sleep(min(5.0, max(1.0, next_attempt_at - now)))
                continue

            logging.info("Attempting scheduled VRChat login retry")
            try:
                self.attempt_login(force=True)
            except Exception:
                # This thread is the ONLY thing that can recover a lost session --
                # the main thread is blocked consuming RabbitMQ. If it dies, every
                # request fails until someone restarts the container, so no
                # exception may ever escape this loop.
                logging.exception("Scheduled VRChat login retry raised; will retry")
            time.sleep(5)

    def start_relogin_thread(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.relogin_loop,
            name=f"vrchat-relogin-{self.account.label}",
            daemon=True,
        )
        thread.start()
        return thread
