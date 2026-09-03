"""Every bot string, in every language, rendered and frozen (#231).

This is the guard that makes the gettext conversion reviewable. #231 moves the
bot's twelve languages out of the `localizations` dict in src/locales.py and
into gettext catalogues -- 1,092 renderings, in eleven languages nobody on this
project reads, through a converter script, a .po round trip and a compile step.

Every other test in the suite asks whether the machinery works. This one asks
the only question that actually matters to a user: does the Japanese sentence
that arrives in their DMs today still arrive, byte for byte, tomorrow.

**Why a fixture and not a property.** The failure this catches is silent. A
string that comes out of the conversion in English instead of Japanese raises
nothing, renders fine, and is wrong only to the one group of people least able
to report it here. There is no invariant to assert against -- "the Japanese for
'Verification request received!'" is not derivable, it is data. So it gets
recorded before the change and compared after, which is the only form of the
check that can fail for the right reason.

The fixture was generated from the dict as it stood at the head of the #231
branch, before any of the conversion landed. It is not regenerated. If a change
makes this test fail, the change is either a wording change -- which #231 rules
out as a non-goal and which belongs in its own PR -- or the conversion losing a
string. Regenerating the fixture to get back to green would convert this from a
guard into a rubber stamp.

**Why it renders rather than comparing tables.** `.format()` is where a broken
placeholder turns into a KeyError and where a translation that invented its own
`{name}` blows up. Rendering with real arguments puts that on the same footing
as the text itself, so the round trip is checked end to end: catalogue lookup,
fallback, and substitution.
"""

import json
import pathlib
from types import SimpleNamespace

import pytest

import bot
import locales

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "bot_strings.json"

_snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
ARGS = _snapshot["args"]
STRINGS = _snapshot["strings"]

# Every string is formatted with every argument. str.format ignores the ones a
# template does not mention, so this avoids a per-key argument table that would
# itself need maintaining -- and it means a translation that invents a
# placeholder the English never had fails here rather than in front of a user.
CASES = [
    (key, code) for key in sorted(STRINGS) for code in sorted(STRINGS[key])
]


def msgid_for(key: str) -> str:
    """The first argument to `get_message` for a string, on either side of #231.

    Before the conversion that is the symbolic key itself (`not_verified`).
    After it, `locales.py` holds English constants marked with `N_`, and the
    argument is the English text (`locales.NOT_VERIFIED`). The uppercased key
    is the constant's name, by construction, so this resolves through whichever
    of the two `locales.py` currently is.

    Written this way so the file does not need editing mid-conversion. A
    snapshot test that gets rewritten in the same commit as the thing it is
    snapshotting is not a snapshot test.
    """
    return getattr(locales, key.upper(), key)


def interaction(locale: str):
    """Minimal stand-in for a discord.Interaction: `get_locale` reads `.locale`
    and nothing else."""
    return SimpleNamespace(locale=locale)


@pytest.mark.parametrize("key,code", CASES, ids=[f"{k}-{c}" for k, c in CASES])
def test_the_string_is_byte_identical_to_the_snapshot(key, code):
    assert bot.get_message(msgid_for(key), interaction(code), **ARGS) == (
        STRINGS[key][code]
    )


def test_the_snapshot_covers_every_string_in_every_language():
    """The comparison above is only as good as its coverage.

    Without this, deleting a key from `locales.py` -- or dropping a language on
    the floor during the conversion -- removes cases from `CASES` and the suite
    goes green with less in it than it started with. A snapshot that shrinks
    silently is the failure mode a snapshot exists to prevent.
    """
    assert len(STRINGS) == 91, f"expected 91 bot strings, snapshot has {len(STRINGS)}"
    assert len(CASES) == 1092, f"expected 1,092 renderings, got {len(CASES)}"
    for key, per_locale in STRINGS.items():
        assert sorted(per_locale) == sorted(locales.LANGUAGE_CODES), (
            f"{key} is not snapshotted in every supported language"
        )
