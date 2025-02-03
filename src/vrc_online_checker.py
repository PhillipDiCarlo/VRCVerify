from flask import Flask, request, jsonify
import vrchatapi
from vrchatapi.api import authentication_api, users_api
from vrchatapi.exceptions import UnauthorizedException
from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
from dotenv import load_dotenv
import os
import psycopg2

# Load environment variables
load_dotenv()

VRCHAT_USERNAME = os.getenv("VRCHAT_USERNAME")
VRCHAT_PASSWORD = os.getenv("VRCHAT_PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL")

# Flask microservice
app = Flask(__name__)

# VRChat API Configuration
configuration = vrchatapi.Configuration(
    username=VRCHAT_USERNAME,
    password=VRCHAT_PASSWORD
)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def login_to_vrchat():
    """Logs into VRChat and returns the API client."""
    api_client = vrchatapi.ApiClient(configuration)
    api_client.user_agent = "VRCVerifyMicroservice/1.0 (contact@yourdomain.com) my@email.com"
    
    auth_api = authentication_api.AuthenticationApi(api_client)

    try:
        current_user = auth_api.get_current_user()
        return api_client, current_user
    except UnauthorizedException as e:
        if e.status == 200 and "2 Factor Authentication" in e.reason:
            two_factor_code = input("Enter VRChat 2FA Code: ")
            auth_api.verify2_fa(two_factor_auth_code=TwoFactorAuthCode(two_factor_code))
            current_user = auth_api.get_current_user()
            return api_client, current_user
        else:
            print("VRChat Login Failed:", e)
            return None, None
    except vrchatapi.ApiException as e:
        print("VRChat API Error:", e)
        return None, None

@app.route('/check_vrc_18plus', methods=['POST'])
def check_vrc_18plus():
    """Receives a VRChat user ID, checks their 18+ badge, and updates the database."""
    data = request.json
    vrc_user_id = data.get("vrcUserID")

    if not vrc_user_id:
        return jsonify({"error": "Missing vrcUserID"}), 400

    api_client, _ = login_to_vrchat()
    if not api_client:
        return jsonify({"error": "Failed to log into VRChat"}), 500

    users_api_instance = users_api.UsersApi(api_client)
    try:
        vrc_user = users_api_instance.get_user(vrc_user_id)
    except vrchatapi.ApiException as e:
        return jsonify({"error": "Failed to fetch VRChat user", "details": str(e)}), 500

    is_18_plus = getattr(vrc_user, "age_verified", False)

    # Update database
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET verification_status = %s WHERE vrc_user_id = %s", (is_18_plus, vrc_user_id))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"vrcUserID": vrc_user_id, "is_18_plus": is_18_plus})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5532)
