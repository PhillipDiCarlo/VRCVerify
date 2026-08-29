#!/usr/bin/env bash
#
# Re-extract, merge and compile the dashboard's translation catalogues (#97).
#
# Run this after changing ANY user-facing string in src/dashboard. The three
# steps below are one operation and are not useful apart:
#
#   extract  reads the templates and the Python, writes dashboard.pot
#   update   merges that into the twelve .po files, keeping every translation
#            that still has a matching msgid and marking the rest fuzzy
#   compile  turns the .po files into the .mo files the site actually reads
#
# Doing the first without the second leaves the catalogues behind the
# templates. Doing the second without the third leaves the running site behind
# the catalogues, silently -- gettext has no way to say "there is a newer
# translation you did not compile", it simply serves the English.
#
# BABEL IS A DEV DEPENDENCY AND STAYS ONE. Nothing here runs on the VPS. The
# .mo files are committed and copied into the image with the rest of
# src/dashboard/, and the running app reads them with the standard library.
# See the note at the top of requirements-dashboard.txt for why the
# internet-facing image does not grow a package to gain a feature.
#
#   pip install -r config/other_configs/requirements-dev.txt
#   ./scripts/i18n.sh
#
# Then open the changed .po files, translate what is new, and run it again.

set -euo pipefail

cd "$(dirname "$0")/.."

DOMAIN=dashboard
CONFIG=config/other_configs/babel.cfg
SOURCE=src/dashboard
CATALOGUES="$SOURCE/translations"
POT="$CATALOGUES/$DOMAIN.pot"

# The twelve. Underscored, because that is how gettext names a directory --
# i18n.py translates the hyphenated codes the bot and Discord use into these,
# in one function, so this is the only other place the spelling appears.
LANGUAGES=(es_ES zh_CN ja de nl hi_IN ar bn pt_BR ru pa_IN)
# en_US is absent on purpose. Its catalogue is the msgids themselves; a
# directory for it would be a file full of entries translating English into
# the same English, with every one of them a chance to drift.

if ! command -v pybabel >/dev/null 2>&1; then
	echo "pybabel not found. Install the dev dependencies first:" >&2
	echo "  pip install -r config/other_configs/requirements-dev.txt" >&2
	exit 1
fi

mkdir -p "$CATALOGUES"

echo "==> extract"
# `-k N_` is not optional. The view modules keep their labels in tables built
# at import, so they are marked with the no-op `N_` and looked up per request
# -- see the docstring on i18n.N_. Without this flag every one of those msgids
# is silently missing from the .pot, and the only symptom is a chip that stays
# in English.
#
# `--no-location` because the alternative is a 400-line diff every time a
# template gains a paragraph: the line numbers of every msgid below the change
# shift, and reviewing a translation update becomes reviewing line noise.
pybabel extract \
	-F "$CONFIG" \
	-k N_ \
	--no-location \
	--project=VRCVerify \
	--copyright-holder="Esatto Technologies" \
	-o "$POT" \
	"$SOURCE"

echo "==> update"
for lang in "${LANGUAGES[@]}"; do
	if [ -d "$CATALOGUES/$lang" ]; then
		pybabel update -i "$POT" -d "$CATALOGUES" -D "$DOMAIN" -l "$lang" \
			--previous
	else
		pybabel init -i "$POT" -d "$CATALOGUES" -D "$DOMAIN" -l "$lang"
	fi
done

echo "==> compile"
# NOT `--use-fuzzy`. A fuzzy entry is Babel's guess that an old translation
# still fits a changed English string, and on this site the changed strings are
# disproportionately the ones about money -- a price, a renewal date, a
# cancellation. Shipping a guess there is worse than shipping English, so a
# fuzzy entry renders the msgid until a person has looked at it.
pybabel compile -d "$CATALOGUES" -D "$DOMAIN" --statistics

echo
echo "==> fuzzy check"
# `--statistics` COUNTS A FUZZY ENTRY AS TRANSLATED, and compile then drops it.
# So a catalogue can report "154 of 154 messages (100%)" and still serve English
# for four of them -- which is exactly what happened when the sidebar's labels
# were added: pybabel matched "Settings" against "Settings sections", marked it
# fuzzy, counted it, and compiled it out.
#
# The only symptom was a German page with an English word in the sidebar, found
# by looking at a screenshot. This turns that into a line of output.
python3 - <<'PY'
import glob, sys
from babel.messages.pofile import read_po

trouble = False
for path in sorted(glob.glob("src/dashboard/translations/*/LC_MESSAGES/dashboard.po")):
    catalog = read_po(open(path, "rb"))
    fuzzy = [m.id for m in catalog if m.id and "fuzzy" in m.flags]
    empty = [m.id for m in catalog if m.id and not m.string]
    if fuzzy or empty:
        trouble = True
        lang = path.split("/")[3]
        for msgid in fuzzy:
            print(f"  {lang}: FUZZY (will render English) {msgid[:60]!r}")
        for msgid in empty:
            print(f"  {lang}: UNTRANSLATED {msgid[:60]!r}")
if not trouble:
    print("  no fuzzy or untranslated entries: every catalogue ships complete")
sys.exit(0)
PY

echo
echo "Done. Translate anything listed above in:"
echo "  $CATALOGUES/<lang>/LC_MESSAGES/$DOMAIN.po"
echo "then run this again to compile it. Clear the #, fuzzy line when you do --"
echo "compile drops fuzzy entries, deliberately: a guess about a renewal date"
echo "is worse than the English."
