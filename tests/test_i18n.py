"""The dashboard's twelve languages (issue #97).

Split into three kinds of test, and the split is the point:

* **The negotiation** is pure, so it is asserted directly against
  `i18n.negotiate` rather than through a test client. Precedence between a
  cookie, a guild's setting and an `Accept-Language` header is the thing most
  likely to be got wrong later, and it deserves to fail in one line rather
  than in a page render.
* **The catalogues** are files on disk, and what is worth pinning about them
  is not their wording -- that is a translator's -- but that they exist, that
  they compile, and that they agree with the bot about which languages there
  are.
* **The request** is where the parts meet: the `lang` attribute, the picker,
  and the one thing that could go quietly wrong on the page that takes money.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dashboard import i18n  # noqa: E402


class TestTheLanguageListMatchesTheBot:
    """#97's first constraint, and the one a second system can violate.

    The issue is explicit that two translation systems disagreeing about what
    "verified" is called in Japanese is worse than either alone. The wording is
    a review problem and cannot be tested. WHICH LANGUAGES EXIST can be, and
    this is where the two systems are held together.
    """

    def test_the_dashboard_offers_exactly_what_the_bot_speaks(self):
        # Imported here rather than at module scope: the dashboard image does
        # not ship locales.py, and this test runs from the repo root where it
        # is importable. That asymmetry is the whole reason UI_LANGUAGES is a
        # literal instead of an import -- see the comment on it.
        from locales import LANGUAGE_CODES

        assert list(i18n.UI_LANGUAGES) == list(LANGUAGE_CODES)

    def test_every_language_has_a_name_in_its_own_script(self):
        """A picker labelled in English is no use to the person opening it."""
        for code in i18n.UI_LANGUAGES:
            assert code in i18n.ENDONYMS, code
            assert i18n.ENDONYMS[code].strip()

    def test_english_is_the_default_and_has_no_catalogue_directory(self):
        """Its catalogue is the msgids. A directory for it would be a file of
        entries translating English into the same English, each one a chance
        to drift."""
        assert i18n.DEFAULT_LANGUAGE == "en-US"
        assert not os.path.isdir(os.path.join(i18n.LOCALE_DIR, "en_US"))


class TestEveryCatalogueIsCompiledAndLoadable:
    """A .po that was never compiled is a translation that silently does not
    ship: gettext has no way to say "there is a newer translation you did not
    compile", it just serves the English. That failure is invisible in review
    and obvious to a reader, which is the worst way round."""

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_the_compiled_catalogue_is_in_the_tree(self, code):
        path = os.path.join(
            i18n.LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", "dashboard.mo"
        )
        assert os.path.isfile(path), f"run scripts/i18n.sh: {path} is missing"

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_the_money_sentences_are_actually_translated(self, code):
        """The four #97 named as the expensive ones to misunderstand.

        Not a spot check on wording -- it cannot be, from here -- but on the
        thing that would make the whole feature a no-op: a catalogue that
        loads, reports success, and hands back the English for every string.
        """
        gettext = i18n.translator(code)
        for english in (
            "Renews",
            "Premium until",
            "Manage billing",
            "All plans renew automatically. Cancel any time from this page.",
        ):
            assert gettext(english) != english, f"{code}: {english!r} untranslated"

    @pytest.mark.parametrize(
        "code", [c for c in i18n.UI_LANGUAGES if c != i18n.DEFAULT_LANGUAGE]
    )
    def test_no_catalogue_hides_english_behind_a_fuzzy_flag(self, code):
        """The trap `pybabel --statistics` sets, and the one that caught us.

        A fuzzy entry is Babel's guess that an old translation still fits a
        changed English string. `--statistics` counts it as translated;
        `compile` without `--use-fuzzy` drops it. So a catalogue can report
        "154 of 154 (100%)" and serve English for four of them -- which is
        what happened when the sidebar's labels were added and "Settings" was
        fuzzy-matched against "Settings sections". The only symptom was an
        English word in a German sidebar, in a screenshot.

        Not shipping the guess is the right policy on a site where the changed
        strings are disproportionately about money. Being told is the part
        that was missing.
        """
        pytest.importorskip("babel", reason="Babel is a dev-only dependency")
        from babel.messages.pofile import read_po

        path = os.path.join(
            i18n.LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", "dashboard.po"
        )
        with open(path, "rb") as handle:
            catalog = read_po(handle)
        fuzzy = [m.id for m in catalog if m.id and "fuzzy" in m.flags]
        untranslated = [m.id for m in catalog if m.id and not m.string]
        assert not fuzzy, f"{code}: fuzzy entries render English: {fuzzy}"
        assert not untranslated, f"{code}: untranslated: {untranslated}"

    def test_english_hands_back_the_msgid(self):
        gettext = i18n.translator("en-US")
        assert gettext("Renews") == "Renews"

    def test_an_unknown_language_degrades_to_english_rather_than_raising(self):
        """A stale cookie naming a language since dropped, or a bot running
        ahead of a dashboard deploy. Both are normal on two hosts."""
        assert i18n.translator("xx-YY")("Renews") == "Renews"


class TestPrecedence:
    """Which of the three inputs wins, and in what order.

    Asserted against the pure function. The order is a product decision and
    reads as one here, rather than being inferred from a page render.
    """

    def test_the_picker_beats_everything(self):
        """#97 calls the guild's language "the least discoverable if it is
        wrong". This is the answer to that: an explicit choice always wins."""
        assert i18n.negotiate(cookie="ja", guild_locale="de", accept_language="ru") == "ja"

    def test_the_guild_beats_the_browser(self):
        """A server configured in German gets a German dashboard with nobody
        doing anything, which is the whole consistency argument with the bot."""
        assert i18n.negotiate(guild_locale="de", accept_language="ru") == "de"

    def test_the_browser_is_the_floor_before_english(self):
        """The only one of the three available on the sign-in page."""
        assert i18n.negotiate(accept_language="ru,en;q=0.5") == "ru"

    def test_nothing_at_all_is_english(self):
        assert i18n.negotiate() == "en-US"

    def test_an_unsupported_value_falls_THROUGH_rather_than_failing(self):
        """A cookie naming a language we cannot render must show the guild's,
        not an error and not English-when-German-was-available."""
        assert i18n.negotiate(cookie="xx", guild_locale="de") == "de"
        assert i18n.negotiate(cookie="../../etc/passwd", accept_language="de") == "de"


