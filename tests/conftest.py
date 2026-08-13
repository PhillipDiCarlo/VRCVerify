"""Shared test setup.

bot.py and vrc_online_checker.py read environment variables and open a
database engine at import time, so we pin safe test values BEFORE any test
module imports them. This also guarantees tests can never touch the real
Postgres/RabbitMQ/VRChat credentials from .env (load_dotenv does not
override variables that are already set).
"""

import os
import sys

TEST_ENV = {
    "DATABASE_URL": "sqlite:///:memory:",
    "DISCORD_BOT_TOKEN": "test-token",
    "RABBITMQ_HOST": "localhost",
    "RABBITMQ_PORT": "5672",
    "RABBITMQ_USERNAME": "guest",
    "RABBITMQ_PASSWORD": "guest",
    "RABBITMQ_VHOST": "/",
    "RABBITMQ_QUEUE_NAME": "test_verification_requests",
    "RABBITMQ_RESULT_QUEUE": "test_verification_results",
    "VRCHAT_USERNAME": "test-user",
    "VRCHAT_PASSWORD": "test-password",
    "GMAIL_USER": "test@example.com",
    "GMAIL_APP_PASSWORD": "test-app-password",
    "LOG_LEVEL": "WARNING",
}

os.environ.update(TEST_ENV)

# The grandfather line is captured into the database at launch, and the env var
# is only an escape hatch. A developer's local .env setting it would silently
# override that capture for the whole suite -- tests would then pass or fail
# depending on an untracked file. Set it empty rather than deleting it: an
# absent variable is one load_dotenv() would happily fill in from .env, while
# an empty one it leaves alone and _optional_int_env reads as "not set".
os.environ["PREMIUM_GRANDFATHER_MAX_ID"] = ""

# Same problem, same fix. With DASHBOARD_URL set, several command replies
# attach a link button, and a developer whose .env points at the real dashboard
# would see a dozen tests fail that pass in CI -- on the contents of an
# untracked file. Tests that care about the button set bot.DASHBOARD_URL
# directly, which is the honest way to ask for one.
os.environ["DASHBOARD_URL"] = ""

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
