"""Every colour pair the dashboard actually renders, against WCAG AA.

WHY THIS IS A TEST AND NOT A ONE-OFF AUDIT

The palette was audited by hand once (issue #123 phase 4) and four dark pairs
and three light ones were below AA -- including blurple as a link on a dark
card at 2.7:1. None of that was visible in review, because a hex value in a
diff looks like every other hex value. A ratio does not.

So the audit is pinned here. Change a colour and this fails with the pair and
the number, rather than shipping a theme nobody can read.

WHAT IS DELIBERATELY NOT CHECKED

Only pairs that are really drawn. `--faint` on `--panel` is a real
combination; `--ok` on `--chrome` is not, and asserting it would be inventing
a requirement to satisfy a requirement.

Contrast is also not the whole of legibility -- weight, size and surrounding
colour all matter, and none of them are measurable here. Passing this is a
floor, not a finish.
"""

from __future__ import annotations

import pathlib
import re

import pytest

STYLE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src" / "dashboard" / "static" / "style.css"
)

# 4.5:1 is AA for body text. Everything below is text or a state indicator
# sitting on a surface, so the stricter figure applies to all of it -- rather
# than reaching for the 3:1 large-text allowance to make a value pass.
AA_TEXT = 4.5

# WCAG 1.4.11. A control's boundary, and anything inside it that carries
# meaning, only has to reach 3:1 -- a lower bar than text because a shape is
# not read letter by letter.
AA_UI = 3.0

# fg token, bg token, what it is on screen.
PAIRS = [
    ("ink", "panel", "body text on a card"),
    ("ink", "bg", "body text on the page ground"),
    ("ink", "chrome", "body text on the bar and sidebar"),
    ("ink", "inset", "text typed into an input"),
    ("ink-strong", "panel", "headings and figures on a card"),
    ("muted", "panel", "descriptions under a control"),
    ("muted", "chrome", "the guild name in the sidebar"),
    ("muted", "bg", "descriptions on the ground"),
    ("faint", "panel", "the least important text on a card"),
    ("faint", "chrome", "the least important text in the bar"),
    ("faint", "bg", "the least important text on the ground"),
    ("accent-ink", "accent", "a button's label on blurple"),
    ("accent-text", "panel", "a link or tick on a card"),
    ("accent-text", "chrome", "the current section in the sidebar"),
    ("accent-text", "bg", "an accent stripe on the ground"),
    ("ok", "panel", '"Saved."'),
    ("notice", "notice-bg", "a warning in its own box"),
    # Unboxed, straight on the card -- the Setup list's "todo" and "broken"
    # rows (#135 phase 3). Used since #123's setup checklist for a missing
    # required field, but never actually pinned until now.
    ("notice", "panel", "an unfinished or broken row in the setup list"),
    # The success notice has no fill of its own and sits on the card. Filling
    # it to match the warning was tried and is 4.14:1 in light -- see the
    # comment on .notice.ok.
    ("ok", "panel", "a save confirmation"),
    # #141 phase 1: the subscription status chips. Tinted text on a hairline
    # of the same colour, drawn straight on the card -- so the text itself has
    # to clear 4.5:1 there, not just the border. `ok` and `notice` on `panel`
    # are already above; `muted` on `panel` is too. Listed by name anyway,
    # because these are the words that say whether somebody is being charged
    # twice and it should be obvious which pairs that claim rests on.
    ("ok", "panel", "the ACTIVE subscription chip"),
    ("notice", "panel", "the PAYMENT FAILED and CHARGED TWICE chips"),
    ("muted", "panel", "the CONFIRMING and UNKNOWN chips"),
    ("ink-strong", "inset", "the lapsed win-back heading"),
    ("muted", "inset", "the lapsed win-back sentence"),
    # #141 phase 2: the plan cards are drawn on --inset, not --panel, so every
    # pair on them needs its own entry. Only `ink on inset` was listed before
    # -- for text typed into an input -- and the price, the saving, the plan
    # label and the trial chip were all riding on that one line.
    ("ink", "inset", "the price on a plan card"),
    ("muted", "inset", "a plan's label"),
    ("faint", "inset", "the per-period suffix beside a price"),
    ("ok", "inset", "a plan's saving"),
    ("accent-text", "inset", "the trial chip on a plan card"),
    # #140 phase 3: the sub-nav's current group sits on the same selected pill
    # the current section always did -- a pairing that shipped with the sidebar
    # and was never on this list. Adding a second, smaller use of it is the
    # cheapest moment to find out whether the first one was ever legible.
    ("ink-strong", "selected", "the current section and group in the sidebar"),
    # #159: surfaces this list said it covered and did not. The claim at the top
    # is "every colour pair the dashboard actually renders", and --hover had no
    # entries at all -- a whole surface, three foregrounds, never measured. It
    # is the fill every row in the bell panel, the account menu and the sidebar
    # takes under the pointer.
    #
    # THESE WERE READ OFF THE RENDERED PAGE, not off the rules. Reasoning from
    # the stylesheet alone gets this wrong: `.bell-all:hover` sets a background
    # and no colour, which looks like an accent link sitting on --hover at
    # 4.30:1 in dark, under AA. It is not. The global `a:hover` rule takes it to
    # --ink-strong at the same moment, and measuring in a browser says 10.73.
    # Every number below came from computed styles under a real pointer.
    ("ink", "hover", "a menu row, bar button or bell button under the pointer"),
    ("ink-strong", "hover", "a link or sidebar row under the pointer"),
    ("muted", "hover", "the second line of a menu row under the pointer"),
    ("ink-strong", "chrome", "the wordmark and the guild name in the bar"),
    ("accent-ink", "accent-hover", "a button's label while the pointer is on it"),
]

