import time
import imaplib
import email
import re
import os
import json
import pika
import psycopg2
import logging
from dotenv import load_dotenv

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
DATABASE_URL = os.getenv("DATABASE_URL")

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
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST,
    port=RABBITMQ_PORT,
    virtual_host=RABBITMQ_VHOST,
    credentials=credentials,
)


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
    """Logs into VRChat and handles possible 2FA prompts automatically."""
    configuration = vrchatapi.Configuration(
        username=VRCHAT_USERNAME, password=VRCHAT_PASSWORD
    )

    api_client = vrchatapi.ApiClient(configuration)
    api_client.user_agent = "VRCVerifyBot/1.0 (contact@yourdomain.com)"
    auth_api = authentication_api.AuthenticationApi(api_client)

    try:
        current_user = auth_api.get_current_user()
        logging.info("Successfully logged in as %s", current_user.display_name)
        return api_client

    except UnauthorizedException as e:
        if e.status == 200:
            logging.info("2FA Required! Fetching code from email...")

            # Auto-fetch the 2FA code
            two_factor_code = fetch_latest_2fa_code()
            if not two_factor_code:
                logging.error("2FA required but no valid code found. Exiting.")
                return None

            if "Email 2 Factor Authentication" in e.reason:
                auth_api.verify2_fa_email_code(TwoFactorEmailCode(two_factor_code))
            else:
                auth_api.verify2_fa(TwoFactorAuthCode(two_factor_code))

            current_user = auth_api.get_current_user()
            logging.info("Successfully logged in as %s", current_user.display_name)
            return api_client

        logging.error("VRChat login failed: %s", e)
        return None

    except ApiException as e:
        logging.error("VRChat API error: %s", e)
        return None


# One-time login
vrchat_api_client = login_to_vrchat()
if not vrchat_api_client:
    logging.error("VRChat login failed. Exiting soon...")

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
# Verification Logic
# -------------------------------------------------------------------
def process_verification_request(ch, method, properties, body):
    """
    The bot sends us JSON with:
      - discordID
      - vrcUserID
      - guildID
      - verificationCode (possibly None)
      - update_nickname (optional, default False)
    We'll check VRChat, then publish a result back to RESULT_QUEUE_NAME.
    """
    data = json.loads(body)
    discord_id = data.get("discordID")
    vrc_user_id = data.get("vrcUserID")
    guild_id = data.get("guildID")
    verification_code = data.get("verificationCode")
    update_nickname = data.get("updateNickname", False)

    logging.info("Received verification request: %s", data)

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


def verify_and_build_result(discord_id, vrc_user_id, guild_id, verification_code):
    """
    Queries VRChat to determine if the user is 18+ and/or if the verification code is present in their bio.
    Returns a dictionary with:
      - "is_18_plus"
      - "code_found" (bool)
      - "verificationCode"
      - etc.
    """
    if not vrchat_api_client:
        logging.error("VRChat session not active. Failing verification.")
        return {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "is_18_plus": False,
            "verificationCode": verification_code,
            "code_found": False,
            "display_name": None,
        }

    users_api_instance = users_api.UsersApi(vrchat_api_client)
    display_name = None

    # Try cache first
    cached = _vrc_cache.get(vrc_user_id)
    if cached is not None:
        vrc_user = cached
    else:
        try:
            vrc_user = users_api_instance.get_user(vrc_user_id)
            _vrc_cache.set(vrc_user_id, vrc_user)
        except ApiException as e:
            logging.error("Failed to fetch VRChat user %s. Error: %s", vrc_user_id, e)
            return {
                "discordID": discord_id,
                "vrcUserID": vrc_user_id,
                "guildID": guild_id,
                "is_18_plus": False,
                "verificationCode": verification_code,
                "code_found": False,
                "display_name": display_name,
            }

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

    return {
        "discordID": discord_id,
        "vrcUserID": vrc_user_id,
        "guildID": guild_id,
        "is_18_plus": is_18_plus,
        "verificationCode": verification_code,
        "code_found": code_found,
        "display_name": display_name,
    }


def send_verification_result(result: dict):
    """Publish the verification result to the bot's queue."""
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=RESULT_QUEUE_NAME, durable=True)

    message_str = json.dumps(result)
    channel.basic_publish(exchange="", routing_key=RESULT_QUEUE_NAME, body=message_str)
    connection.close()

    logging.info("Sent verification result to '%s': %s", RESULT_QUEUE_NAME, message_str)


def listen_for_verifications():
    """Blocking function that listens for new requests from the bot."""
    if not vrchat_api_client:
        logging.warning("VRChat login was not successful. We might fail all requests.")
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)
    channel.basic_consume(
        queue=RABBITMQ_QUEUE_NAME,
        on_message_callback=process_verification_request,
        auto_ack=True,
    )
    logging.info("Listening for verification requests on '%s'...", RABBITMQ_QUEUE_NAME)
    channel.start_consuming()


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    if not vrchat_api_client:
        logging.error("VRChat login failed. Exiting...")
    else:
        listen_for_verifications()
