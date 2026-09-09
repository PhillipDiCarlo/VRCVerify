"""Where a Postgres test run is allowed to point.

The opt-in Postgres mode (#275) exists so a type divergence can be reproduced
against a real schema. It also hands the suite's teardown -- which deletes rows,
correctly, because that is what teardown does -- a live Postgres connection.
Aimed at the wrong host that is the 2026-09-08 outage again, from inside the
test suite this time rather than from an ad-hoc script.

conftest.local_database_only is the safety. These are its tests.
"""

import pytest

from conftest import local_database_only


class TestWhatIsAllowed:
    def test_sqlite_needs_no_argument(self):
        assert local_database_only("sqlite:///:memory:") is None

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@localhost:5432/vrcverify_test",
            "postgresql://u:p@127.0.0.1:5432/vrcverify_test",
            "postgresql://u:p@[::1]:5432/vrcverify_test",
            "postgresql://u:p@localhost/vrcverify_test",
        ],
    )
    def test_loopback_in_its_various_spellings(self, url):
        assert local_database_only(url) is None


class TestWhatIsRefused:
    def test_the_production_host(self):
        """The address in .env on every developer checkout."""
        refusal = local_database_only(
            "postgresql://vrcverify_app:secret@10.53.1.87:5432/vrcverify_database"
        )
        assert refusal is not None
        assert "10.53.1.87" in refusal

    def test_the_refusal_says_what_to_do_instead(self):
        """Whoever reads this is trying to reproduce something. A refusal that
        only says no sends them back to the host it just refused."""
        refusal = local_database_only("postgresql://u:p@db.example.com/x")
        assert "restore a copy locally" in refusal
        assert "never at production" in refusal

    def test_the_credential_is_not_echoed_back(self):
        """The message names the host so it is actionable. It must not name the
        password: this lands in terminal scrollback and in CI logs."""
        refusal = local_database_only(
            "postgresql://vrcverify_app:hunter2@10.53.1.87:5432/vrcverify_database"
        )
        assert "hunter2" not in refusal

    def test_a_password_containing_an_at_sign_does_not_disguise_the_host(self):
        """Splitting the authority from the left would read the password's own
        @ as the separator and take a fragment of the credential for the
        hostname -- which is not loopback, so this one fails safe. The test is
        here because the same slip in the other direction would not."""
        refusal = local_database_only("postgresql://u:pa@ss@evil.example.com/x")
        assert refusal is not None
        assert "evil.example.com" in refusal

    def test_a_host_merely_containing_localhost_is_not_loopback(self):
        """`localhost.evil.example.com` resolves wherever its owner says."""
        refusal = local_database_only("postgresql://u:p@localhost.example.com/x")
        assert refusal is not None
