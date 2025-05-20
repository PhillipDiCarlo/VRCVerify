import time
import imaplib
import email
import re
import os
import json
import pika
import logging
import heapq
import threading
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
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# -------------------------------------------------------------------
# RabbitMQ Setup
# -------------------------------------------------------------------
credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST,
    port=RABBITMQ_PORT,
    virtual_host=RABBITMQ_VHOST,
    credentials=credentials
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
        username=VRCHAT_USERNAME,
        password=VRCHAT_PASSWORD
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
                auth_api.verify2_fa_email_code(two_factor_email_code=TwoFactorEmailCode(two_factor_code))
            elif "2 Factor Authentication" in e.reason:
                auth_api.verify2_fa(two_factor_auth_code=TwoFactorAuthCode(two_factor_code))

            # Retry login after submitting 2FA
            current_user = auth_api.get_current_user()
            logging.info("Successfully logged in as %s", current_user.display_name)
            return api_client

        logging.error("VRChat login failed: %s", e)
        return None

    except vrchatapi.ApiException as e:
        logging.error("VRChat API error: %s", e)
        return None

# One-time login
vrchat_api_client = login_to_vrchat()
if not vrchat_api_client:
    logging.error("VRChat login failed. Exiting soon...")

# -------------------------------------------------------------------
# Rate-Limited Scheduler (1 task every 10s)
# -------------------------------------------------------------------
class RateLimitedScheduler:
    def __init__(self, interval_seconds: float):
        self.interval = interval_seconds
        self.lock     = threading.Lock()
        self.cv       = threading.Condition(self.lock)
        self.heap     = []   # heap of (run_at, func, args, kwargs)
        self.last_run = 0.0

        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def schedule(self, func, *args, **kwargs):
        with self.lock:
            now    = time.time()
            # next slot is max(now, last_run + interval)
            run_at = max(now, self.last_run + self.interval)
            heapq.heappush(self.heap, (run_at, func, args, kwargs))
            self.last_run = run_at
            self.cv.notify()

    def _run_loop(self):
        while True:
            with self.lock:
                while not self.heap:
                    self.cv.wait()
                run_at, func, args, kwargs = self.heap[0]
                delay = run_at - time.time()
                if delay > 0:
                    self.cv.wait(timeout=delay)
                    continue
                # time to run
                heapq.heappop(self.heap)

            try:
                func(*args, **kwargs)
            except Exception as e:
                logging.error("Scheduled task error: %s", e)

# instantiate scheduler with 10s interval
scheduler = RateLimitedScheduler(interval_seconds=10.0)

# -------------------------------------------------------------------
# Core Verification Logic
# -------------------------------------------------------------------
def handle_verification(data: dict):
    discord_id       = data["discordID"]
    vrc_user_id      = data["vrcUserID"]
    guild_id         = data["guildID"]
    verification_code= data.get("verificationCode")

    result = verify_and_build_result(
        discord_id, vrc_user_id, guild_id, verification_code
    )
    send_verification_result(result)

def process_verification_request(ch, method, properties, body):
    data = json.loads(body)
    logging.info("Enqueuing verification for %s", data)
    scheduler.schedule(handle_verification, data)

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
            "code_found": False
        }

    # Attempt to fetch user from VRChat
    users_api_instance = users_api.UsersApi(vrchat_api_client)
    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except ApiException as e:
        logging.error("Failed to fetch VRChat user %s. Error: %s", vrc_user_id, e)
        return {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "is_18_plus": False,
            "verificationCode": verification_code,
            "code_found": False
        }

    age_status = getattr(vrc_user, "age_verification_status", "unknown")
    bio = getattr(vrc_user, "bio", "")

    logging.info("[verify_and_build_result] user=%s, age_status=%s, bio=%s", vrc_user_id, age_status, bio)

    is_18_plus = (age_status == "18+")

    code_found = False
    if verification_code is not None:
        stripped_code = verification_code.strip()
        # Split the bio into lines and remove leading/trailing whitespace from each
        for line in bio.splitlines():
            if stripped_code == line.strip():
                code_found = True
                break

    return {
        "discordID": discord_id,
        "vrcUserID": vrc_user_id,
        "guildID": guild_id,
        "is_18_plus": is_18_plus,
        "verificationCode": verification_code,  # None => re-check
        "code_found": code_found
    }

def send_verification_result(result: dict):
    """Publish the verification result to the bot's queue."""
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=RESULT_QUEUE_NAME, durable=True)

    message_str = json.dumps(result)
    channel.basic_publish(
        exchange="",
        routing_key=RESULT_QUEUE_NAME,
        body=message_str
    )
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
        auto_ack=True
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
