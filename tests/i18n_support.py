"""Reading a bot string in one language, for tests (#231).

`bot.translate` and `bot.get_message` always `.format(**kwargs)`, deliberately:
a string whose placeholder the caller forgot should raise where it is called,
not render `{role}` to a member. That is the right behaviour for the bot and
the wrong one for a test that wants to inspect the template itself -- checking
that a translation kept its `{server}`, or that it opens its own paragraph, or
that Japanese is not still English.

Before #231 those tests read `localizations[code][key]`, which was the raw
template. This is that, against the compiled catalogues: the same string the
dict used to hand back, translated and unformatted.
"""

import bot


def template(msgid: str, locale: str) -> str:
    """The translated string for `locale`, with its placeholders left alone."""
    return bot.CATALOGUES.translator(locale)(msgid)


def is_translated(msgid: str, locale: str) -> bool:
    """Whether `locale` has its own words for this string.

    gettext answers an untranslated msgid with the msgid, so "the key exists"
    -- the question the dict-era tests asked -- is no longer answerable and no
    longer interesting. This is the question that replaced it: an English
    sentence sitting in the middle of an otherwise Japanese DM is the failure
    that survives every check that only counts keys.
    """
    return locale == "en-US" or template(msgid, locale) != msgid
