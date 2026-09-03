"""Structural checks for the bot's gettext catalogues (#231).

These were checks on `src/locales.py` when it was a dict-of-dicts. #231 moved
the twelve languages into `src/translations/bot/`, and the questions worth
asking changed shape with the data:

| Before                              | Now                                     |
|-------------------------------------|-----------------------------------------|
| `test_english_locale_exists`         | gone -- English IS the msgids           |
| `test_language_codes_match_tables`   | every code has a compiled `.mo`         |
| `test_locale_has_no_unknown_keys`    | free -- gettext has no extra keys       |
| `test_locale_is_complete`            | no untranslated entry in any catalogue  |
| `test_placeholders_match_english`    | kept, against the `.po`                 |
| `test_bold/inline_code_balanced`     | kept, against the `.po`                 |
| `test_no_string_is_left_as_english`  | kept, and the allowlist is gone         |

**Why the structural checks still earn their place.** Nobody on this project
reads all twelve of these languages. Whether a Bengali sentence is *good*
cannot be asserted here and is not pretended to be. What can be asserted is
everything that goes wrong without anyone being able to read the language at
all: an odd `**` renders literal asterisks mid-sentence, a stray backtick
swallows the rest of a paragraph into a code span, a renamed placeholder is a
KeyError in front of a member, and a locale left as an English copy looks
translated to every check that only counts entries.

**The UNTRANSLATED allowlist is gone, on purpose.** It held one entry --
`support_invite_line`, English in all eleven tables, with a comment saying #97
owned translating it. #231 translated it. The alternative was carrying the
allowlist into gettext keyed by full English text instead of a short key name,
which is more brittle for the same result, and which would have kept alive a
mechanism whose only remaining job was to excuse a string nobody had gotten to.
If a string ever genuinely must stay English in one language, the honest fix is
a comment in that catalogue and a line here, added deliberately -- not an
allowlist standing open waiting for one.

`tests/test_locales_snapshot.py` is the other half of this file's job and the
more important half: it pins what the strings actually SAY, in all twelve, byte
for byte. This one pins that they are structurally sound.
"""

import os
import string

import pytest

import locales
from i18n_support import template

pytest.importorskip("babel", reason="Babel is a dev-only dependency for the bot")

from babel.messages.pofile import read_po  # noqa: E402

DOMAIN = "bot"
LOCALE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "translations", DOMAIN,
)
TRANSLATED = [code for code in locales.LANGUAGE_CODES if code != "en-US"]