class TestAcceptLanguage:
    """Parsed here rather than taken from Werkzeug, so it can be asserted
    without a request -- and because this header is attacker-controlled and
    arrives on every single request."""

    def test_quality_values_order_the_result(self):
        assert i18n.parse_accept_language("en;q=0.2, ja;q=0.9, de") == ["de", "ja", "en-US"]

    def test_a_base_language_matches_the_variant_we_carry(self):
        """One Portuguese catalogue read by a Portuguese speaker beats
        English."""
        assert i18n.parse_accept_language("pt-PT") == ["pt-BR"]
        assert i18n.parse_accept_language("ja-JP") == ["ja"]

    def test_q_zero_is_a_refusal_not_a_weak_preference(self):
        assert i18n.parse_accept_language("de;q=0") == []

    def test_one_malformed_tag_does_not_discard_the_good_ones(self):
        assert i18n.parse_accept_language("!!!,ja") == ["ja"]

    def test_a_wildcard_is_not_a_preference(self):
        """Otherwise `*` would outrank the guild's configured language."""
        assert i18n.parse_accept_language("*") == []

    def test_an_absurd_header_is_refused_before_it_is_parsed(self):
        assert i18n.parse_accept_language("de," * 5000) == []

    def test_nothing_unsupported_ever_comes_back(self):
        """The caller never has to re-check, which is what stops an unchecked
        value reaching a `lang` attribute or a catalogue path."""
        for code in i18n.parse_accept_language("fr, ja, kl, de, xx-YY"):
            assert i18n.is_supported(code)


class TestDirection:
    """`dir` ships from the first day; the stylesheet catches up separately.

    The two are not the same job. `dir` decides which end of the line a
    sentence starts at and how a mixed string like "VRCVerify Premium" is
    ordered inside an Arabic one. Getting that wrong is unreadable in a way an
    unmirrored sidebar is not.
    """

    def test_arabic_is_rtl_and_nothing_else_is(self):
        assert i18n.direction("ar") == "rtl"
        for code in i18n.UI_LANGUAGES:
            if code != "ar":
                assert i18n.direction(code) == "ltr", code


class TestTheNoOpMarker:
    def test_it_returns_its_argument(self):
        """Its entire job is being a name `pybabel extract -k N_` recognises,
        so that a table built at import can hold msgids and be looked up per
        request."""
        assert i18n.N_("Renews") == "Renews"