# NOT here, though it is drawn: --accent-text on --hover.
#
# The two things wearing it are the bell's unread dot and the tick beside the
# current row in the language and theme menus. Both are shapes, so both belong
# at the 3:1 floor in UI_PAIRS, where the first already was. It is 4.30:1 in
# dark -- fine for a glyph, and it would fail here. That is the distinction
# this file's two lists exist to make, so the entry goes in one and not both.

# Deliberately NOT here: --ok on --bg, and --notice on --bg.
#
# Both were real when #159 was written -- the subscription page's confirmation
# was a direct child of <main> and landed on the page ground at 4.27:1. The fix
# was to put it in a card like every other notice in the app, not to pin the
# pairing, and pinning it now would assert a combination nothing renders. That
# is the thing this file's docstring says it will not do.
#
# What stops it coming back is not here. It is
# TestEveryNoticeLivesInACard in test_dashboard.py, which walks the templates
# instead of the palette -- because the defect was never a colour, it was an
# element in the wrong place.

# Deliberately NOT here: a control's edge against its OWN fill. It was added
# and then removed -- --control-line on --inset is 2.69:1 in light, and the
# instinct was to darken the border until it passed. It is not a requirement.
# 1.4.11 asks that a component be distinguishable from what is ADJACENT and
# outside it; the border and the fill together are the control, and nothing is
# lost when they sit close. Pairs listed here should be things a reader has to
# tell apart, not every two colours that touch.
#
# THE SWITCH HAS TO CLEAR 3:1 TWICE, in opposite directions.
#
# The track is a component and must be distinguishable from the card behind
# it. The knob is what states on or off, so it must be distinguishable from
# the track it sits on. One shape, two requirements, and satisfying either one
# alone is easy -- which is exactly how a switch ends up looking fine and
# being unreadable. Both are listed so neither can be traded for the other.
UI_PAIRS = [
    ("control-line", "panel", "a control's edge against the card"),
    # The bell's unread dot (#136). A shape, not text, so 3:1 -- and it has to
    # clear it against BOTH surfaces the button wears, because the button goes
    # to --hover while its panel is open and on pointer hover. A dot that is
    # legible at rest and vanishes the moment you reach for it is the one
    # failure a notification indicator cannot have.
    ("accent-text", "chrome", "the bell's unread dot at rest"),
    # The same pairing carries a second shape found while auditing the pair
    # list for #159: `.menu-tick`, the mark beside the current row in the
    # language and theme menus, which sits on --hover whenever that row is
    # under the pointer. 4.58:1 light, 4.30:1 dark -- clear of the 3:1 a glyph
    # needs and under the 4.5:1 it would need if it were ever words.
    ("accent-text", "hover", "the bell's dot, and a menu's tick, on a hovered row"),
    ("switch-knob", "control-line", "the knob on an off switch"),
    ("switch-on", "panel", "an on switch against the card"),
    ("switch-knob", "switch-on", "the knob on an on switch"),
]


