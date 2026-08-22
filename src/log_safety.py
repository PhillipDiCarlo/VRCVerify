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
#
# The three at the end are the ones that get missed. NEL, LINE SEPARATOR and
# PARAGRAPH SEPARATOR are not C0 and look like ordinary characters, but
# str.splitlines() breaks on all three, as does most JavaScript log tooling --
# so a display name carrying U+2028 splits the line in the reader even though
# nothing in the byte stream looks like a newline.
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(match: "re.Match") -> str:
    char = match.group()
    named = _NAMED.get(char)
    if named is not None:
        return named
    # \xNN is two hex digits by definition, so U+2028 has to be \u2028 -- as
    # \x2028 it reads as an escaped \x20 followed by a literal "28", which is
    # both wrong and quietly misleading about what was in the log.
    point = ord(char)
    return "\\x%02x" % point if point < 0x100 else "\\u%04x" % point


def scrub(text: str) -> str:
    """One log line's worth of text, with control characters made visible."""
    return _CONTROL.sub(_escape, text)


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

    Tracebacks are deliberately untouched, and this is a KNOWN GAP rather
    than a clean win. They live in ``exc_info`` and are appended by the
    formatter after this runs, so ``logger.exception`` keeps its multi-line
    stack trace -- but the exception's own ``str()`` is part of that block,
    and it is not always ours. ``vrchatapi.ApiException`` embeds the raw
    upstream response body, so a newline in a VRChat error reaches the log
    through ``exc_info=True`` unescaped.

    Not closed because it cannot be closed cheaply and honestly: once the
    traceback is a block of text there is nothing left to distinguish a
    newline between two frames from a newline inside the message, so escaping
    the block destroys every stack trace and escaping nothing leaves this. The
    same value logged through ``%s`` IS escaped, which covers the deliberate
    "here is what went wrong" lines; what stays exposed is the incidental
    copy inside a stack trace. Worth revisiting with a formatter that renders
    frames and message separately.
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


def install_log_scrubbing(*loggers: logging.Logger) -> int:
    """Filter every handler on each logger (root by default). Returns how many.

    Attached to HANDLERS, not to the loggers. A filter on a logger only sees
    records logged directly to it -- records propagating up from child loggers
    skip it entirely, which would be every line in these services, since they
    all log through `getLogger(__name__)` and let it propagate to root.

    Which is also why this takes more than one: root covers everything that
    propagates, and nothing that does not. A logger with `propagate = False`
    and its own handlers -- gunicorn.error and gunicorn.access are both -- is
    invisible from root and has to be named.

    Call it AFTER whatever configures logging, and again after anything that
    might add a handler of its own. Idempotent, so the second call is free.
    """
    targets = loggers or (logging.getLogger(),)
    installed = 0
    for target in targets:
        for handler in target.handlers:
            if any(isinstance(f, ControlCharacterFilter) for f in handler.filters):
                continue
            handler.addFilter(ControlCharacterFilter())
            installed += 1
    return installed
