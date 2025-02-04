import os
import json
import pika
import psycopg2
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
RABBITMQ_QUEUE_NAME = os.getenv("RABBITMQ_QUEUE_NAME") # The queue from which we (vrc_online_checker) receive requests
RESULT_QUEUE_NAME = os.getenv("RABBITMQ_RESULT_QUEUE") # The queue to which we send results back

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
# VRChat Login
# -------------------------------------------------------------------
def login_to_vrchat():
    """Logs into VRChat and handles possible 2FA prompts (SMS or Email)."""
    configuration = vrchatapi.Configuration(
        username=VRCHAT_USERNAME,
        password=VRCHAT_PASSWORD
    )
    api_client = vrchatapi.ApiClient(configuration)
    api_client.user_agent = "VRCVerifyBot/1.0 (contact@yourdomain.com)"

    auth_api = authentication_api.AuthenticationApi(api_client)
    try:
        current_user = auth_api.get_current_user()
        print(f"✅ Successfully logged in as {current_user.display_name}")
        return api_client
    except UnauthorizedException as e:
        # VRChat can respond with 200 but still require 2FA
        if e.status == 200:
            if "Email 2 Factor Authentication" in e.reason:
                two_factor_code = input("🔒 Enter VRChat Email 2FA Code: ")
                auth_api.verify2_fa_email_code(
                    two_factor_email_code=TwoFactorEmailCode(two_factor_code)
                )
            elif "2 Factor Authentication" in e.reason:
                two_factor_code = input("🔒 Enter VRChat 2FA Code: ")
                auth_api.verify2_fa(
                    two_factor_auth_code=TwoFactorAuthCode(two_factor_code)
                )
            else:
                print("❌ Unknown 2FA challenge. Exiting...")
                return None
            # Retry
            current_user = auth_api.get_current_user()
            print(f"✅ Successfully logged in as {current_user.display_name}")
            return api_client
        else:
            print(f"❌ VRChat Login Failed: {e}")
            return None

# One-time login
vrchat_api_client = login_to_vrchat()
if not vrchat_api_client:
    print("❌ VRChat login failed. Exiting soon...")

# -------------------------------------------------------------------
# Verification Logic
# -------------------------------------------------------------------
def check_vrc_user_has_code_and_age(vrc_user_id: str, verification_code: str | None) -> bool:
    """
    Checks if:
      - The vrc_user_id is valid
      - The user's age_verification_status is "18+"
      - (If verification_code is not None) the user's bio contains that code
    Returns True if 18+ AND (code is in bio if code is provided), otherwise False.
    """
    if not vrchat_api_client:
        print("❌ VRChat session not active. Skipping verification.")
        return False

    users_api_instance = users_api.UsersApi(vrchat_api_client)
    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except ApiException as e:
        print(f"❌ Failed to fetch VRChat user {vrc_user_id}. Error: {e}")
        return False

    age_status = getattr(vrc_user, "age_verification_status", "unknown")
    bio = getattr(vrc_user, "bio", "")

    print(f"[check_vrc_user_has_code_and_age] user={vrc_user_id}, age_status={age_status}, bio={bio}")

    # Must be 18+
    if age_status != "18+":
        return False

    # If a code is provided, ensure it's in the bio
    if verification_code and verification_code not in bio:
        return False

    return True

# -------------------------------------------------------------------
# RabbitMQ Callbacks
# -------------------------------------------------------------------
def process_verification_request(ch, method, properties, body):
    """
    The bot sends us JSON with:
      - discordID
      - vrcUserID
      - guildID
      - verificationCode (possibly None)
    We'll check VRChat, then publish a result back to RESULT_QUEUE_NAME.
    """
    data = json.loads(body)
    discord_id = data.get("discordID")
    vrc_user_id = data.get("vrcUserID")
    guild_id = data.get("guildID")
    verification_code = data.get("verificationCode")
    code_found=False

    print(f"🔎 Received verification request: {data}")

    if verification_code:
        code_found=True

    # If verification_code is None => "re-check" approach. 
    # If not None => "new code check" approach. 
    user_is_18_plus = check_vrc_user_has_code_and_age(vrc_user_id, verification_code)

    # Create the result object
    result = {
        "discordID": discord_id,
        "vrcUserID": vrc_user_id,
        "guildID": guild_id,
        "is_18_plus": user_is_18_plus,
        # Optionally return the verificationCode so the bot can match it exactly
        "verificationCode": verification_code,
        "code_found": code_found
    }

    send_verification_result(result)

def send_verification_result(result: dict):
    """Publish the verification result to the queue the bot is listening on."""
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

    print(f"📤 Sent verification result to '{RESULT_QUEUE_NAME}': {message_str}")

def listen_for_verifications():
    """Blocking function that listens for new requests from the bot."""
    if not vrchat_api_client:
        print("⚠️ VRChat login was not successful. We might fail all requests.")
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)

    channel.basic_consume(
        queue=RABBITMQ_QUEUE_NAME,
        on_message_callback=process_verification_request,
        auto_ack=True
    )
    print(f"✅ Listening for verification requests on '{RABBITMQ_QUEUE_NAME}'...")
    channel.start_consuming()

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    if not vrchat_api_client:
        print("❌ VRChat login failed. Exiting...")
    else:
        listen_for_verifications()
