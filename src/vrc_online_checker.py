import os
import json
import psycopg2
import pika
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

# The queue to which we send the result back
RESULT_QUEUE_NAME = "vrc_verification_results"

# -------------------------------------------------------------------
# RabbitMQ setup
# -------------------------------------------------------------------
credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST,
    port=RABBITMQ_PORT,
    virtual_host=RABBITMQ_VHOST,
    credentials=credentials
)

# -------------------------------------------------------------------
# Database connection
# -------------------------------------------------------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# -------------------------------------------------------------------
# VRChat login
# -------------------------------------------------------------------
def login_to_vrchat():
    """Logs into VRChat and handles possible 2FA prompts (SMS or Email)."""
    configuration = vrchatapi.Configuration(
        username=VRCHAT_USERNAME,
        password=VRCHAT_PASSWORD
    )
    # Optionally set user agent info
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

            # Retry checking user
            current_user = auth_api.get_current_user()
            print(f"✅ Successfully logged in as {current_user.display_name}")
            return api_client
        else:
            print(f"❌ VRChat Login Failed: {e}")
            return None

# Store the VRChat session globally
vrchat_api_client = login_to_vrchat()

# -------------------------------------------------------------------
# Verification Logic
# -------------------------------------------------------------------
def verify_with_code(discord_id, vrc_user_id, verification_code, guild_id):
    """
    Full check for new users:
      1) Make sure the code is in their VRChat bio.
      2) Check if their 'age_verification_status' is "18+".
    If both pass, set them verified in the DB.
    """
    if not vrchat_api_client:
        print("❌ VRChat session not active. Skipping verification.")
        return None

    users_api_instance = users_api.UsersApi(vrchat_api_client)
    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except ApiException as e:
        print(f"❌ Failed to fetch VRChat user {vrc_user_id}. Error: {e}")
        return None

    age_status = getattr(vrc_user, "age_verification_status", "unknown")
    bio = getattr(vrc_user, "bio", "")

    print(f"[verify_with_code] VRChat user {vrc_user_id} age_status={age_status}, bio={bio}")

    if verification_code in bio:
        # Check 18+ badge
        is_18_plus = (age_status == "18+")
        # Update DB
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (discord_id, vrc_user_id, verification_status)
            VALUES (%s, %s, %s)
            ON CONFLICT (discord_id) DO UPDATE
            SET vrc_user_id = EXCLUDED.vrc_user_id,
                verification_status = EXCLUDED.verification_status
            """,
            (discord_id, vrc_user_id, is_18_plus)
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ User {vrc_user_id} verified? {is_18_plus}")
        return {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "is_18_plus": is_18_plus
        }
    else:
        print(f"❌ Verification code not found in {vrc_user_id}'s bio.")
        # Not verified
        return {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "is_18_plus": False
        }

def verify_without_code(discord_id, vrc_user_id, guild_id):
    """
    "Re-check" for existing users who might have gained 18+ status since last time.
    We do NOT look for a code; we simply see if age_verification_status is "18+" now.
    """
    if not vrchat_api_client:
        print("❌ VRChat session not active. Skipping verification.")
        return None

    users_api_instance = users_api.UsersApi(vrchat_api_client)
    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except ApiException as e:
        print(f"❌ Failed to fetch VRChat user {vrc_user_id}. Error: {e}")
        return None

    age_status = getattr(vrc_user, "age_verification_status", "unknown")
    print(f"[verify_without_code] VRChat user {vrc_user_id} age_status={age_status}")

    is_18_plus = (age_status == "18+")
    # Update DB
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET verification_status = %s
        WHERE discord_id = %s
        """,
        (is_18_plus, discord_id)
    )
    conn.commit()
    cur.close()
    conn.close()

    return {
        "discordID": discord_id,
        "vrcUserID": vrc_user_id,
        "guildID": guild_id,
        "is_18_plus": is_18_plus
    }

# -------------------------------------------------------------------
# RabbitMQ Callbacks
# -------------------------------------------------------------------
def process_verification_request(ch, method, properties, body):
    """
    Process incoming requests from the bot. 
    The body should have:
      - discordID
      - vrcUserID
      - guildID
      - verificationCode (possibly None)
    """
    data = json.loads(body)
    discord_id = data["discordID"]
    vrc_user_id = data["vrcUserID"]
    guild_id = data["guildID"]
    verification_code = data.get("verificationCode")

    print(f"🔎 Received verification request for discordID={discord_id}, guildID={guild_id}, vrcUserID={vrc_user_id}, code={verification_code}")

    if verification_code:
        result = verify_with_code(discord_id, vrc_user_id, verification_code, guild_id)
    else:
        result = verify_without_code(discord_id, vrc_user_id, guild_id)

    if result is not None:
        send_verification_result(result)

def send_verification_result(result: dict):
    """
    Publishes the verification result to the 'vrc_verification_results' queue,
    which the bot consumes.
    """
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=RESULT_QUEUE_NAME, durable=True)

    channel.basic_publish(
        exchange="",
        routing_key=RESULT_QUEUE_NAME,
        body=json.dumps(result)
    )
    connection.close()

    print(f"📤 Sent verification result to {RESULT_QUEUE_NAME}: {result}")

def listen_for_verifications():
    """Blocking function that listens for new requests from the bot."""
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    # The queue name for incoming requests from the bot
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
