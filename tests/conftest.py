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

def local_database_only(url):
    """Reason to refuse `url` as a test database, or None to allow it.

    THE POINT IS NOT TIDINESS. Pointing the suite at a database means pointing
    the suite's teardown at it, and the teardown deletes rows -- that is its
    job. `session.query(Server).delete()` run against production is exactly
    what emptied the servers table on 2026-09-08 (VPS_RUNBOOK.md section 14b). A
    Postgres run of this suite is therefore a loaded gun by construction, and
    the safety is that the barrel may only point at localhost.

    Deliberately an allowlist of loopback hosts rather than a denylist of known
    production addresses: a denylist is wrong the first time production moves,
    and it is wrong silently.

    Pure, so it is testable without a database -- see test_test_database_guard.
    """
    if url.startswith("sqlite"):
        return None
    without_scheme = url.split("://", 1)[-1]
    # The credential half may contain an @, so split from the right.
    authority = without_scheme.rsplit("@", 1)[-1]
    host = authority.split("/")[0].rsplit(":", 1)[0].strip("[]")
    if host in ("localhost", "127.0.0.1", "::1"):
        return None
    return (
        f"VRCVERIFY_TEST_DATABASE_URL points at {host!r}, which is not"
        " loopback. The suite's teardown deletes rows, so it may only be aimed"
        " at a disposable local database. To reproduce something against a"
        " real schema, restore a copy locally and point this at that copy --"
        " never at production. See scripts/test_postgres.sh."
    )


# Opt-in Postgres run (#275). SQLite stays the default because it is fast and
# needs nothing installed, and because most of what this suite asserts is
# behavior rather than storage. What SQLite cannot do is reproduce a
# disagreement about a column's TYPE -- it gives VARCHAR columns TEXT affinity,
# so an integer written into one comes back as a string, which is how #164
# reached production with two regression tests passing over it.
#
# scripts/test_postgres.sh sets this to a throwaway container seeded from
# schema/production.sql. Seeded from the DUMP rather than from create_all(),
# because create_all() would build the columns the models describe and the
# models are the half of the disagreement that was never in doubt.
_test_database_url = os.environ.get("VRCVERIFY_TEST_DATABASE_URL", "").strip()
if _test_database_url:
    _refusal = local_database_only(_test_database_url)
    if _refusal is not None:
        raise RuntimeError(_refusal)
    TEST_ENV["DATABASE_URL"] = _test_database_url
    # bot.py refuses a non-SQLite URL on import (#273), which is the guard that
    # makes an ad-hoc script safe and which must stay armed. Opening it here
    # only after local_database_only() has passed means the override is granted
    # to a proven-loopback URL and to nothing else -- the guard is narrowed to
    # this one case rather than switched off for the suite.
    os.environ["VRCVERIFY_ALLOW_DB_IMPORT"] = "1"


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

# Same again for the panel's website button (#240). With WEBSITE_URL set the
# instruction panel grows a third button, so a developer whose .env points at
# the real site would see the panel-shape assertions fail on the contents of an
# untracked file. Tests that want the button set bot.WEBSITE_URL directly.
os.environ["WEBSITE_URL"] = ""

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
# this bot -- ten tests fail. They are the tests asserting behavior "while the
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

# Fifth, and this one is a timer rather than a switch. Issue #49's invite gate
# is written in terms of these three numbers, and several tests assert on which
# side of them a request falls -- "a transient failure may not be retried
# immediately" is only true while the cooldown is not zero. A developer who
# tuned any of them in their own .env would move the line those tests are
# measuring against, and the suite would disagree with itself per machine for
# reasons no diff explains.
#
# Empty rather than a chosen value: _int_env falls back to the default written
# in bot.py, so the suite measures against the numbers the code actually ships
# with. Tests that need a different timing monkeypatch the module attribute.
os.environ["GROUP_INVITE_TIMEOUT_SECONDS"] = ""
os.environ["GROUP_INVITE_COOLDOWN_SECONDS"] = ""
os.environ["INVITE_MIN_SPACING_SECONDS"] = ""

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
