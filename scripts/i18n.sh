#!/usr/bin/env bash
#
# Re-extract, merge and compile the translation catalogues, both domains.
#
# Run this after changing ANY user-facing string, in src/dashboard (#97) or in
# src/locales.py (#231). The three steps below are one operation and are not
# useful apart:
#
#   extract  reads the templates and the Python, writes the .pot
#   update   merges that into the eleven .po files, keeping every translation
#            that still has a matching msgid and marking the rest fuzzy
#   compile  turns the .po files into the .mo files that actually get read
#
# Doing the first without the second leaves the catalogues behind the source.
# Doing the second without the third leaves the running code behind the
# catalogues, silently -- gettext has no way to say "there is a newer
# translation you did not compile", it simply serves the English.
#
# TWO DOMAINS, ON PURPOSE. dashboard.pot and bot.pot stay separate so neither
# image carries the other's strings. The cost is that a string appearing on
# both sides gets translated twice; the mitigation, which did not exist while
# the bot's strings were a Python dict, is that both halves are now in a format
# a translation memory can read across.
#
# BABEL DOES NOT ENTER THE BOT IMAGE. Nothing here runs on the VPS: the .mo
# files are committed and copied in with the source, and both runtimes read
# them with the standard library's `gettext`. That was once true of the whole
# repo and is now true only of the strings -- #230 gave Babel a runtime job
# formatting the dashboard's dates and numbers, so it is in
# requirements-dashboard.txt. requirements-bot.txt gains nothing.
#
#   pip install -r config/other_configs/requirements-dev.txt
#   ./scripts/i18n.sh
#
# Then open the changed .po files, translate what is new, and run it again.

set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=config/other_configs/babel.cfg

# The eleven. Underscored, because that is how gettext names a directory --
# i18n_core translates the hyphenated codes the bot and Discord use into these,
# in one function, so this is the only other place the spelling appears.
LANGUAGES=(es_ES zh_CN ja de nl hi_IN ar bn pt_BR ru pa_IN)
# en_US is absent on purpose, in both domains. Its catalogue is the msgids
# themselves; a directory for it would be a file full of entries translating
# English into the same English, with every one of them a chance to drift.

if ! command -v pybabel >/dev/null 2>&1; then
	echo "pybabel not found. Install the dev dependencies first:" >&2
	echo "  pip install -r config/other_configs/requirements-dev.txt" >&2
	exit 1
fi

# domain, source path to extract from, directory holding the catalogues
run_domain() {
	local domain="$1" source="$2" catalogues="$3"
	local pot="$catalogues/$domain.pot"

	echo
	echo "=================== $domain ==================="
	mkdir -p "$catalogues"

	echo "==> extract"
	# `-k N_` is not optional. Both domains keep strings in tables built at
	# import and looked up per request or per interaction -- the dashboard's
	# view modules hold their labels that way, and since #231 every bot string
	# is an N_() constant in locales.py. Without this flag every one of those
	# msgids is silently missing from the .pot, and the only symptom is a
	# string that stays in English.
	#
	# `--no-location` because the alternative is a 400-line diff every time a
	# template gains a paragraph: the line numbers of every msgid below the
	# change shift, and reviewing a translation update becomes reviewing line
	# noise.
	pybabel extract \
		-F "$CONFIG" \
		-k N_ \
		--no-location \
		--project=VRCVerify \
		--copyright-holder="Esatto Technologies" \
		-o "$pot" \
		"$source"

	echo "==> update"
	for lang in "${LANGUAGES[@]}"; do
		if [ -d "$catalogues/$lang" ]; then
			pybabel update -i "$pot" -d "$catalogues" -D "$domain" -l "$lang" \
				--previous
		else
			pybabel init -i "$pot" -d "$catalogues" -D "$domain" -l "$lang"
		fi
	done

	echo "==> compile"
	# NOT `--use-fuzzy`. A fuzzy entry is Babel's guess that an old translation
	# still fits a changed English string. On the dashboard the changed strings
	# are disproportionately the ones about money -- a price, a renewal date, a
	# cancellation. In the bot they include role assignment failures and the
	# premium pitch. A wrong guess in either place is a support ticket, so a
	# fuzzy entry renders the msgid until a person has looked at it.
	pybabel compile -d "$catalogues" -D "$domain" --statistics
}

run_domain dashboard src/dashboard src/dashboard/translations
run_domain bot src/locales.py src/translations/bot

echo
echo "==> fuzzy check, both domains"
# `--statistics` COUNTS A FUZZY ENTRY AS TRANSLATED, and compile then drops it.
# So a catalogue can report "154 of 154 messages (100%)" and still serve English
# for four of them -- which is exactly what happened when the sidebar's labels
# were added: pybabel matched "Settings" against "Settings sections", marked it
# fuzzy, counted it, and compiled it out. The only symptom was a German page
# with an English word in the sidebar, found by looking at a screenshot.
#
# The same trap has a whole-file form, which #231 walked into: Babel's Catalog
# defaults to fuzzy, which marks the HEADER fuzzy, which makes compile skip the
# entire catalogue -- printing "91 of 91 (100%)" on the line above the one where
# it skips it. Eleven complete .po files, eleven empty .mo files, every language
# English. This turns both into a line of output.
python3 - <<'PY'
import glob, sys
from babel.messages.pofile import read_po

PATTERNS = [
    ("dashboard", "src/dashboard/translations/*/LC_MESSAGES/dashboard.po"),
    ("bot", "src/translations/bot/*/LC_MESSAGES/bot.po"),
]

trouble = False
for domain, pattern in PATTERNS:
    for path in sorted(glob.glob(pattern)):
        catalog = read_po(open(path, "rb"))
        lang = path.split("/")[-3]
        if catalog.fuzzy:
            trouble = True
            print(f"  {domain}/{lang}: THE WHOLE CATALOGUE IS FUZZY -- compile "
                  f"will skip every entry in it and serve English")
        fuzzy = [m.id for m in catalog if m.id and "fuzzy" in m.flags]
        empty = [m.id for m in catalog if m.id and not m.string]
        if fuzzy or empty:
            trouble = True
        for msgid in fuzzy:
            print(f"  {domain}/{lang}: FUZZY (will render English) {msgid[:60]!r}")
        for msgid in empty:
            print(f"  {domain}/{lang}: UNTRANSLATED {msgid[:60]!r}")
if not trouble:
    print("  no fuzzy or untranslated entries: every catalogue ships complete")
sys.exit(0)
PY

echo
echo "Done. Translate anything listed above in:"
echo "  src/dashboard/translations/<lang>/LC_MESSAGES/dashboard.po"
echo "  src/translations/bot/<lang>/LC_MESSAGES/bot.po"
echo "then run this again to compile it. Clear the #, fuzzy line when you do --"
echo "compile drops fuzzy entries, deliberately: a guess about a renewal date"
echo "or a failed role assignment is worse than the English."
