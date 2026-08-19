"""Unit tests for src/vrc_group_inviter.py.

Every VRChat call is faked. What these pin down is the behaviour confirmed
against a live group on 2026-08-19 (issue #49): that the invite permission is
its own thing an admin role can lack, that a non-member reads as
membership_status None rather than a 404, and that the worker never joins a
group it was not told about.
"""

from types import SimpleNamespace

import pytest

import vrc_group_inviter as inviter
import vrc_session as vrcs


GROUP_ID = "grp_0e1d4755-2f87-4129-a192-5587068cbf73"

JOB = {"type": "verify_group_setup", "jobID": "j1", "guildID": "123", "groupID": GROUP_ID}

# The two permissions a correctly configured role carries, and the noise that
# comes with the group's default member role.
READY_PERMISSIONS = [
    "group-instance-join",
    "group-instance-plus-portal",
    "group-invites-manage",
    "group-members-viewall",
]

# A real admin role that still cannot invite -- this exact set was observed.
ADMIN_WITHOUT_INVITE = [
    "group-announcement-manage",
    "group-galleries-manage",
    "group-instance-join",
    "group-members-manage",
    "group-members-viewall",
    "group-roles-assign",
]


class FakeApiException(Exception):
    def __init__(self, status=None, body=""):
        super().__init__(f"{status}: {body}")
        self.status = status
        self.body = body
        self.reason = body


def group(membership_status="member", permissions=None, name="Club LA"):
    my_member = (
        SimpleNamespace(permissions=list(permissions)) if permissions is not None else None
    )
    return SimpleNamespace(
        name=name, membership_status=membership_status, my_member=my_member
    )


class FakeGroupsApi:
    """Stands in for GroupsApi, recording what the worker actually called."""

    def __init__(self):
        self.calls = []
        self.groups = [group(permissions=READY_PERMISSIONS)]
        self.get_group_error = None
        self.join_error = None

    def get_group(self, group_id, **kwargs):
        self.calls.append(("get_group", group_id))
        if self.get_group_error:
            raise self.get_group_error
        return self.groups.pop(0) if len(self.groups) > 1 else self.groups[0]

    def join_group(self, group_id, **kwargs):
        self.calls.append(("join_group", group_id))
        if self.join_error:
            raise self.join_error
        return SimpleNamespace()

    def joined(self):
        return [c for c in self.calls if c[0] == "join_group"]


@pytest.fixture(autouse=True)
def api(monkeypatch):
    """A live session and a fake GroupsApi, with no real API anywhere."""
    fake = FakeGroupsApi()
    monkeypatch.setattr(inviter, "GroupsApi", lambda client=None: fake)
    monkeypatch.setattr(inviter.vrchat_session, "get", lambda: (object(), None))
    monkeypatch.setattr(inviter, "ApiException", FakeApiException)
    monkeypatch.setattr(inviter, "request_timeout", lambda: (1, 1))
    monkeypatch.setattr(inviter.time, "sleep", lambda *_: None)
    return fake


class TestAlreadySetUp:
    def test_ready_when_member_with_invite_permission(self, api):
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_READY
        assert result["ok"] is True
        assert result["can_invite"] is True
        assert result["can_see_members"] is True
        assert result["group_name"] == "Club LA"

    def test_does_not_rejoin_a_group_it_is_already_in(self, api):
        inviter.verify_group_setup(JOB)
        assert api.joined() == [], "already a member; join_group must not be called"

    def test_wildcard_permission_counts(self, api):
        api.groups = [group(permissions=["*"])]
        result = inviter.verify_group_setup(JOB)
        assert result["can_invite"] is True
        assert result["can_see_members"] is True

    def test_invite_works_without_member_visibility(self, api):
        """members-viewall is optional; losing it must not block the feature."""
        api.groups = [group(permissions=["group-invites-manage"])]
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_READY
        assert result["can_invite"] is True
        assert result["can_see_members"] is False


class TestPermissionGap:
    def test_admin_role_without_invite_permission_is_reported(self, api):
        """The exact state a real admin lands in: joined, cannot invite."""
        api.groups = [group(permissions=ADMIN_WITHOUT_INVITE)]
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_NO_INVITE_PERMISSION
        assert result["ok"] is False
        assert result["can_invite"] is False
        # It still saw the members permission, which the message must not deny.
        assert result["can_see_members"] is True
        assert "Manage Group Invites" in result["error_message"]
        assert "admin" in result["error_message"].lower(), (
            "admins hit this exact case; the message has to say admin is not enough"
        )

    def test_member_with_no_permissions_at_all(self, api):
        api.groups = [group(permissions=[])]
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_NO_INVITE_PERMISSION
        assert result["can_invite"] is False
        assert result["can_see_members"] is False


