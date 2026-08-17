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

# Third time, same problem, same fix -- and this one was found the hard way.
# STRIPE_ENABLED went in with #88 step 1 and was not pinned here, so the moment
# a developer switched it on in their own .env, load_dotenv() carried it into
# bot.py at import and every kill-switch test in test_stripe.py went red on a
# file git has never seen. CI would have stayed green, which is the worse half:
# the suite would disagree with itself depending on whose machine ran it.
#
# Note this covers the dashboard's copy of the switch too. Nothing under
# src/dashboard/ calls load_dotenv, but bot.py does, and importing bot puts
# whatever .env says into os.environ for everything that runs afterwards.
#
# Tests that want Stripe on set bot.STRIPE_ENABLED directly (see the stripe_on
# fixture), which is the honest way to ask for it.
os.environ["STRIPE_ENABLED"] = ""
os.environ["STRIPE_STATUS_TTL"] = ""

# Fourth time, and the one the three notes above should have caught. The switch
# that turns the premium tier on was never pinned, so on any machine whose .env
# carries the production SKU -- which is every machine that has ever deployed
# this bot -- ten tests fail. They are the tests asserting behaviour "while the
# tier is off", and they cannot be off while PREMIUM_ENFORCED is true at import.
#
# It went unnoticed because it fails in the safe direction on a fresh checkout
# and the noisy direction only for the maintainer, who could reasonably read ten
# reds as a real regression in whatever they had just changed. With A-23 still
# open there is no CI to disagree with them, so "run the suite before merging"
# was resting on a suite that was already red for reasons unrelated to the diff.
#
# Tests that want the tier on use the `enforced` fixture, which sets both
# PREMIUM_SKU_ID and PREMIUM_ENFORCED on the bot module directly.
os.environ["PREMIUM_SKU_ID"] = ""

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
