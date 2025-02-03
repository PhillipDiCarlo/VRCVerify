import vrchatapi
from vrchatapi.api import authentication_api, users_api
from vrchatapi.exceptions import UnauthorizedException
from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

VRCHAT_USERNAME = os.getenv("VRCHAT_USERNAME")
VRCHAT_PASSWORD = os.getenv("VRCHAT_PASSWORD")

def login_to_vrchat():
    """Logs into VRChat and returns an authenticated API client."""
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
                if "Email 2 Factor Authentication" in e.reason:
                    auth_api.verify2_fa_email_code(two_factor_email_code=TwoFactorEmailCode(input("Email 2FA Code: ")))
                elif "2 Factor Authentication" in e.reason:
                    auth_api.verify2_fa(two_factor_auth_code=TwoFactorAuthCode(input("2FA Code: ")))
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