class TestJoining:
    def test_open_group_is_joined_then_rechecked(self, api):
        api.groups = [group(membership_status=None), group(permissions=READY_PERMISSIONS)]
        result = inviter.verify_group_setup(JOB)
        assert api.joined() == [("join_group", GROUP_ID)]
        assert result["state"] == inviter.STATE_READY

    def test_request_to_join_waits_for_a_moderator(self, api):
        api.groups = [group(membership_status=None), group(membership_status="requested")]
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_JOIN_REQUESTED
        assert result["ok"] is False
        assert "approve" in result["error_message"]

    def test_closed_group_without_an_invite(self, api):
        api.groups = [group(membership_status=None)]
        api.join_error = FakeApiException(status=403, body="not allowed")
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_NOT_INVITED
        assert "not been invited" in result["error_message"]

    def test_only_the_group_named_by_the_job_is_ever_joined(self, api):
        """The rule this worker exists to enforce: no group it was not told about."""
        api.groups = [group(membership_status=None), group(permissions=READY_PERMISSIONS)]
        inviter.verify_group_setup(dict(JOB, groupID="grp_only-this-one"))
        assert {c[1] for c in api.calls} == {"grp_only-this-one"}


class TestFailures:
    def test_unknown_group_id(self, api):
        api.get_group_error = FakeApiException(status=404, body="Can't find group")
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_GROUP_NOT_FOUND
        assert result["ok"] is False

    def test_invisible_group_reads_as_not_found(self, api):
        api.get_group_error = FakeApiException(status=403, body="nope")
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_GROUP_NOT_FOUND

    def test_no_vrchat_session(self, monkeypatch):
        monkeypatch.setattr(
            inviter.vrchat_session, "get",
            lambda: (None, {"error_message": "VRChat session not active"}),
        )
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_VRCHAT_UNAVAILABLE
        assert result["ok"] is False

    def test_transient_failure_is_retried(self, api):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise FakeApiException(status=503, body="upstream")
            return group(permissions=READY_PERMISSIONS)

        api.get_group = flaky
        result = inviter.verify_group_setup(JOB)
        assert calls["n"] == 3
        assert result["state"] == inviter.STATE_READY

    def test_permanent_failure_is_not_retried(self, api):
        calls = {"n": 0}

        def always_404(*a, **k):
            calls["n"] += 1
            raise FakeApiException(status=404, body="Can't find group")

        api.get_group = always_404
        result = inviter.verify_group_setup(JOB)
        assert calls["n"] == 1, "a 404 will never become a 200; retrying wastes the rate budget"
        assert result["state"] == inviter.STATE_GROUP_NOT_FOUND


class TestJobDispatch:
    class Chan:
        def __init__(self):
            self.acked = self.nacked = None

        def basic_ack(self, delivery_tag=None):
            self.acked = delivery_tag

        def basic_nack(self, delivery_tag=None, requeue=None):
            self.nacked = (delivery_tag, requeue)

    def test_malformed_json_is_dropped_not_requeued(self):
        ch = self.Chan()
        inviter.process_job(ch, SimpleNamespace(delivery_tag=1, redelivered=False), None, b"{not json")
        assert ch.nacked == (1, False)

    def test_unknown_job_type_is_dropped(self):
        ch = self.Chan()
        inviter.process_job(
            ch, SimpleNamespace(delivery_tag=2, redelivered=False), None, b'{"type": "wat"}'
        )
        assert ch.nacked == (2, False), "an unknown type would wedge the queue if requeued"

    def test_unexpected_error_requeues_once_then_drops(self, monkeypatch):
        def boom(job):
            raise RuntimeError("bug")

        monkeypatch.setitem(inviter.HANDLERS, inviter.JOB_VERIFY_SETUP, boom)
        body = b'{"type": "verify_group_setup", "groupID": "grp_x"}'

        first = self.Chan()
        inviter.process_job(first, SimpleNamespace(delivery_tag=3, redelivered=False), None, body)
        assert first.nacked == (3, True)

        again = self.Chan()
        inviter.process_job(again, SimpleNamespace(delivery_tag=4, redelivered=True), None, body)
        assert again.nacked == (4, False)


class TestServiceGuards:
    def test_not_configured_without_credentials(self, monkeypatch):
        monkeypatch.setattr(
            inviter, "INVITE_ACCOUNT",
            vrcs.VRChatAccount(username=None, password=None, user_agent="x"),
        )
        assert inviter.is_configured() is False

    def test_uses_its_own_queues(self):
        """Sharing the verification queue would have the age checker eat these."""
        import vrc_online_checker as checker

        assert inviter.REQUEST_QUEUE_NAME != checker.RABBITMQ_QUEUE_NAME
        assert inviter.RESULT_QUEUE_NAME != checker.RESULT_QUEUE_NAME

    def test_identifies_itself_to_vrchat_with_a_real_contact(self):
        ua = inviter.INVITE_USER_AGENT
        assert "yourdomain" not in ua, "placeholder contact; VRChat treats this as grounds for action"
        assert "@" in ua and ua.startswith("VRCVerifyGroupInvite/")

    def test_there_is_no_invite_polling_loop(self):
        """The worker must never accept invites it was not told about."""
        import inspect

        src = inspect.getsource(inviter)
        for forbidden in ("get_group_invites", "decline_group_invite"):
            assert forbidden not in src.replace("# ", ""), (
                f"{forbidden} suggests reacting to unsolicited invites; joining must "
                "only ever follow a job naming a specific group"
            )


