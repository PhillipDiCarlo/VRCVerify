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
    ("danger", "panel", "an error an admin has to act on"),
    ("notice", "notice-bg", "a warning in its own box"),
]

# THE SWITCH HAS TO CLEAR 3:1 TWICE, in opposite directions.
#
# The track is a component and must be distinguishable from the card behind
# it. The knob is what states on or off, so it must be distinguishable from
# the track it sits on. One shape, two requirements, and satisfying either one
# alone is easy -- which is exactly how a switch ends up looking fine and
# being unreadable. Both are listed so neither can be traded for the other.
UI_PAIRS = [
    ("switch-off", "panel", "an off switch against the card"),
    ("switch-knob", "switch-off", "the knob on an off switch"),
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
