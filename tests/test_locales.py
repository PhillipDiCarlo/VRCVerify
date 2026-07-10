"""Consistency checks for src/locales.py.

These catch the two failure modes that break localization at runtime:
missing/extra keys per locale, and translations whose {placeholders}
don't match the English template (which makes str.format raise KeyError
when the bot builds the message).
"""

import string

import pytest

from locales import localizations, LANGUAGE_CODES


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