class TestReviewRegressions:
    """One test per hole found reviewing the first cut of this worker.

    Each of these passed silently before: no exception, no log that named the
    cause, and in three of the five the job simply vanished.
    """

    def test_missing_group_id_is_answered_not_dropped(self, api):
        """None reaches the client as ApiValueError, which is NOT an ApiException.

        It subclasses ValueError, so it sailed past every handler, landed in
        the generic catch, and the job was dropped with no result -- leaving
        whoever asked waiting on an answer that never came.
        """
        for bad in [None, "", "not-a-group", 12345]:
            result = inviter.verify_group_setup(dict(JOB, groupID=bad))
            assert result["state"] == inviter.STATE_BAD_JOB
            assert result["ok"] is False
        assert api.calls == [], "a malformed job must never reach the VRChat API"

    def test_banned_is_terminal_not_an_invite_problem(self, api):
        """Being banned was reported as "we have not been invited yet", which
        sends the admin round a loop of re-inviting that cannot ever work."""
        for status in ["banned", "userblocked"]:
            api.calls.clear()
            api.groups = [group(membership_status=status)]
            result = inviter.verify_group_setup(JOB)
            assert result["state"] == inviter.STATE_BANNED
            assert "banned or blocked" in result["error_message"]
            assert api.joined() == [], "join_group cannot lift a ban; do not spend the call"

    def test_banned_after_the_join_attempt_is_also_terminal(self, api):
        api.groups = [group(membership_status=None), group(membership_status="banned")]
        result = inviter.verify_group_setup(JOB)
        assert result["state"] == inviter.STATE_BANNED

    def test_401_from_any_call_invalidates_the_session(self, api, monkeypatch):
        """Only the first call used to do this, so a 401 from a later one left
        a known-dead client installed until an unrelated job hit call one."""
        invalidated = []
        monkeypatch.setattr(
            inviter.vrchat_session, "invalidate", lambda meta=None: invalidated.append(meta)
        )
        api.groups = [group(membership_status=None)]
        api.join_error = FakeApiException(status=401, body="Missing Credentials")

        inviter.verify_group_setup(JOB)
        assert invalidated, "a 401 from join_group left the dead session in place"

    def test_failed_result_publish_requeues_instead_of_acking(self, monkeypatch):
        """publish_result used to swallow AMQPError and return normally, so the
        job was ACKed with no result ever delivered -- and the join had already
        happened, so the side effect was applied and the outcome lost."""
        from pika.exceptions import AMQPError

        monkeypatch.setattr(inviter, "_rabbitmq_connect_with_retry",
                            lambda max_tries=0: (_ for _ in ()).throw(AMQPError("broker down")))
        with pytest.raises(AMQPError):
            inviter.publish_result({"state": "ready"})

    def test_publish_failure_makes_process_job_requeue(self, monkeypatch):
        from pika.exceptions import AMQPError

        monkeypatch.setitem(inviter.HANDLERS, inviter.JOB_VERIFY_SETUP, lambda job: {"ok": True})
        monkeypatch.setattr(
            inviter, "publish_result",
            lambda result: (_ for _ in ()).throw(AMQPError("broker down")),
        )
        ch = TestJobDispatch.Chan()
        inviter.process_job(
            ch, SimpleNamespace(delivery_tag=9, redelivered=False), None,
            b'{"type": "verify_group_setup", "groupID": "grp_x"}',
        )
        assert ch.nacked == (9, True), "the outcome must not be lost to a broker blip"
        assert ch.acked is None

    def test_a_job_dropped_for_good_still_reports(self, monkeypatch):
        """A job dropped after its retry left the dashboard waiting forever."""
        published = []
        monkeypatch.setitem(
            inviter.HANDLERS, inviter.JOB_VERIFY_SETUP,
            lambda job: (_ for _ in ()).throw(RuntimeError("bug")),
        )
        monkeypatch.setattr(inviter, "publish_result", published.append)

        ch = TestJobDispatch.Chan()
        inviter.process_job(
            ch, SimpleNamespace(delivery_tag=10, redelivered=True), None,
            b'{"type": "verify_group_setup", "jobID": "j9", "groupID": "grp_x"}',
        )
        assert ch.nacked == (10, False)
        assert len(published) == 1, "dropped silently; nobody is ever told"
        assert published[0]["ok"] is False
        assert published[0]["jobID"] == "j9", "the reply must be routable back to the job"

    def test_heartbeat_outlasts_the_worst_case_job(self):
        """VRChat work runs inline, and pika cannot heartbeat while blocked in
        the callback. Three retried call chains can outlast a 60s heartbeat,
        after which the broker drops the connection, the ack fails, and the
        job is redelivered and re-run."""
        params = inviter._rabbitmq_parameters()
        assert params.heartbeat >= 300, (
            "heartbeat too tight for a job that can spend minutes inside VRChat calls"
        )