def _blocks(css: str, pattern: str) -> dict:
    """The custom properties declared in the first block matching `pattern`."""
    match = re.search(pattern, css)
    assert match, f"no block matching {pattern!r}"
    index = css.index("{", match.start()) + 1
    depth, start = 1, index
    while depth:
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
        index += 1
    body = css[start : index - 1]
    return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


def _palettes():
    css = re.sub(r"/\*.*?\*/", "", STYLE.read_text(), flags=re.S)
    root = _blocks(css, r"(?m)^:root\s*\{")
    light = {k[2:]: v for k, v in _blocks(css, r"(?m)^:root,").items()}
    # The dark values are named once as --dark-*; both dark selectors point at
    # them. Reading them here rather than from either selector is what keeps
    # this honest if a third dark context is ever added.
    overrides = {
        k[len("--dark-") :]: v for k, v in root.items() if k.startswith("--dark-")
    }
    # Layered over the light values rather than standing alone, because that is
    # what the cascade actually does: a token the dark blocks do not mention
    # keeps the value :root gave it. --switch-knob is white in both themes and
    # is deliberately declared once, so reading the dark palette as only the
    # --dark-* names would have left it undefined here and unchecked on dark.
    dark = {**light, **overrides}
    return {"light": light, "dark": dark}


def _relative_luminance(value: str) -> float:
    value = value.lstrip("#")
    assert re.fullmatch(r"[0-9a-fA-F]{6}", value), f"not a plain hex colour: {value}"
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(foreground: str, background: str) -> float:
    a, b = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


class TestContrast:
    @pytest.mark.parametrize("theme", ["light", "dark"])
    @pytest.mark.parametrize("fg,bg,described", PAIRS)
    def test_every_rendered_pair_clears_aa(self, theme, fg, bg, described):
        palette = _palettes()[theme]
        assert fg in palette, f"--{fg} is not defined in the {theme} palette"
        assert bg in palette, f"--{bg} is not defined in the {theme} palette"
        ratio = contrast(palette[fg], palette[bg])
        assert ratio >= AA_TEXT, (
            f"{theme}: {described} is {ratio:.2f}:1 "
            f"(--{fg} {palette[fg]} on --{bg} {palette[bg]}), below {AA_TEXT}:1"
        )

    @pytest.mark.parametrize("theme", ["light", "dark"])
    @pytest.mark.parametrize("fg,bg,described", UI_PAIRS)
    def test_every_control_boundary_clears_the_ui_floor(
        self, theme, fg, bg, described
    ):
        palette = _palettes()[theme]
        assert fg in palette, f"--{fg} is not defined in the {theme} palette"
        assert bg in palette, f"--{bg} is not defined in the {theme} palette"
        ratio = contrast(palette[fg], palette[bg])
        assert ratio >= AA_UI, (
            f"{theme}: {described} is {ratio:.2f}:1 "
            f"(--{fg} {palette[fg]} on --{bg} {palette[bg]}), below {AA_UI}:1"
        )

    def test_neither_existing_blurple_could_have_been_the_switch_on_dark(self):
        """Why --switch-on exists rather than reusing something.

        On dark the on-state is squeezed from both sides at once: --accent is
        too dark to read against the card, --accent-text is too light to read
        against the white knob. On light both comparisons are against white, so
        they collapse into one number and --switch-on is plain --accent.

        If this ever starts passing for one of the two, the extra token should
        go rather than linger as a third blurple nobody can justify.
        """
        dark = _palettes()["dark"]
        assert contrast(dark["accent"], dark["panel"]) < AA_UI
        assert contrast(dark["accent-text"], dark["switch-knob"]) < AA_UI

        light = _palettes()["light"]
        assert light["switch-on"] == light["accent"], (
            "on light the two constraints are the same comparison, so the "
            "switch should not have diverged from the accent here"
        )

    def test_the_two_accents_are_different_on_dark_and_must_stay_so(self):
        """The finding that made --accent-text necessary.

        On a dark card one blurple cannot be both a button background with
        white on it and a readable foreground -- the two requirements move in
        opposite directions. If these ever collapse back to one value, one of
        those two jobs is silently failing.
        """
        dark = _palettes()["dark"]
        assert dark["accent"] != dark["accent-text"]
        assert contrast(dark["accent-ink"], dark["accent"]) >= AA_TEXT
        assert contrast(dark["accent-text"], dark["panel"]) >= AA_TEXT

    def test_the_checker_agrees_with_known_values(self):
        """Guards the maths, so a broken formula cannot make everything pass."""
        assert contrast("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
        assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)
        assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.02)
