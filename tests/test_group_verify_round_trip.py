"""The dashboard -> bot -> queue -> worker -> back round trip (issue #49, phase 4).

The bot had no request/response hop before this: everything else it publishes
is fire-and-forget, and everything it consumes is a verification result keyed
by Discord id. This one has to survive an answer arriving late, twice, or not
at all, and it has to be impossible for the request to name the group.

That last point is the one worth stating plainly. The worker joins a VRChat
group when it is told to. If the group could come from the request body,
anyone who reached the endpoint could park the bot in a group of their
choosing -- so the job is built from the stored settings row and the caller
supplies only a guild id it has already been authorised for.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import bot


GUILD_ID = 987654321
ADMIN_ID = 4242
OWNER_ID = 77
SKU_ID = 555000111
GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"
OTHER_GROUP_ID = "grp_11111111-2222-3333-4444-555555555555"
ACCOUNT_ID = "usr_0e59962a-3e0d-4303-802b-9314623027e5"
ICON_URL = "https://api.vrchat.cloud/api/1/file/file_5ec52378/1/file"


def run(coro):
    return asyncio.run(coro)


def make_server(server_id=str(GUILD_ID), row_id=10, **overrides):
    fields = dict(
        id=row_id,
        server_id=server_id,
        owner_id=str(OWNER_ID),
        role_id="1",
        instructions_locale="en-US",
    )
    fields.update(overrides)
    with bot.session_scope() as session:
        session.add(bot.Server(**fields))


def configure_group(group_id=GROUP_ID, enabled=True):
    bot.save_group_invite_config(GUILD_ID, group_id=group_id, enabled=enabled)
    return bot.load_group_invite_config(GUILD_ID)


def stored():
    return bot.load_group_invite_config(GUILD_ID)


def audit_fields():
    with bot.session_scope() as session:
        return [row.field for row in session.query(bot.DashboardAudit).all()]


def result_payload(job_id, **overrides):
    """Shaped exactly as vrc_group_inviter._result builds it."""
    payload = {
        "type": "verify_group_setup",
        "jobID": job_id,
        "guildID": str(GUILD_ID),
        "groupID": GROUP_ID,
        "ok": True,
        "state": bot.GROUP_SETUP_READY,
        "can_invite": True,
        "can_see_members": True,
        "group_name": "Club LA",
        "icon_url": ICON_URL,
        "error_message": None,
        "accountID": ACCOUNT_ID,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with bot.session_scope() as session:
            session.query(bot.Server).delete()
            session.query(bot.GroupInviteConfig).delete()
            session.query(bot.DashboardAudit).delete()
            session.query(bot.PremiumGrandfatherLine).delete()

    wipe()
    bot.premium_status_cache.clear()
    yield
    wipe()
    bot.premium_status_cache.clear()


@pytest.fixture(autouse=True)
def account_configured(monkeypatch):
    """An invite account exists, as it does in any real deployment."""
    monkeypatch.setattr(bot, "INVITE_VRCHAT_USER_ID", ACCOUNT_ID)


@pytest.fixture
def published(monkeypatch):
    """Collect the jobs that would have gone on the queue."""
    jobs = []

    def fake_publish(job):
        jobs.append(job)
        return True

    monkeypatch.setattr(bot, "publish_group_invite_job", fake_publish)
    return jobs


@pytest.fixture
def publishing_fails(monkeypatch):
    def fake_publish(job):
        return False

    monkeypatch.setattr(bot, "publish_group_invite_job", fake_publish)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(bot, "PREMIUM_SKU_ID", SKU_ID)
    monkeypatch.setattr(bot, "PREMIUM_ENFORCED", True)
    bot.premium_status_cache.clear()


@pytest.fixture
def free(monkeypatch, enforced):
    async def no(guild_id):
        return False

    monkeypatch.setattr(bot, "guild_has_premium", no)
    with bot.session_scope() as session:
        session.add(bot.PremiumGrandfatherLine(id=1, max_server_id=1))
    bot.premium_status_cache.clear()


# -------------------------------------------------------------------
# Building the job
# -------------------------------------------------------------------
class TestBuildingTheJob:
    def test_a_guild_with_no_row_has_nothing_to_check(self):
        assert bot.begin_group_verification(GUILD_ID) is None

    def test_a_row_with_no_group_has_nothing_to_check(self):
        configure_group(group_id=None, enabled=True)
        assert bot.begin_group_verification(GUILD_ID) is None

    def test_the_job_carries_the_stored_group_and_code(self):
        config = configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        assert job["type"] == bot.JOB_VERIFY_GROUP_SETUP
        assert job["groupID"] == GROUP_ID
        assert job["claimCode"] == config["claim_code"]
        assert job["guildID"] == str(GUILD_ID)
        assert job["jobID"]

    def test_the_job_has_no_field_a_caller_could_supply(self):
        """The security property, as an assertion rather than a comment.

        Every key is either the bot's own bookkeeping or read from the stored
        row. A new one that came from a request body is how this endpoint
        would turn into "make the invite account join whatever I name".
        """
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        assert set(job) == {
            "type",
            "jobID",
            "guildID",
            "groupID",
            "claimCode",
            "requireCode",
        }

    def test_a_new_group_must_prove_itself(self):
        configure_group()
        assert bot.begin_group_verification(GUILD_ID)["requireCode"] is True

    def test_a_group_already_verified_need_not_prove_itself_again(self):
        """So an admin may tidy the code out of their description afterwards."""
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(result_payload(job["jobID"]))
        assert bot.begin_group_verification(GUILD_ID)["requireCode"] is False

    def test_changing_the_group_demands_proof_again(self):
        """`verified_at` is cleared with everything else about the old group,
        so this falls out of the reset rather than being a second rule."""
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(result_payload(job["jobID"]))
        bot.save_group_invite_config(GUILD_ID, group_id=OTHER_GROUP_ID, enabled=True)
        assert bot.begin_group_verification(GUILD_ID)["requireCode"] is True

    def test_asking_marks_the_row_as_checking(self):
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        row = stored()
        assert row["verify_state"] == bot.GROUP_SETUP_CHECKING
        assert row["verify_job_id"] == job["jobID"]
        assert row["verify_requested_at"] is not None
        assert row["verify_error"] is None

    def test_asking_again_replaces_the_job_id(self):
        """So the first answer becomes stale rather than racing the second."""
        configure_group()
        first = bot.begin_group_verification(GUILD_ID)
        second = bot.begin_group_verification(GUILD_ID)
        assert first["jobID"] != second["jobID"]
        assert stored()["verify_job_id"] == second["jobID"]

    def test_a_previous_error_is_cleared_while_checking(self):
        configure_group()
        bot.record_group_verification_result(
            result_payload(
                bot.begin_group_verification(GUILD_ID)["jobID"],
                state=bot.GROUP_SETUP_NOT_INVITED,
                ok=False,
                error_message="not invited",
            )
        )
        assert stored()["verify_error"] == "not invited"
        bot.begin_group_verification(GUILD_ID)
        assert stored()["verify_error"] is None


# -------------------------------------------------------------------
# Storing the answer
# -------------------------------------------------------------------
class TestStoringTheAnswer:
    def test_a_matching_answer_is_applied(self):
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        assert bot.record_group_verification_result(result_payload(job["jobID"])) == "applied"

        row = stored()
        assert row["verify_state"] == bot.GROUP_SETUP_READY
        assert row["can_invite"] is True
        assert row["can_see_members"] is True
        assert row["group_name"] == "Club LA"
        assert row["invite_account_id"] == ACCOUNT_ID
        assert row["verified_at"] is not None
        # Answered, so the question is closed.
        assert row["verify_job_id"] is None

    def test_a_failure_is_stored_without_a_verified_timestamp(self):
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(
            result_payload(
                job["jobID"],
                ok=False,
                state=bot.GROUP_SETUP_NO_INVITE_PERMISSION,
                can_invite=False,
                error_message="The bot is in the group but cannot invite",
            )
        )
        row = stored()
        assert row["verify_state"] == bot.GROUP_SETUP_NO_INVITE_PERMISSION
        assert row["can_invite"] is False
        assert row["verified_at"] is None
        assert row["verify_error"] == "The bot is in the group but cannot invite"

    def test_an_answer_to_an_old_question_is_dropped(self):
        """The reason verify_job_id exists.

        A slow answer about a group the admin has already replaced would
        otherwise overwrite a fast answer about the new one, and the page would
        show a verdict about a group it is no longer describing.
        """
        configure_group()
        first = bot.begin_group_verification(GUILD_ID)
        second = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(result_payload(second["jobID"]))

        assert bot.record_group_verification_result(
            result_payload(first["jobID"], state=bot.GROUP_SETUP_GROUP_NOT_FOUND, ok=False)
        ) == "stale"
        assert stored()["verify_state"] == bot.GROUP_SETUP_READY

    def test_a_duplicate_delivery_is_not_applied_twice(self):
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        assert bot.record_group_verification_result(result_payload(job["jobID"])) == "applied"
        assert bot.record_group_verification_result(result_payload(job["jobID"])) == "stale"

    def test_an_answer_about_a_replaced_group_is_dropped(self):
        """Changing the group clears the pending job, so its answer is stale.

        Without this the row could end up saying "ready" about a group id it
        no longer holds -- and phase 5 would send invites on the strength of it.
        """
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.save_group_invite_config(GUILD_ID, group_id=OTHER_GROUP_ID, enabled=True)

        assert bot.record_group_verification_result(result_payload(job["jobID"])) == "stale"
        row = stored()
        assert row["group_id"] == OTHER_GROUP_ID
        assert row["verify_state"] == bot.GROUP_SETUP_UNVERIFIED

    def test_an_answer_for_a_guild_with_no_row_is_dropped(self):
        assert bot.record_group_verification_result(result_payload("nope")) == "unknown_guild"

    @pytest.mark.parametrize("payload", [None, "not a dict", {}, {"guildID": "1"}])
    def test_a_malformed_payload_is_dropped(self, payload):
        assert bot.record_group_verification_result(payload) == "bad_payload"

    def test_a_state_this_bot_does_not_know_is_refused(self):
        """Stored verbatim it would render as nothing at all, which reads to
        an admin as the check never having happened."""
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        assert bot.record_group_verification_result(
            result_payload(job["jobID"], state="something_new")
        ) == "unknown_state"
        assert stored()["verify_state"] == bot.GROUP_SETUP_CHECKING

    def test_a_worker_that_does_not_name_itself_falls_back_to_our_account(self):
        """One invite account today. A worker predating the field is not a
        reason to lose which account joined."""
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        payload = result_payload(job["jobID"])
        del payload["accountID"]
        bot.record_group_verification_result(payload)
        assert stored()["invite_account_id"] == ACCOUNT_ID


# -------------------------------------------------------------------
# When nothing answers
# -------------------------------------------------------------------
class TestTheTimeout:
    def config_checking(self, seconds_ago):
        configure_group()
        bot.begin_group_verification(GUILD_ID)
        with bot.session_scope() as session:
            row = session.query(bot.GroupInviteConfig).first()
            row.verify_requested_at = datetime.now(timezone.utc) - timedelta(
                seconds=seconds_ago
            )
        return stored()

    def test_a_recent_request_is_still_checking(self):
        config = self.config_checking(5)
        assert bot.effective_group_setup_state(config) == bot.GROUP_SETUP_CHECKING

    def test_an_old_request_has_timed_out(self):
        config = self.config_checking(bot.GROUP_VERIFY_TIMEOUT_SECONDS + 30)
        assert bot.effective_group_setup_state(config) == bot.GROUP_SETUP_TIMED_OUT

    def test_checking_with_no_timestamp_is_already_lost(self):
        """Nothing could ever expire it, so a spinner would run for ever."""
        configure_group()
        bot.begin_group_verification(GUILD_ID)
        with bot.session_scope() as session:
            session.query(bot.GroupInviteConfig).first().verify_requested_at = None
        assert bot.effective_group_setup_state(stored()) == bot.GROUP_SETUP_TIMED_OUT

    def test_every_other_state_passes_through(self):
        for state in bot.GROUP_SETUP_STATES - {bot.GROUP_SETUP_CHECKING}:
            assert bot.effective_group_setup_state({"verify_state": state}) == state

    def test_no_config_reads_as_unverified(self):
        assert bot.effective_group_setup_state(None) == bot.GROUP_SETUP_UNVERIFIED
        assert bot.effective_group_setup_state({}) == bot.GROUP_SETUP_UNVERIFIED

    def test_the_payload_reports_the_expired_state(self):
        make_server()
        self.config_checking(bot.GROUP_VERIFY_TIMEOUT_SECONDS + 30)
        payload = run(bot.read_dashboard_settings(GUILD_ID))
        assert payload["group_invite"]["state"] == bot.GROUP_SETUP_TIMED_OUT

    def test_reading_does_not_rewrite_the_row(self):
        """Expiry is a view of the stored value, not an edit of it. A read that
        wrote would need a transaction on every page load, and a late answer
        would then have nothing to match against."""
        make_server()
        self.config_checking(bot.GROUP_VERIFY_TIMEOUT_SECONDS + 30)
        run(bot.read_dashboard_settings(GUILD_ID))
        assert stored()["verify_state"] == bot.GROUP_SETUP_CHECKING
        assert stored()["verify_job_id"] is not None


# -------------------------------------------------------------------
# Asking for a check
# -------------------------------------------------------------------
class TestRequestingACheck:
    def test_the_happy_path_publishes_and_reports_checking(self, published):
        make_server()
        configure_group()
        payload = run(bot.request_group_verification(GUILD_ID, ADMIN_ID))

        assert len(published) == 1
        assert published[0]["groupID"] == GROUP_ID
        assert payload["group_invite"]["state"] == bot.GROUP_SETUP_CHECKING
        assert audit_fields() == ["group_verify"]

    def test_a_free_server_is_refused_and_nothing_is_published(self, free, published):
        make_server(row_id=9000)
        configure_group()
        with pytest.raises(bot.SettingRejected) as caught:
            run(bot.request_group_verification(GUILD_ID, ADMIN_ID))
        assert caught.value.reason == "requires_premium"
        assert caught.value.locked is True
        assert published == []

    def test_a_guild_with_no_group_is_told_so(self, published):
        make_server()
        with pytest.raises(bot.SettingRejected) as caught:
            run(bot.request_group_verification(GUILD_ID, ADMIN_ID))
        assert caught.value.reason == "no_group_configured"
        assert published == []

    def test_no_invite_account_is_an_operator_problem_not_the_admins(
        self, monkeypatch, published
    ):
        """503, not a refusal that blames the caller: there is no account for
        them to have invited, and nothing they can do about it."""
        monkeypatch.setattr(bot, "INVITE_VRCHAT_USER_ID", None)
        make_server()
        configure_group()
        assert run(bot.request_group_verification(GUILD_ID, ADMIN_ID)) is None
        assert published == []
        # Nothing was stamped either, so the page does not show a check that
        # was never asked for.
        assert stored()["verify_state"] == bot.GROUP_SETUP_UNVERIFIED

    def test_a_publish_failure_says_so_instead_of_spinning(self, publishing_fails):
        """Without the rollback the row sits in "checking" until the timeout,
        and the admin waits two minutes to be told something the bot knew at
        once."""
        make_server()
        configure_group()
        payload = run(bot.request_group_verification(GUILD_ID, ADMIN_ID))

        row = stored()
        assert row["verify_state"] == bot.GROUP_SETUP_WORKER_UNREACHABLE
        assert "worker" in (row["verify_error"] or "")
        assert row["verify_job_id"] is None
        assert payload["group_invite"]["state"] == bot.GROUP_SETUP_WORKER_UNREACHABLE
        # Nothing happened, so nothing is recorded as having happened.
        assert audit_fields() == []

    def test_a_late_failure_does_not_roll_back_a_newer_request(self, monkeypatch):
        """Two clicks: the first fails to publish after the second succeeded.

        Rolling back on job id rather than on guild means the first one's
        failure cannot clear the second one's pending check.
        """
        make_server()
        configure_group()
        first = bot.begin_group_verification(GUILD_ID)
        second = bot.begin_group_verification(GUILD_ID)
        bot.abandon_group_verification(GUILD_ID, first["jobID"], "too late")

        row = stored()
        assert row["verify_job_id"] == second["jobID"]
        assert row["verify_state"] == bot.GROUP_SETUP_CHECKING

    def test_the_published_job_is_the_stored_group_not_the_callers(self, published):
        """There is no argument through which a caller could name a group, and
        this is the test that keeps it that way."""
        make_server()
        configure_group()
        run(bot.request_group_verification(GUILD_ID, ADMIN_ID))
        assert published[0]["groupID"] == stored()["group_id"]
        assert published[0]["claimCode"] == stored()["claim_code"]


# -------------------------------------------------------------------
# The two processes have to agree
# -------------------------------------------------------------------
class TestTheContractWithTheWorker:
    def test_the_job_type_matches(self):
        import vrc_group_inviter as inviter

        assert bot.JOB_VERIFY_GROUP_SETUP == inviter.JOB_VERIFY_SETUP

    def test_the_queue_names_match(self):
        import vrc_group_inviter as inviter

        assert bot.RABBITMQ_GROUP_INVITE_QUEUE == inviter.REQUEST_QUEUE_NAME
        assert bot.RABBITMQ_GROUP_INVITE_RESULT_QUEUE == inviter.RESULT_QUEUE_NAME

    def test_a_real_worker_result_is_storable(self):
        """Built by the worker's own _result, not by a fixture that agrees with
        the bot by construction."""
        import vrc_group_inviter as inviter

        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        payload = inviter._result(
            {
                "jobID": job["jobID"],
                "guildID": job["guildID"],
                "groupID": job["groupID"],
            },
            inviter.STATE_READY,
            can_invite=True,
            can_see_members=True,
            group_name="Club LA",
        )
        assert bot.record_group_verification_result(payload) == "applied"
        assert stored()["verify_state"] == bot.GROUP_SETUP_READY
        assert stored()["can_invite"] is True


class TestTheGroupIcon:
    """Stored like the name, and forgotten like the name.

    The column arrived after the table shipped, so any database that already
    had group_invite_config needs one ALTER. bot.py's startup check names the
    statement in the log rather than leaving a driver error to be decoded.
    """

    def test_it_is_stored_with_the_rest_of_the_verdict(self):
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(result_payload(job["jobID"]))
        assert stored()["group_icon_url"] == ICON_URL

    def test_changing_the_group_forgets_it(self):
        """A picture of the old group is no more true about the new one than
        the old verdict was."""
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(result_payload(job["jobID"]))
        bot.save_group_invite_config(GUILD_ID, group_id=OTHER_GROUP_ID, enabled=True)
        assert stored()["group_icon_url"] is None

    def test_a_worker_that_sends_none_clears_it(self):
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(
            result_payload(job["jobID"], icon_url=None)
        )
        assert stored()["group_icon_url"] is None

    def test_the_payload_carries_it(self):
        make_server()
        configure_group()
        job = bot.begin_group_verification(GUILD_ID)
        bot.record_group_verification_result(result_payload(job["jobID"]))
        block = run(bot.read_dashboard_settings(GUILD_ID))["group_invite"]
        assert block["icon_url"] == ICON_URL

    def test_a_complete_database_says_nothing(self, caplog):
        """The suite's database is built by create_all, so it has the column.
        A check that shouted on a healthy deployment would be ignored on an
        unhealthy one."""
        with caplog.at_level("ERROR"):
            bot._warn_about_missing_columns()
        assert "group_icon_url" not in caplog.text

    def test_a_missing_column_is_named_with_the_statement_to_fix_it(
        self, monkeypatch, caplog
    ):
        """The symptom otherwise is every query against this table failing with
        a driver error that names a column and says nothing about a migration.
        Whoever reads that log is several steps from the answer."""

        class Blind:
            def get_table_names(self):
                return ["group_invite_config"]

            def get_columns(self, table):
                return [{"name": "server_id"}, {"name": "group_id"}]

        monkeypatch.setattr(bot, "inspect", lambda engine: Blind())
        with caplog.at_level("ERROR"):
            bot._warn_about_missing_columns()

        assert "group_icon_url" in caplog.text
        assert "ALTER TABLE group_invite_config" in caplog.text

    def test_a_database_without_the_table_at_all_is_left_alone(
        self, monkeypatch, caplog
    ):
        """create_all builds a missing table complete, so there is nothing to
        warn about -- and a warning here would fire on every fresh install."""

        class Empty:
            def get_table_names(self):
                return []

            def get_columns(self, table):  # pragma: no cover - never reached
                raise AssertionError("should not have been asked")

        monkeypatch.setattr(bot, "inspect", lambda engine: Empty())
        with caplog.at_level("ERROR"):
            bot._warn_about_missing_columns()
        assert caplog.text == ""
