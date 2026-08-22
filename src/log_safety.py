"""Keep attacker-controlled text from forging log lines (CWE-117).

Every service here logs values that came from outside it: Discord ids and
OAuth claims, Stripe subscription and event ids, guild ids lifted from
Checkout metadata, VRChat display names. A newline inside any of those ends
the current log line and starts one the attacker wrote.

That matters most in exactly the place it looks least interesting. The
bot-api DENY/ALLOW trail is, by its own docstring, "the forensic record"
under an assume-breach model -- so being able to inject a plausible ALLOW
line into it is being able to write the evidence.

Escaped rather than stripped. Deleting the newline hides that anyone tried;
turning it into a visible ``\\n`` keeps the line readable, keeps it on one
line, and leaves the attempt in the record where someone can find it.

Stdlib only, so the two worker images -- which carry neither discord.py nor
a database driver -- can import it as cheaply as the bot does.
"""

import logging
import re

# C0 controls and DEL. Tab is in here too: it is harmless to a terminal but
# not to anything splitting these lines on whitespace, and a log format is a
# format whether or not it was ever written down.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def scrub(text: str) -> str:
    """One log line's worth of text, with control characters made visible."""
    return _CONTROL.sub(
        lambda match: _NAMED.get(match.group(), "\\x%02x" % ord(match.group())),
        text,
    )


class ControlCharacterFilter(logging.Filter):
    """Renders the record and escapes anything that could forge a line.

    Works on the RENDERED message rather than on ``record.args``, because the
    dangerous value is not always a string when it is handed to the logger.
    ``logger.warning("stripe said: %s", error)`` puts an exception object in
    args, and whatever ends up in the log is whatever its ``__str__`` returns
    -- which a filter inspecting args for ``str`` instances would never see.

    The record is only rewritten when scrubbing actually changed something.
    That keeps ``msg`` and ``args`` intact for the overwhelming majority of
    records, so a structured handler downstream still sees the format string
    and its arguments separately; only a record that carried a control
    character is collapsed to a single pre-rendered string.

    Tracebacks are deliberately untouched. They live in ``exc_info`` and are
    appended by the formatter after this runs, so ``logger.exception`` keeps
    its multi-line stack trace -- escaping those would make every one of them
    unreadable to fix a problem they do not have.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # Mismatched format string and args. Logging's own error handling
            # reports this far better than swallowing it here would, so leave
            # the record exactly as it is and let it get there.
            return True

        scrubbed = scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            # Cleared, or the formatter would try to interpolate them a second
            # time -- and a scrubbed message containing a literal %s would take
            # them. getMessage() skips interpolation entirely on empty args.
            record.args = ()
        return True


def install_log_scrubbing(logger: logging.Logger = None) -> int:
    """Filter every handler on `logger` (root by default). Returns how many.

    Attached to HANDLERS, not to the logger. A filter on a logger only sees
    records logged directly to it -- records propagating up from child loggers
    skip it entirely, which would be every line in these services, since they
    all log through `getLogger(__name__)` and let it propagate to root.

    Idempotent, so a service that configures logging more than once (tests,
    or a reload) does not stack filters.
    """
    target = logger if logger is not None else logging.getLogger()
    installed = 0
    for handler in target.handlers:
        if any(isinstance(f, ControlCharacterFilter) for f in handler.filters):
            continue
        handler.addFilter(ControlCharacterFilter())
        installed += 1
    return installed
