#!/usr/bin/env python3
"""One-shot: turn locales.localizations into gettext catalogues (#231).

TEMPORARY. This script exists for the length of the #231 branch and is deleted
in the same commit that deletes the dict. It is committed rather than run from
a shell buffer so that the conversion is reviewable -- the 24,937 words it
moves are in eleven languages nobody on this project reads, and "trust me, I
ran a script" is not a reviewable claim about them.

WHAT IT DOES, AND WHAT IT REFUSES TO DO
---------------------------------------
Nothing is retranslated. Every msgstr it writes is a string that is already in
locales.py, paired with its en-US counterpart as the msgid. The mapping is
key -> English text, which is 1:1: all 91 English strings are distinct, checked
below rather than assumed, because two keys sharing English text would collapse
into one msgid and silently take one of their two translations.

The one string it will not carry is support_invite_line, English in all eleven
tables on purpose and tracked by the UNTRANSLATED allowlist in
tests/test_locales.py. #231 decided to translate it rather than carry the
allowlist into gettext, so this script leaves its msgstr empty and the
translation is written by hand. Copying English into eleven msgstrs would be
indistinguishable, afterwards, from eleven real translations.

    ./scripts/dict_to_po.py           # write the .pot and eleven .po files
    ./scripts/dict_to_po.py --check   # verify what was written, change nothing

Then run ./scripts/i18n.sh to compile.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po, write_po

from locales import localizations, LANGUAGE_CODES  # noqa: E402

DOMAIN = "bot"
LOCALE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src", "translations", "bot"
)
DEFAULT = "en-US"

# English in every table on purpose, until #231. The generator leaves its
# msgstr empty rather than copying the English across, because a copy would be
# indistinguishable afterwards from eleven real translations -- and the whole
# point of #231's decision to delete the UNTRANSLATED allowlist is that
# "msgstr equals msgid" goes back to meaning "nobody has translated this".
#
# The eleven translations are then written by hand into the .po files. --check
# asserts that happened: non-empty, and not a copy of the English.
LEAVE_EMPTY = {"support_invite_line"}

# ...and here they are. Written by hand, in this file rather than straight into
# the .po files, so that regenerating the catalogues cannot silently drop them
# -- which is the one way this conversion could quietly lose a string it was
# supposed to gain.
#
# Register is matched to each catalogue's existing voice rather than chosen
# fresh: informal in de/nl/es-ES, 您 in zh-CN, polite -masu in ja, вы in ru,
# আপনি/ਤੁਹਾਡੇ in bn/pa-IN. `{invite}` is the one thing the sentence exists to
# deliver, so --check asserts every one of them still carries it.
HAND_TRANSLATED = {
    "support_invite_line": {
        "es-ES": "Recibe novedades de VRCVerify en tu propio servidor: únete a {invite} y sigue el canal de anuncios.",
        "zh-CN": "在您自己的服务器中获取 VRCVerify 更新：加入 {invite} 并关注公告频道。",
        "ja": "VRCVerify の最新情報をご自身のサーバーで受け取れます。{invite} に参加して、アナウンスチャンネルをフォローしてください。",
        "de": "Hol dir VRCVerify-Neuigkeiten in deinen eigenen Server: Tritt {invite} bei und folge dem Ankündigungskanal.",
        "nl": "Ontvang VRCVerify-updates in je eigen server: word lid van {invite} en volg het aankondigingskanaal.",
        "hi-IN": "अपने सर्वर में VRCVerify के अपडेट पाएं: {invite} से जुड़ें और घोषणा चैनल को फ़ॉलो करें।",
        "ar": "احصل على تحديثات VRCVerify في خادمك الخاص: انضم إلى {invite} وتابع قناة الإعلانات.",
        "bn": "আপনার নিজের সার্ভারে VRCVerify-এর আপডেট পান: {invite}-এ যোগ দিন এবং ঘোষণা চ্যানেল ফলো করুন।",
        "pt-BR": "Receba novidades do VRCVerify no seu próprio servidor: entre em {invite} e siga o canal de anúncios.",
        "ru": "Получайте новости VRCVerify на своём сервере: присоединяйтесь к {invite} и подпишитесь на канал объявлений.",
        "pa-IN": "ਆਪਣੇ ਸਰਵਰ ਵਿੱਚ VRCVerify ਦੇ ਅਪਡੇਟ ਪ੍ਰਾਪਤ ਕਰੋ: {invite} ਵਿੱਚ ਸ਼ਾਮਲ ਹੋਵੋ ਅਤੇ ਐਲਾਨ ਚੈਨਲ ਨੂੰ ਫ਼ਾਲੋ ਕਰੋ।",
    },
}


def check_msgids_are_unique() -> dict:
    """key -> English text, asserted 1:1 before anything is written."""
    english = localizations[DEFAULT]
    by_text = collections.defaultdict(list)
    for key, text in english.items():
        by_text[text].append(key)
    collisions = {t: k for t, k in by_text.items() if len(k) > 1}
    if collisions:
        for text, keys in collisions.items():
            print(f"  COLLISION: {keys} share {text[:60]!r}", file=sys.stderr)
        raise SystemExit(
            "two keys share one English string, so they would share one msgid "
            "and one translation. Reword one of them before converting."
        )
    return english


def build(english: dict) -> None:
    os.makedirs(LOCALE_DIR, exist_ok=True)

    # The .pot: msgids only, no translations. Its header matches the one
    # scripts/i18n.sh passes to pybabel so that the first `pybabel update`
    # after this does not rewrite every line of it.
    pot = Catalog(
        project="VRCVerify",
        copyright_holder="Esatto Technologies",
        charset="utf-8",
        # NOT FUZZY. Babel's Catalog defaults to fuzzy=True, which writes a
        # `#, fuzzy` on the header entry and marks the WHOLE CATALOGUE as a
        # guess -- and `pybabel compile` then skips the file entirely, saying
        # "91 of 91 messages (100%) translated" on the line above the one where
        # it skips it. The first run of this script hit exactly that: eleven
        # complete catalogues, eleven empty .mo files, every language English.
        fuzzy=False,
    )
    for key in sorted(english):
        pot.add(english[key], string="", auto_comments=[f"key: {key}"])
    pot_path = os.path.join(LOCALE_DIR, f"{DOMAIN}.pot")
    with open(pot_path, "wb") as handle:
        write_po(handle, pot, width=None, omit_header=False, sort_output=False)
    print(f"  {pot_path}  ({len(english)} msgids)")

    for code in LANGUAGE_CODES:
        if code == DEFAULT:
            continue  # the source language's catalogue is the msgids themselves
        catalog = Catalog(
            locale=code.replace("-", "_"),
            project="VRCVerify",
            copyright_holder="Esatto Technologies",
            charset="utf-8",
            fuzzy=False,  # see the .pot above
        )
        translated = 0
        for key in sorted(english):
            if key in LEAVE_EMPTY:
                string = HAND_TRANSLATED.get(key, {}).get(code, "")
            else:
                string = localizations[code][key]
            catalog.add(english[key], string=string, auto_comments=[f"key: {key}"])
            translated += bool(string)
        directory = os.path.join(LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{DOMAIN}.po")
        with open(path, "wb") as handle:
            write_po(handle, catalog, width=None, omit_header=False, sort_output=False)
        print(f"  {path}  ({translated}/{len(english)} translated)")


def check() -> int:
    """Every .po parses, and says exactly what the dict says. Nothing is written.

    This is the guard #231 asks for before bot.py is touched: the snapshot test
    cannot run against the catalogues until get_message reads them, so the
    round trip through write_po/read_po is verified here on its own first.
    """
    english = localizations[DEFAULT]
    problems = []
    for code in LANGUAGE_CODES:
        if code == DEFAULT:
            continue
        path = os.path.join(
            LOCALE_DIR, code.replace("-", "_"), "LC_MESSAGES", f"{DOMAIN}.po"
        )
        if not os.path.exists(path):
            problems.append(f"{code}: no catalogue at {path}")
            continue
        with open(path, "rb") as handle:
            catalog = read_po(handle)
        entries = {str(m.id): str(m.string) for m in catalog if m.id}
        if len(entries) != len(english):
            problems.append(f"{code}: {len(entries)} msgids, expected {len(english)}")
        for key, text in english.items():
            if text not in entries:
                problems.append(f"{code}: missing msgid for {key}")
            elif key in LEAVE_EMPTY:
                if not entries[text]:
                    problems.append(f"{code}: {key} still awaits its hand translation")
                elif entries[text] == text:
                    problems.append(f"{code}: {key} is a copy of the English, not a translation")
                elif "{invite}" not in entries[text]:
                    problems.append(f"{code}: {key} dropped the {{invite}} placeholder")
            elif entries[text] != localizations[code][key]:
                problems.append(f"{code}: {key} does not round-trip through .po")
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(
        f"  all {len(LANGUAGE_CODES) - 1} catalogues parse; {len(english)} msgids each; "
        f"{len(english)} non-empty msgstrs, none a copy of its msgid"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify, write nothing")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(check())
    english = check_msgids_are_unique()
    print("==> writing")
    build(english)
    print("==> checking")
    raise SystemExit(check())
