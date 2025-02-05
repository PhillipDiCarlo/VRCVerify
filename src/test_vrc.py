import time
import imaplib
import email
import re
import os
import vrchatapi
from vrchatapi.api import authentication_api, users_api
from vrchatapi.exceptions import UnauthorizedException
from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

VRCHAT_USERNAME = os.getenv("VRCHAT_USERNAME")
VRCHAT_PASSWORD = os.getenv("VRCHAT_PASSWORD")

# Gmail IMAP Credentials
GMAIL_USER = os.getenv("GMAIL_USER")  
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")  

# -------------------------------
# Function to Fetch 2FA Code from Gmail (Extract from Subject)
# -------------------------------
def fetch_latest_2fa_code():
    """Waits for VRChat's 2FA email and retrieves the code from the subject line."""
    print("⏳ Waiting 10 seconds for 2FA email to arrive...")
    time.sleep(10)  # Initial wait to allow the email to arrive

    retries = 3  # Number of times to retry
    wait_time = 5  # Seconds between retries

    for attempt in range(retries):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("inbox")

            print(f"📩 Checking for VRChat 2FA email (Attempt {attempt + 1}/{retries})...")

            # Search for the latest VRChat 2FA email
            status, messages = mail.search(None, 'FROM "noreply@vrchat.com"')

            if not messages[0]:
                print("❌ No VRChat 2FA emails found yet.")
                time.sleep(wait_time)  # Wait before retrying
                continue

            latest_email_id = messages[0].split()[-1]  # Get the most recent email
            status, data = mail.fetch(latest_email_id, "(BODY[HEADER.FIELDS (SUBJECT)])")

            # Extract subject
            raw_subject = data[0][1].decode()
            subject_match = re.search(r"Your One-Time Code is (\d{6})", raw_subject)

            if subject_match:
                vrchat_2fa_code = subject_match.group(1)
                print(f"✅ Found VRChat 2FA Code: {vrchat_2fa_code}")
                return vrchat_2fa_code

            print("❌ No 2FA code found in email subject. Retrying...")
            time.sleep(wait_time)  # Wait before retrying

        except Exception as e:
            print(f"❌ Error fetching 2FA code from Gmail: {e}")

        finally:
            try:
                mail.logout()
            except:
                pass  # Ignore errors when logging out

    print("❌ Failed to retrieve VRChat 2FA code after multiple attempts.")
    return None

# -------------------------------
# VRChat Login with Auto 2FA
# -------------------------------
def login_to_vrchat():
    """Logs into VRChat and handles possible 2FA prompts automatically."""
    configuration = vrchatapi.Configuration(
        username=VRCHAT_USERNAME,
        password=VRCHAT_PASSWORD
    )

    with vrchatapi.ApiClient(configuration) as api_client:
        api_client.user_agent = "VRCVerifyBot/1.0 (contact@yourdomain.com) my@email.com"

        auth_api = authentication_api.AuthenticationApi(api_client)

        try:
            current_user = auth_api.get_current_user()
            print(f"✅ Successfully logged in as {current_user.display_name}")
            return api_client

        except UnauthorizedException as e:
            if e.status == 200:
                print("🔒 2FA Required! Fetching code from email...")
                
                # Auto-fetch the 2FA code
                two_factor_code = fetch_latest_2fa_code()

                if not two_factor_code:
                    print("❌ 2FA Required but no valid code found. Exiting.")
                    return None

                if "Email 2 Factor Authentication" in e.reason:
                    auth_api.verify2_fa_email_code(two_factor_email_code=TwoFactorEmailCode(two_factor_code))

                elif "2 Factor Authentication" in e.reason:
                    auth_api.verify2_fa(two_factor_auth_code=TwoFactorAuthCode(two_factor_code))

                # Retry login after submitting 2FA
                current_user = auth_api.get_current_user()
                print(f"✅ Successfully logged in as {current_user.display_name}")
                return api_client

            else:
                print("❌ VRChat Login Failed:", e)
                return None

        except vrchatapi.ApiException as e:
            print("❌ VRChat API Error:", e)
            return None

def check_vrc_user_info(vrc_user_id):
    """Fetches VRChat user info, including age verification status and bio."""
    api_client = login_to_vrchat()
    if not api_client:
        print("❌ Failed to log into VRChat. Exiting.")
        return

    users_api_instance = users_api.UsersApi(api_client)

    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except vrchatapi.ApiException as e:
        print(f"❌ Failed to fetch VRChat user {vrc_user_id}. Error: {e}")
        return

    age_verification_status = getattr(vrc_user, "age_verification_status", "unknown")
    user_bio = getattr(vrc_user, "bio", "No bio available")

    print(f"User {vrc_user_id} age verification status: {age_verification_status}")
    print(f"User {vrc_user_id} bio: {user_bio}")

    return {
        "vrcUserID": vrc_user_id,
        "age_verification_status": age_verification_status,
        "bio": user_bio
    }

# Test the login and verification process
vrc_user_id = "usr_a08f0340-2774-4ee0-9f1e-5c06d8404745"
user_info = check_vrc_user_info(vrc_user_id)