def catalogue_path(code: str) -> str:
    return os.path.join(LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", f"{DOMAIN}.po")


def entries(code: str) -> dict:
    with open(catalogue_path(code), "rb") as handle:
        catalog = read_po(handle)
    return {str(m.id): m for m in catalog if m.id}


def placeholder_names(text: str) -> set:
    return {
        field_name.split(".")[0].split("[")[0]
        for _, field_name, _, _ in string.Formatter().parse(text)
        if field_name
    }


class TestTheCataloguesAreThereAndComplete:
    def test_english_has_no_catalogue_directory(self):
        """Its "translation" is the msgids. A directory here would be a file
        translating English into the same English, every line a chance to
        drift -- the argument scripts/i18n.sh makes and this enforces."""
        for name in ("en-US", "en_US", "en"):
            assert not os.path.isdir(os.path.join(LOCALE_DIR, name))

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_the_compiled_catalogue_is_in_the_tree(self, code):
        """An ignored .mo is a bot that DMs English in every language while
        every .po in the tree says otherwise, with nothing to indicate it --
        gettext falls back silently. .gitignore carves these out for exactly
        this reason; this is what notices if that carve-out ever breaks."""
        path = os.path.join(LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", f"{DOMAIN}.mo")
        assert os.path.exists(path), f"{code}: no compiled catalogue at {path}"

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_every_string_is_translated(self, code):
        assert len(entries(code)) == len(locales.ALL_MESSAGES)
        untranslated = [m.id for m in entries(code).values() if not m.string]
        assert not untranslated, f"{code} has untranslated entries: {untranslated}"

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_nothing_hides_english_behind_a_fuzzy_flag(self, code):
        """The trap `pybabel --statistics` sets, and the one that caught the
        first run of the #231 converter.

        A fuzzy entry is Babel's guess that an old translation still fits a
        changed English string. `--statistics` counts it as translated;
        `compile` without `--use-fuzzy` drops it. So a catalogue reports
        "91 of 91 (100%)" and serves English -- which is what the whole
        catalogue did when Babel's default `fuzzy=True` marked its header.

        Not shipping the guess is the right policy: these strings include role
        assignment failures and the premium pitch, where a wrong guess is a
        support ticket. Being told is the part that needed a test.
        """
        fuzzy = [m.id for m in entries(code).values() if "fuzzy" in m.flags]
        assert not fuzzy, f"{code}: fuzzy entries render English: {fuzzy}"

    def test_every_msgid_in_the_code_is_in_the_pot(self):
        """locales.py and the extracted .pot must agree.

        A constant added without re-running scripts/i18n.sh is missing from
        every catalogue, and its only symptom is a string that stays English
        in all eleven languages -- which looks exactly like a string nobody has
        translated yet, and so gets ignored.
        """
        with open(os.path.join(LOCALE_DIR, f"{DOMAIN}.pot"), "rb") as handle:
            pot = {str(m.id) for m in read_po(handle) if m.id}
        assert pot == set(locales.ALL_MESSAGES), (
            "run ./scripts/i18n.sh: "
            f"only in code {sorted(set(locales.ALL_MESSAGES) - pot)[:3]}, "
            f"only in .pot {sorted(pot - set(locales.ALL_MESSAGES))[:3]}"
        )


class TestTheTranslationsAreStructurallySound:
    @pytest.mark.parametrize("code", TRANSLATED)
    def test_placeholders_match_the_english(self, code):
        """A translation that renames `{server}` is a KeyError in front of a
        member; one that invents a placeholder the caller never passes is the
        same. Dropping one is legal and sometimes right, so only additions
        fail here -- which is the rule the dict-era check used too."""
        problems = []
        for msgid, message in entries(code).items():
            extra = placeholder_names(str(message.string)) - placeholder_names(msgid)
            if extra:
                problems.append(f"{msgid[:45]!r}: unexpected {sorted(extra)}")
        assert not problems, f"{code}:\n" + "\n".join(problems)

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_bold_markers_are_balanced(self, code):
        """An odd number of ** renders literal asterisks mid-sentence.

        Discord does not "fail" on unbalanced markup, it just shows it, so this
        surfaces nowhere except in front of the reader -- and only in front of
        the readers of one language, which is the set of people least likely to
        be able to report it in a way anyone here can act on.
        """
        broken = [m.id for m in entries(code).values() if str(m.string).count("**") % 2]
        assert not broken, f"{code} has unbalanced ** in: {broken}"

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_inline_code_markers_are_balanced(self, code):
        """Worse than the bold case: the slash commands in this copy are
        wrapped in backticks, so a stray one swallows the rest of the message
        into a code span -- landing on exactly the lines telling somebody which
        command to run."""
        broken = [m.id for m in entries(code).values() if str(m.string).count("`") % 2]
        assert not broken, f"{code} has an unbalanced backtick in: {broken}"

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_no_string_is_left_as_english(self, code):
        """`msgstr == msgid` is legal gettext and renders fine, which is what
        makes it dangerous: a table of English copies passes every check that
        counts entries, and looks translated to anyone scanning the file who
        does not read the language.

        Flat, with no allowlist -- see this module's docstring for why that is
        the point rather than an oversight.
        """
        copied = [
            m.id for m in entries(code).values() if str(m.string) == str(m.id)
        ]
        assert not copied, (
            f"{code} is still English in: {copied}. A string that must stay "
            f"English needs a deliberate exception here and a comment in the "
            f"catalogue, not a silent copy."
        )


class TestTheRuntimeAgreesWithTheCatalogues:
    """The checks above read the .po files. The bot reads the .mo. A test that
    only ever reads the source of truth cannot catch the compile step going
    wrong, which is the step that silently did nothing on the first run."""

    @pytest.mark.parametrize("code", TRANSLATED)
    def test_what_the_bot_serves_is_what_the_catalogue_says(self, code):
        catalogue = entries(code)
        for msgid in locales.ALL_MESSAGES:
            assert template(msgid, code) == str(catalogue[msgid].string), (
                f"{code}: the compiled .mo disagrees with the .po for "
                f"{msgid[:45]!r} -- run ./scripts/i18n.sh"
            )

    def test_an_unknown_language_degrades_to_english(self):
        for code in ("fr", "en-GB", "", "../etc/passwd"):
            assert template(locales.ALREADY_VERIFIED, code) == locales.ALREADY_VERIFIED
