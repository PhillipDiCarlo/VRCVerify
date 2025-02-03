import vrchatapi
import psycopg2
import pika
import json
import os
from dotenv import load_dotenv
from vrchatapi.api import authentication_api, users_api
from vrchatapi.exceptions import UnauthorizedException
from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode

# Load environment variables
load_dotenv()

VRCHAT_USERNAME = os.getenv("VRCHAT_USERNAME")
VRCHAT_PASSWORD = os.getenv("VRCHAT_PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL")

# RabbitMQ Configuration
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")
RABBITMQ_QUEUE_NAME = os.getenv("RABBITMQ_QUEUE_NAME")

# RabbitMQ setup
credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST,
    port=RABBITMQ_PORT,
    virtual_host=RABBITMQ_VHOST,
    credentials=credentials
)

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    return psycopg2.connect(DATABASE_URL)

def login_to_vrchat():
    """Logs into VRChat and persists session after 2FA verification."""
    configuration = vrchatapi.Configuration(
        username=VRCHAT_USERNAME,
        password=VRCHAT_PASSWORD
    )

    api_client = vrchatapi.ApiClient(configuration)
    api_client.user_agent = "VRCVerifyBot/1.0 (contact@yourdomain.com) my@email.com"

    auth_api = authentication_api.AuthenticationApi(api_client)

    try:
        current_user = auth_api.get_current_user()
        print(f"✅ Successfully logged in as {current_user.display_name}")
        return api_client
    except UnauthorizedException as e:
        if e.status == 200:
            two_factor_code = None
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

            current_user = auth_api.get_current_user()
            print(f"✅ Successfully logged in as {current_user.display_name}")
            return api_client
        else:
            print("❌ VRChat Login Failed:", e)
            return None

# Store the VRChat session globally so it persists
vrchat_api_client = login_to_vrchat()

def verify_vrc_user(discord_id, vrc_user_id, verification_code):
    """Fetches VRChat user info, checks for the verification code in the bio, and updates the database."""
    global vrchat_api_client

    if not vrchat_api_client:
        print("❌ VRChat session is not active. Skipping verification.")
        return

    users_api_instance = users_api.UsersApi(vrchat_api_client)

    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except vrchatapi.ApiException as e:
        print(f"❌ Failed to fetch VRChat user {vrc_user_id}. Error: {e}")
        return

    age_verification_status = getattr(vrc_user, "age_verification_status", "unknown")
    user_bio = getattr(vrc_user, "bio", "No bio available")

    print(f"User {vrc_user_id} age verification status: {age_verification_status}")
    print(f"User {vrc_user_id} bio: {user_bio}")

    if verification_code in user_bio:
        is_18_plus = age_verification_status == "18+"

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (discord_id, vrc_user_id, verification_status)
            VALUES (%s, %s, %s)
            ON CONFLICT (discord_id) DO UPDATE
            SET vrc_user_id = EXCLUDED.vrc_user_id, verification_status = EXCLUDED.verification_status
            """,
            (discord_id, vrc_user_id, is_18_plus)
        )
        conn.commit()
        cur.close()
        conn.close()

        print(f"✅ User {vrc_user_id} is verified! 18+ Status: {is_18_plus}")

        return {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "is_18_plus": is_18_plus,
            "verified": True
        }
    else:
        print(f"❌ Verification code not found in {vrc_user_id}'s bio.")
        return {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "is_18_plus": False,
            "verified": False
        }

def process_verification_request(ch, method, properties, body):
    """Processes verification requests from RabbitMQ."""
    data = json.loads(body)
    discord_id = data["discordID"]
    vrc_user_id = data["vrcUserID"]
    verification_code = data["verificationCode"]

    print(f"🔎 Received verification request for {discord_id} -> {vrc_user_id}")

    verification_result = verify_vrc_user(discord_id, vrc_user_id, verification_code)
    if verification_result:
        send_verification_result(verification_result)

def send_verification_result(result):
    """Sends the verification result back to the bot through RabbitMQ."""
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue="vrc_verification_results", durable=True)

    channel.basic_publish(
        exchange="",
        routing_key="vrc_verification_results",
        body=json.dumps(result),
    )

    print(f"📤 Sent verification result for {result['discordID']} to RabbitMQ.")
    connection.close()

def listen_for_verifications():
    """Listens for new verification requests from RabbitMQ."""
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)

    channel.basic_consume(queue=RABBITMQ_QUEUE_NAME, on_message_callback=process_verification_request, auto_ack=True)
    print("✅ Listening for verification requests...")
    channel.start_consuming()

if __name__ == "__main__":
    if vrchat_api_client:
        listen_for_verifications()
    else:
        print("❌ VRChat login failed. Exiting...")
