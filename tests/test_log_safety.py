"""Control characters must not be able to forge a log line (CWE-117).

Found by CodeQL's security-extended pack: fourteen py/log-injection alerts
across the dashboard and bot-api, all of the same shape -- an id or claim
that came from outside the process, interpolated straight into a log line.

The one that matters most is the least eye-catching. bot_api._deny writes
the DENY/ALLOW trail its own docstring calls "the forensic record" under an
assume-breach model, so injecting a plausible ALLOW line into it is writing
the evidence.
"""

import io
import logging

import pytest

from log_safety import ControlCharacterFilter, install_log_scrubbing, scrub


class TestScrub:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("plain", "plain"),
            ("a\nb", "a\\nb"),
            ("a\rb", "a\\rb"),
            ("a\tb", "a\\tb"),
            ("a\r\nb", "a\\r\\nb"),
            ("\x00", "\\x00"),
            # ESC, the start of every ANSI terminal sequence.
            ("\x1b[31mred", "\\x1b[31mred"),
            ("\x7f", "\\x7f"),
        ],
    )
    def test_control_characters_are_escaped(self, raw, expected):
        assert scrub(raw) == expected

    def test_ordinary_text_is_untouched(self):
        """Including the characters a log line is actually made of."""
        line = "bot-api ALLOW actor=123456789 guild=987 op=settings.write - 200 %s"
        assert scrub(line) == line

    def test_non_ascii_survives(self):
        """Display names are not ASCII. Escaping them would mangle every log
        line naming a member with a non-Latin name."""
        assert scrub("ゆい / Yui — 🎧") == "ゆい / Yui — 🎧"

    def test_the_result_is_always_one_line(self):
        assert "\n" not in scrub("a\nb\nc\r\nd")


class TestTheFilter:
    def record(self, msg, *args):
        return logging.LogRecord(
            "t", logging.WARNING, __file__, 1, msg, args or None, None
        )

    def test_an_injected_line_is_neutralised(self):
        forged = "bob\nbot-api ALLOW actor=attacker guild=1 op=settings.write"
        record = self.record("bot-api DENY actor=%s", forged)
        ControlCharacterFilter().filter(record)
        assert "\n" not in record.getMessage()
        assert "ALLOW" in record.getMessage()  # visible, not silently dropped

    def test_a_clean_record_keeps_its_args(self):
        """Structured handlers downstream still get the format string and its
        arguments separately; only a record carrying a control character is
        collapsed to one rendered string."""
        record = self.record("guild=%s op=%s", "987", "settings.write")
        ControlCharacterFilter().filter(record)
        assert record.args == ("987", "settings.write")
        assert record.msg == "guild=%s op=%s"

    def test_a_dirty_record_is_collapsed_exactly_once(self):
        """If args survived alongside a pre-rendered message, the formatter
        would interpolate them a second time -- and a scrubbed message
        containing a literal %s would swallow them."""
        record = self.record("actor=%s", "a\nb %s")
        ControlCharacterFilter().filter(record)
        assert record.args == ()
        assert record.getMessage() == "actor=a\\nb %s"

    def test_a_non_string_argument_is_still_caught(self):
        """The dangerous value is not always a str when it reaches the logger.
        An exception object renders through __str__, which a filter inspecting
        args for str instances would never look at."""

        class Boom(Exception):
            def __str__(self):
                return "upstream said\nbot-api ALLOW actor=attacker"

        record = self.record("stripe error: %s", Boom())
        ControlCharacterFilter().filter(record)
        assert "\n" not in record.getMessage()

    def test_a_mismatched_format_string_is_left_alone(self):
        """Logging reports this far better than swallowing it here would."""
        record = self.record("%s and %s", "only-one")
        assert ControlCharacterFilter().filter(record) is True
        assert record.msg == "%s and %s"

    def test_the_record_is_never_dropped(self):
        record = self.record("anything\n")
        assert ControlCharacterFilter().filter(record) is True


class TestInstall:
    @pytest.fixture
    def wired(self):
        logger = logging.getLogger("log_safety_test")
        logger.handlers.clear()
        logger.propagate = False
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        yield logger, stream
        logger.handlers.clear()

    def test_end_to_end_through_a_real_handler(self, wired):
        logger, stream = wired
        install_log_scrubbing(logger)
        logger.warning(
            "bot-api DENY actor=%s", "bob\nbot-api ALLOW actor=attacker"
        )
        assert stream.getvalue().count("\n") == 1  # the line terminator only

    def test_without_it_the_forgery_lands(self, wired):
        """The bug this closes, demonstrated rather than asserted about."""
        logger, stream = wired
        logger.warning(
            "bot-api DENY actor=%s", "bob\nbot-api ALLOW actor=attacker"
        )
        assert stream.getvalue().count("\n") == 2

    def test_it_is_idempotent(self, wired):
        """Tests and reloads can configure logging more than once; filters
        must not stack."""
        logger, _ = wired
        assert install_log_scrubbing(logger) == 1
        assert install_log_scrubbing(logger) == 0
        assert len(logger.handlers[0].filters) == 1

    def test_it_filters_handlers_not_the_logger(self, wired):
        """A filter on a logger never sees records propagating up from child
        loggers -- which is every line these services write, since they all
        log through getLogger(__name__) and let it propagate."""
        logger, stream = wired
        install_log_scrubbing(logger)
        logging.getLogger("log_safety_test.child").warning("a\nb")
        assert stream.getvalue() == "a\\nb\n"

    def test_a_traceback_keeps_its_shape(self, wired):
        """Tracebacks are multi-line on purpose and are appended after this
        filter runs. Escaping them would make every logger.exception call
        unreadable to fix a problem they do not have."""
        logger, stream = wired
        install_log_scrubbing(logger)
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("it broke")
        out = stream.getvalue()
        assert "Traceback (most recent call last):" in out
        assert out.count("\n") > 2
