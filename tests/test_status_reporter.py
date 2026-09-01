"""The homelab's half of the status page (issue #170 phase 2).

The status page cannot probe anything on the homelab -- that is the whole
reason this code exists -- so everything it knows about the bot, the checker
and the inviter arrives through the two pieces tested here: a heartbeat file
each service writes, and one reporter that reads them and posts.

WHAT THESE TESTS ARE REALLY ABOUT: silence. A homelab that is off cannot say
so, so every path where information is MISSING has to resolve to "down" rather
than to nothing. Each of those paths is a separate test below, because each one
is a different way for the page to quietly go green while the bot is dead:
a file that stopped being written, a file that cannot be parsed, a service that
never wrote one at all, and a probe that raised on its way to answering.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

import heartbeat  # noqa: E402
import status_reporter as reporter  # noqa: E402


class TestHeartbeatFiles:
    def test_a_heartbeat_is_one_atomic_write(self, tmp_path):
        path = tmp_path / "discord-bot.json"
        heartbeat.write_heartbeat(path, "discord-bot", {"discord-bot": (True, "gateway ready")})
        payload = json.loads(path.read_text())
        assert payload["service"] == "discord-bot"
        assert payload["parts"]["discord-bot"] == {"up": True, "detail": "gateway ready"}
        assert abs(payload["at"] - int(time.time())) < 5
        # The temporary file is gone: a reader globbing *.json must not find
        # half a heartbeat sitting beside the real one.
        assert [p.name for p in tmp_path.iterdir()] == ["discord-bot.json"]

    def test_writing_never_raises_into_the_service(self, tmp_path):
        """Bookkeeping must never be the reason a verification fails."""
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("I am a file")
        heartbeat.write_heartbeat(blocked / "checker.json", "checker", {"x": (True, None)})

    def test_nothing_starts_unless_the_deployment_asked(self, monkeypatch):
        monkeypatch.delenv("HEARTBEAT_DIR", raising=False)
        assert heartbeat.start_heartbeat("discord-bot", lambda: {}) is None

    def test_a_probe_that_raises_reports_down(self, tmp_path, monkeypatch):
        """The check breaking is a finding, not a reason to claim health."""
        monkeypatch.setenv("HEARTBEAT_DIR", str(tmp_path))

        def probe():
            raise RuntimeError("database is on fire")

        thread = heartbeat.start_heartbeat("discord-bot", probe, interval=0.05)
        assert thread is not None
        deadline = time.time() + 3
        path = tmp_path / "discord-bot.json"
        while time.time() < deadline and not path.exists():
            time.sleep(0.02)
        payload = json.loads(path.read_text())
        assert payload["parts"]["discord-bot"]["up"] is False
        assert "RuntimeError" in payload["parts"]["discord-bot"]["detail"]


def _write(tmp_path, service, parts, age=0):
    payload = {"service": service, "at": int(time.time()) - age, "pid": 1, "parts": parts}
    (tmp_path / f"{service}.json").write_text(json.dumps(payload))


class TestReadingHeartbeats:
    def test_fresh_files_are_reported_as_written(self, tmp_path):
        _write(tmp_path, "discord-bot", {"discord-bot": {"up": True, "detail": "ready"}})
        _write(tmp_path, "vrc-online-checker", {"vrc-online-checker": {"up": True, "detail": None}})
        _write(tmp_path, "vrc-group-inviter", {"vrc-group-inviter": {"up": True, "detail": None}})
        parts = reporter._read_heartbeats(tmp_path, int(time.time()))
        assert parts["discord-bot"] == {"up": True, "detail": "ready"}

    def test_a_file_that_stopped_being_written_is_an_outage(self, tmp_path):
        """The container was stopped. Nothing else can tell us that."""
        _write(tmp_path, "vrc-online-checker",
               {"vrc-online-checker": {"up": True, "detail": "consuming"},
                "queue": {"up": True, "detail": None}}, age=reporter.STALE_AFTER + 30)
        _write(tmp_path, "discord-bot", {"discord-bot": {"up": True, "detail": None}})
        _write(tmp_path, "vrc-group-inviter", {"vrc-group-inviter": {"up": True, "detail": None}})
        parts = reporter._read_heartbeats(tmp_path, int(time.time()))
        assert parts["vrc-online-checker"]["up"] is False
        # Every part that file spoke for goes with it, including the queue --
        # we have not learned the queue is down, we have learned we no longer
        # have anybody watching it from that side.
        assert parts["queue"]["up"] is False
        assert "no heartbeat" in parts["vrc-online-checker"]["detail"]
        # And the services still writing are untouched.
        assert parts["discord-bot"]["up"] is True

    def test_an_unreadable_heartbeat_is_not_a_healthy_service(self, tmp_path):
        (tmp_path / "discord-bot.json").write_text("{ this is not json")
        _write(tmp_path, "vrc-online-checker", {"vrc-online-checker": {"up": True, "detail": None}})
        _write(tmp_path, "vrc-group-inviter", {"vrc-group-inviter": {"up": True, "detail": None}})
        parts = reporter._read_heartbeats(tmp_path, int(time.time()))
        assert parts["discord-bot"]["up"] is False
        assert "unreadable" in parts["discord-bot"]["detail"]

    def test_a_service_that_never_wrote_anything_is_reported_down(self, tmp_path):
        """A stopped container must not look like a feature nobody deployed."""
        parts = reporter._read_heartbeats(tmp_path, int(time.time()))
        for service in reporter.EXPECTED:
            assert parts[service]["up"] is False
            assert parts[service]["detail"] == "no heartbeat file"

    def test_a_missing_directory_is_reported_rather_than_crashing(self, tmp_path):
        parts = reporter._read_heartbeats(tmp_path / "nope", int(time.time()))
        assert all(part["up"] is False for part in parts.values())

    def test_the_queue_takes_the_worst_of_the_two_answers(self, tmp_path):
        """Both workers watch it, and half-broken is the hardest state to see."""
        _write(tmp_path, "vrc-online-checker",
               {"vrc-online-checker": {"up": True, "detail": None},
                "queue": {"up": True, "detail": None}})
        _write(tmp_path, "vrc-group-inviter",
               {"vrc-group-inviter": {"up": True, "detail": None},
                "queue": {"up": False, "detail": "consumer connection closed"}})
        _write(tmp_path, "discord-bot", {"discord-bot": {"up": True, "detail": None}})
        parts = reporter._read_heartbeats(tmp_path, int(time.time()))
        assert parts["queue"]["up"] is False
        assert parts["queue"]["detail"] == "consumer connection closed"


class TestSigning:
    # THE SAME VECTOR IS PINNED IN status/test/report.test.js, where the
    # Worker's verifier is asked to accept it. The two halves of this protocol
    # are written in different languages, deployed by different people, on
    # different schedules; the only thing that keeps them agreeing is that both
    # sides pin the same bytes. A change that breaks one fails here AND there.
    SECRET = "not-a-real-secret"
    TIMESTAMP = 1788233609
    BODY = b'{"parts":{"discord-bot":{"detail":null,"up":true}}}'
    EXPECTED = "425d79342d2a683b9268d6fe76947fbb3c66389e376494f0b3e833c2833e1f37"

    def test_the_signature_is_over_the_timestamp_and_the_body(self):
        assert reporter._sign(self.SECRET, self.TIMESTAMP, self.BODY) == self.EXPECTED

    def test_moving_the_timestamp_changes_the_signature(self):
        """Otherwise a captured report is replayable forever.

        For this endpoint that means holding the page at "everything is fine"
        while the homelab is dark, which is the single lie the design exists to
        prevent.
        """
        assert reporter._sign(self.SECRET, self.TIMESTAMP + 1, self.BODY) != self.EXPECTED

    def test_the_body_the_reporter_sends_is_the_body_it_signs(self, monkeypatch):
        """A re-serialisation between signing and sending would fail every time."""
        sent = {}

        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            sent["body"] = request.data
            sent["signature"] = request.headers["X-vrcverify-signature"]
            return FakeResponse()

        monkeypatch.setattr(reporter.urllib.request, "urlopen", fake_urlopen)
        assert reporter.post_report("https://example.invalid/report", self.SECRET,
                                    {"discord-bot": {"up": True, "detail": None}})
        timestamp = int(sent["signature"].split(",")[0].split("=")[1])
        signature = sent["signature"].split("v1=")[1]
        assert reporter._sign(self.SECRET, timestamp, sent["body"]) == signature


class TestItHoldsNoCredentials:
    def test_the_reporter_reads_no_database_or_broker_configuration(self):
        """Its only secret is one that can lie to a status page.

        Every fact it reports was established by a process that already had the
        right to establish it. If this ever grows a DATABASE_URL, the argument
        for a container that talks to the public internet on a timer has been
        quietly discarded.
        """
        source = (__import__("pathlib").Path(reporter.__file__)).read_text()
        for name in ("DATABASE_URL", "RABBITMQ_", "DISCORD_BOT_TOKEN", "docker.sock"):
            assert name not in source, f"the reporter has grown a {name}"

    def test_it_exits_cleanly_when_the_feature_is_not_switched_on(self, monkeypatch):
        """Matching the invite worker: unprovisioned is not a crash."""
        monkeypatch.delenv("STATUS_REPORT_URL", raising=False)
        monkeypatch.delenv("STATUS_REPORT_SECRET", raising=False)
        assert reporter.main() == 0
