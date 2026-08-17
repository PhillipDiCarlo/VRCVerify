"""Consistency checks for src/locales.py.

These catch the failure modes that break localization at runtime:
missing/extra keys per locale, translations whose {placeholders} don't match
the English template (which makes str.format raise KeyError when the bot
builds the message), unbalanced Discord markup, and a "translation" that is
still English.

**Why the structural checks earn their place.** Nobody on this project reads
all twelve of these languages. Whether a Bengali sentence is *good* cannot be
asserted here and is not pretended to be. What can be asserted is everything
that goes wrong without anyone being able to read the language at all: an odd
`**` renders literal asterisks in the middle of a sentence, a stray backtick
swallows the rest of a paragraph into a code span, and a locale left as an
English copy looks translated in every test that only counts keys. Those are
the failures a reviewer who does not speak the language would never catch by
eye, which is exactly why they are worth a test rather than a proofread.
"""

import string

import pytest

from locales import localizations, LANGUAGE_CODES

# Known untranslated, and named here so it stays visible instead of quietly
# passing a test that says every locale is translated. This one is English in
# all eleven other locales -- a real gap, predating the check that found it,
# and deliberately not fixed in the change that added this list, because
# widening a payment-copy change into a general translation sweep is how a
# reviewable diff stops being one.
UNTRANSLATED = {"dm_unverified_failed_bot_position"}


def placeholder_names(template: str) -> set[str]:
    return {
        field_name.split(".")[0].split("[")[0]
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


def test_english_locale_exists():
    assert "en-US" in localizations
    assert localizations["en-US"], "en-US must define the reference strings"


def test_language_codes_match_localization_tables():
    assert set(LANGUAGE_CODES) == set(localizations.keys())


@pytest.mark.parametrize("locale", LANGUAGE_CODES)
def test_locale_has_no_unknown_keys(locale):
    """Every key in a translation must exist in en-US (typos surface here)."""
    unknown = set(localizations[locale]) - set(localizations["en-US"])
    assert not unknown, f"{locale} defines keys missing from en-US: {sorted(unknown)}"


@pytest.mark.parametrize("locale", LANGUAGE_CODES)
def test_locale_is_complete(locale):
    """Every en-US key should be translated (fallback hides these silently)."""
    missing = set(localizations["en-US"]) - set(localizations[locale])
    assert not missing, f"{locale} is missing keys: {sorted(missing)}"


@pytest.mark.parametrize("locale", LANGUAGE_CODES)
def test_placeholders_match_english(locale):
    """A translation must not reference placeholders the caller never passes."""
    reference = localizations["en-US"]
    problems = []
    for key, template in localizations[locale].items():
        if key not in reference:
            continue
        extra = placeholder_names(template) - placeholder_names(reference[key])
        if extra:
            problems.append(f"{key}: unexpected placeholders {sorted(extra)}")
    assert not problems, f"{locale}:\n" + "\n".join(problems)


@pytest.mark.parametrize("locale", LANGUAGE_CODES)
def test_bold_markers_are_balanced(locale):
    """An odd number of ** renders literal asterisks mid-sentence.

    Discord does not "fail" on unbalanced markup, it just shows it, so this
    surfaces nowhere except in front of the reader -- and only in front of the
    readers of one language, which is the set of people least likely to be
    able to report it in a way anyone here can act on.
    """
    broken = [key for key, text in localizations[locale].items()
              if text.count("**") % 2]
    assert not broken, f"{locale} has unbalanced ** in: {sorted(broken)}"


@pytest.mark.parametrize("locale", LANGUAGE_CODES)
def test_inline_code_markers_are_balanced(locale):
    """A stray backtick swallows the rest of the message into a code span.

    Worse than the bold case: the slash commands in this copy are wrapped in
    backticks, so the damage lands on exactly the lines telling somebody which
    command to run.
    """
    broken = [key for key, text in localizations[locale].items()
              if text.count("`") % 2]
    assert not broken, f"{locale} has an unbalanced backtick in: {sorted(broken)}"


@pytest.mark.parametrize(
    "locale", [code for code in LANGUAGE_CODES if code != "en-US"]
)
def test_no_string_is_left_as_english(locale):
    """A key that exists but was never translated passes every other check.

    `test_locale_is_complete` only asks whether the key is present, so an
    English string copied into all twelve tables looks fully translated to
    this suite and to anyone scanning the file who does not read the language.
    """
    english = localizations["en-US"]
    copied = [
        key for key, text in localizations[locale].items()
        if key not in UNTRANSLATED and english.get(key) == text
    ]
    assert not copied, (
        f"{locale} is still English in: {sorted(copied)}. If that is "
        f"deliberate, add the key to UNTRANSLATED with a reason."
    )
