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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
