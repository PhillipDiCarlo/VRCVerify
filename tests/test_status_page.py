"""status.vrcverify.com (issue #170), and the three things it must not do.

The status page is a separate Worker, in JavaScript, deployed on its own. None
of that is reachable from pytest, and most of it is covered by
`node --test status/test` instead. What is covered HERE is the part that spans
files and would otherwise be checked by remembering:

  1. Its stylesheet is a COPY of the apex site's, which is itself a copy of
     the dashboard's. Copies drift, and surfaces that drift look like separate
     products. All three sets of token values are pinned against each other
     here -- see TestTheDashboardCopy for why the third comparison needs a
     name mapping the first one does not.
  2. Its status colors are new to this project, and no one has ever drawn a
     red here before. They are measured on every surface they land on, because
     `--ok` has already had to be moved twice for exactly that omission.
  3. Its public copy names capabilities and never infrastructure. That is
     decision 3 on the issue and the reason the dashboard holds no database
     credential at all; a page helpfully listing the estate would give back
     what SECURITY_AUDIT section 2 spends the whole design protecting.

The Node suite asserts the same rule about the RENDERED page. This asserts it
about the SOURCE of the words, so the rule still has a guard on a machine with
no Node installed -- which is the machine this was written on.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from test_contrast import contrast

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE_CSS = ROOT / "site" / "style.css"
STATUS_CSS = ROOT / "status" / "public" / "style.css"
SITE_THEME = ROOT / "site" / "theme.js"
STATUS_THEME = ROOT / "status" / "public" / "theme.js"
DASHBOARD_CSS = ROOT / "src" / "dashboard" / "static" / "style.css"
CONFIG_JS = ROOT / "status" / "src" / "config.js"
WRANGLER = ROOT / "status" / "wrangler.toml"


def _tokens(path: pathlib.Path) -> dict[str, str]:
    """Every `--name: #hex;` declaration in a file, as a flat mapping.

    Flat on purpose: a token declared twice with two different literals is the
    drift this test exists to catch, so the last one wins and the comparison
    below fails rather than silently reading the first.
    """
    css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", css)
    }


class TestTheCopiedStylesheet:
    def test_every_shared_token_holds_the_same_value(self):
        """The status page and the apex site are one product or they are two."""
        site = _tokens(SITE_CSS)
        status = _tokens(STATUS_CSS)
        shared = sorted(set(site) & set(status))
        assert len(shared) > 20, "the copy looks nothing like its source any more"
        drifted = {name: (site[name], status[name]) for name in shared if site[name] != status[name]}
        assert not drifted, f"tokens have drifted from site/style.css: {drifted}"

    def test_each_file_says_where_the_other_copies_live(self):
        """The comments are the only mechanism keeping three files in step."""
        assert "status/public/style.css" in SITE_CSS.read_text(encoding="utf-8"), (
            "site/style.css does not mention the status page's copy of its tokens"
        )
        status = STATUS_CSS.read_text(encoding="utf-8")
        assert "site/style.css" in status
        assert "src/dashboard/static/style.css" in status

    def test_the_mark_is_the_same_drawing_everywhere(self):
        """One logo, three surfaces, three copies of the path that draws it.

        THE DUPLICATION IS THE SAME BARGAIN THE TOKENS MAKE, for the same
        reason: each surface has to keep working when the others are down, so
        none of them may fetch the mark from another. The apex is an
        assets-only Worker, the status page is a Worker rendering strings, the
        dashboard is Flask -- there is no shared build step and #243 says a new
        one must not put a runtime dependency on anything external.

        So the mark is pasted, and this is what stops the three from becoming
        three logos. It compares the `d` attribute itself rather than the file
        around it, because the wrapper differs by necessity: Jinja on one, six
        static HTML files on another, a JavaScript template string on the
        third. The drawing is the part that has to agree.
        """
        pattern = re.compile(r'fill-rule="evenodd" d="([^"]+)"')
        copies = {}
        for label, path in (
            ("dashboard", ROOT / "src" / "dashboard" / "templates" / "_mark.html"),
            ("status", ROOT / "status" / "src" / "render.js"),
            ("apex", ROOT / "site" / "index.html"),
        ):
            found = pattern.findall(path.read_text(encoding="utf-8"))
            assert found, f"{label} has no mark path in {path.name}"
            copies[label] = found[0]

        assert len(set(copies.values())) == 1, (
            "the mark has drifted between surfaces: "
            + ", ".join(f"{k} {len(v)} chars" for k, v in copies.items())
        )

    def test_every_apex_page_carries_the_same_mark(self):
        """The header is byte-identical across the six pages and the mark is
        part of it. test_site.py compares the whole header; this one names the
        mark specifically, so a failure says which thing broke."""
        pattern = re.compile(r'fill-rule="evenodd" d="([^"]+)"')
        marks = {}
        for page in sorted((ROOT / "site").glob("*.html")):
            found = pattern.findall(page.read_text(encoding="utf-8"))
            assert found, f"{page.name} has no mark in its header"
            marks[page.name] = found[0]
        assert len(set(marks.values())) == 1, f"apex pages disagree: {sorted(marks)}"

    def test_the_mark_is_one_colour_and_takes_it_from_the_stylesheet(self):
        """`currentColor` is the whole reason this mark needs no second token.

        A hex typed into the path would be a fourth copy of a colour, on the
        one element that appears on every page of all three surfaces, and it
        would be invisible to the theme -- which is exactly the trap the PNG
        it replaces fell into and answered with `invert(1)`.
        """
        for path in (
            ROOT / "src" / "dashboard" / "templates" / "_mark.html",
            ROOT / "status" / "src" / "render.js",
            *sorted((ROOT / "site").glob("*.html")),
        ):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r'<path fill="([^"]+)" fill-rule="evenodd"', text):
                assert match.group(1) == "currentColor", (
                    f"{path.name} paints the mark {match.group(1)!r} rather than "
                    "taking it from the stylesheet"
                )

    def test_the_theme_toggle_is_the_same_script(self):
        """Byte for byte below its header, so the two sites cannot toggle differently."""
        site = SITE_THEME.read_text(encoding="utf-8")
        status = STATUS_THEME.read_text(encoding="utf-8")
        body = site[site.index("(function ()") :]
        assert status.endswith(body), "status/public/theme.js has diverged from site/theme.js"

    def test_the_font_is_served_from_this_origin(self):
        """A page built to survive the apex site being down cannot fetch from it."""
        assert (ROOT / "status" / "public" / "fonts" / "inter-latin-var.woff2").exists()
        assert "fonts.googleapis" not in STATUS_CSS.read_text(encoding="utf-8")


# The third copy, and the one nothing was comparing (#284).
#
# TestTheCopiedStylesheet above pins status against the apex site by name,
# which works because those two files are byte-level siblings. The dashboard is
# the ORIGIN of both and was never compared to either, so its palette could
# have drifted from theirs indefinitely -- which is precisely the failure the
# comment blocks in all three files exist to prevent, left with no test behind
# it. #284 moves the accent in all three at once and is exactly the change that
# gap would have let land half-done.
#
# It needs a mapping because the two files name their themes differently, for
# reasons written up in each. The dashboard renders light from bare `:root` and
# names its dark values `--dark-*`; the apex site is the mirror, because it has
# no server to stamp a theme into the first paint and so must have dark on bare
# `:root`. So "the dark value" lives under a prefixed name in one file and an
# unprefixed one in the other, and vice versa.


def _themed(path: pathlib.Path, prefix: str) -> tuple[dict[str, str], dict[str, str]]:
    """A file's tokens split into (prefixed, unprefixed), prefix stripped."""
    tokens = _tokens(path)
    marked = {k[len(prefix) :]: v for k, v in tokens.items() if k.startswith(prefix)}
    plain = {k[2:]: v for k, v in tokens.items() if not k.startswith(prefix)}
    return marked, plain


# Deliberate divergences, each argued for in a comment beside the value.
#
# Listed rather than tolerated: an entry here is a claim that somebody measured
# the two surfaces and decided they differ, and the test names the file where
# that argument lives so the next person can check it rather than assume it.
DIVERGENT = {
    "light": {
        # site/style.css: "THE ONE VALUE THAT DELIBERATELY DIFFERS FROM THE
        # DASHBOARD" -- the dashboard's inputs sit on white cards, this site's
        # only control sits on the darker --chrome, and the same gray is 2.84:1
        # there.
        "control-line": "the surface the control sits on differs",
        # status/public/style.css: the dashboard never draws a status color on
        # the page ground; the status page's hero glyph sits directly on it, and
        # the dashboard's green is 4.27:1 there.
        "ok": "the status page draws --ok on --bg and the dashboard never does",
    },
    "dark": {},
}


class TestTheDashboardCopy:
    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_the_apex_palette_matches_the_dashboard_it_was_copied_from(self, theme):
        light_site, dark_site = _themed(SITE_CSS, "--light-")
        dark_dash, light_dash = _themed(DASHBOARD_CSS, "--dark-")
        site = {"dark": dark_site, "light": light_site}[theme]
        dashboard = {"dark": dark_dash, "light": light_dash}[theme]

        shared = sorted(set(site) & set(dashboard) - set(DIVERGENT[theme]))
        assert len(shared) > 12, (
            f"only {len(shared)} {theme} tokens are named the same in both "
            "files -- the copy has stopped resembling its origin"
        )
        drifted = {
            name: (site[name], dashboard[name])
            for name in shared
            if site[name] != dashboard[name]
        }
        assert not drifted, (
            f"{theme} tokens have drifted between site/style.css and the "
            f"dashboard: {drifted}. Either fix one, or add it to DIVERGENT "
            "with the measurement that says the two surfaces really differ."
        )

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_every_listed_divergence_is_still_a_divergence(self, theme):
        """A tolerated difference that has gone away should stop being tolerated."""
        light_site, dark_site = _themed(SITE_CSS, "--light-")
        dark_dash, light_dash = _themed(DASHBOARD_CSS, "--dark-")
        site = {"dark": dark_site, "light": light_site}[theme]
        dashboard = {"dark": dark_dash, "light": light_dash}[theme]

        for name, why in DIVERGENT[theme].items():
            assert name in site and name in dashboard, (
                f"--{name} is listed as a deliberate {theme} divergence but is "
                "no longer declared in both files"
            )
            assert site[name] != dashboard[name], (
                f"--{name} is listed as diverging ({why}) and the two files now "
                f"agree at {site[name]}. Drop it from DIVERGENT."
            )

    def test_the_accent_is_the_same_hue_in_all_three_files(self):
        """#284: the accent lands everywhere or it lands nowhere.

        The name-mapped comparison above already covers this. It is asserted
        again by hand because the accent is the one token the epic moves, and a
        failure here should say "the accent" rather than arrive as one line in a
        dict of twenty.
        """
        for theme, prefix in (("dark", ""), ("light", "--light-")):
            site = _tokens(SITE_CSS)
            status = _tokens(STATUS_CSS)
            dashboard = _tokens(DASHBOARD_CSS)
            key = f"{prefix}accent" if prefix else "--accent"
            dash_key = "--dark-accent" if theme == "dark" else "--accent"
            values = {
                "site": site[key],
                "status": status[key],
                "dashboard": dashboard[dash_key],
            }
            assert len(set(values.values())) == 1, (
                f"the {theme} accent differs between surfaces: {values}"
            )


# The surfaces a status color is ACTUALLY drawn on, and only those.
#
# `--bg` is the page ground, where the hero glyph sits. `--panel` is the card,
# where every row's glyph and pill sit. `--chrome` is deliberately absent: the
# header and footer carry no status color, and asserting that pair would be
# inventing a requirement to satisfy a requirement -- which is what
# test_contrast.py's docstring says this suite will not do.
#
# Both surfaces, every color, every time. `--ok` has now been moved three
# times in this project by measuring against one surface and then drawing on
# another, and the fourth was caught by this test on the day it was written.
SURFACES = ("bg", "panel")


class TestStatusColors:
    @pytest.mark.parametrize("token", ["ok", "notice", "down", "planned"])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_dark_clears_aa(self, token, surface):
        palette = _tokens(STATUS_CSS)
        ratio = contrast(palette[f"--{token}"], palette[f"--{surface}"])
        assert ratio >= 4.5, f"--{token} on --{surface} is {ratio:.2f}:1 on dark"

    @pytest.mark.parametrize("token", ["ok", "notice", "down", "planned"])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_light_clears_aa(self, token, surface):
        palette = _tokens(STATUS_CSS)
        ratio = contrast(palette[f"--light-{token}"], palette[f"--light-{surface}"])
        assert ratio >= 4.5, f"--light-{token} on --light-{surface} is {ratio:.2f}:1 on light"

    @pytest.mark.parametrize("theme", ["", "light-"])
    def test_the_bar_fills_clear_the_graphical_floor(self, theme):
        """A filled bar is a graphical object: WCAG 1.4.11 asks 3:1, not 4.5:1.

        The history strip is the one place this project draws a state as a
        shape rather than as a word, and the foreground red was too pale to
        work there -- it made the worst day of the quarter look gentler than a
        wobble. The fill is measured against the card it sits on, at the floor
        that actually applies to it.
        """
        palette = _tokens(STATUS_CSS)
        surface = palette[f"--{theme}panel"]
        for token in (
            f"--{theme}down-fill",
            f"--{theme}ok",
            f"--{theme}notice",
            f"--{theme}planned",
        ):
            ratio = contrast(palette[token], surface)
            assert ratio >= 3.0, f"{token} fills at {ratio:.2f}:1 on the card"

    def test_the_no_data_bar_is_visible_rather_than_a_gap(self):
        """The page's first eighty-nine days are entirely made of these.

        A no-data bar that fades into the card would draw "we did not exist
        yet" as health. --control-line is this project's token for an edge that
        IS the element, at the 3:1 that asks for.
        """
        palette = _tokens(STATUS_CSS)
        for prefix, surface in (("--", "--panel"), ("--light-", "--light-panel")):
            ratio = contrast(palette[f"{prefix}control-line"], palette[surface])
            assert ratio >= 3.0

    def test_discords_own_red_is_still_not_good_enough(self):
        """Pinned so the obvious candidate is not quietly adopted later.

        #ed4245 is the red every Discord-adjacent product reaches for, and on
        the dark card every status row sits on it is 3.29:1. This is here so
        that swapping it in fails with a number instead of looking right.
        """
        palette = _tokens(STATUS_CSS)
        assert contrast("#ed4245", palette["--panel"]) < 4.5
        assert palette["--down"] != "#ed4245", "the foreground red must not be Discord's"

    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_unknown_state_is_legible_too(self, surface):
        """`--faint` is the fourth status color, and the easiest to forget.

        It is the one a reader sees when the checker itself is broken, which is
        the moment the page most needs to be readable. It is pinned on the page
        ground as well as the card, because the hero glyph goes gray in exactly
        that case.
        """
        palette = _tokens(STATUS_CSS)
        dark = contrast(palette["--faint"], palette[f"--{surface}"])
        light = contrast(palette["--light-faint"], palette[f"--light-{surface}"])
        assert dark >= 4.5, f"--faint on --{surface} is {dark:.2f}:1 on dark"
        assert light >= 4.5, f"--light-faint on --light-{surface} is {light:.2f}:1 on light"

    def test_no_state_is_told_apart_by_color_alone(self):
        """Each state ships a word and a drawn glyph as well as a color."""
        render = (ROOT / "status" / "src" / "render.js").read_text(encoding="utf-8")
        for label in ("Operational", "Degraded", "Down", "Unknown"):
            assert f'"{label}"' in render
        assert render.count("GLYPHS") >= 2


# Words that name the estate rather than the service. The dashboard holds no
# database credential and no bot token precisely so that a compromise of the
# public box yields no route to the users table; a public page enumerating the
# parts would hand back the map for free.
FORBIDDEN = (
    "postgres",
    "rabbit",
    "tailscale",
    "tailnet",
    "mtls",
    "homelab",
    "docker",
    "container",
    "queue",
    "tunnel",
    "healthz",
    "cloudflared",
)


class TestPublicCopy:
    def _public_strings(self) -> list[str]:
        """Only the fields that become words on the page.

        Scanning the whole file would fail on its own comments, which discuss
        the queue and the homelab at length and should -- the comments are how
        the next person learns why the rule exists.
        """
        source = CONFIG_JS.read_text(encoding="utf-8")
        return re.findall(r'(?:name|description|why):\s*"([^"]+)"', source)

    def test_the_public_copy_names_capabilities_not_infrastructure(self):
        found = [
            (word, text)
            for text in self._public_strings()
            for word in FORBIDDEN
            if word in text.lower()
        ]
        assert not found, f"public copy names infrastructure: {found}"

    def test_all_five_capabilities_and_four_dependencies_are_described(self):
        strings = self._public_strings()
        for name in ("Verification", "Discord bot", "Group invites", "Website"):
            assert name in strings, f"{name} is not a row on the page"
        for name in ("Discord", "VRChat", "Stripe", "Cloudflare"):
            assert name in strings

    def test_gmail_appears_nowhere(self):
        """Issue #170 decided it is listed nowhere and alerts nowhere.

        It is a real runtime dependency -- the checker reads VRChat's 2FA codes
        out of it -- so the absence is a decision and not an oversight, and this
        is where the decision is enforced rather than remembered.
        """
        for text in self._public_strings():
            assert "gmail" not in text.lower()


class TestTheBuildScriptsKnowAboutEveryImage:
    """A Dockerfile nothing builds is a service nobody can deploy.

    Written after adding the status reporter to both scripts and leaving the
    PowerShell one with two branches numbered "5": the second was unreachable,
    so "All" would have published four images and silently skipped the fifth.
    Nothing about that is visible in a diff, and the failure appears on a deploy
    host as an image tag that does not exist.
    """

    SCRIPTS = ("tag_and_push_images.sh", "tag_and_push_images.ps1")

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_every_dockerfile_is_reachable_from_the_script(self, script):
        text = (ROOT / script).read_text(encoding="utf-8")
        for dockerfile in sorted((ROOT / "docker").glob("Dockerfile-*")):
            assert f"docker/{dockerfile.name}" in text, f"{script} cannot build {dockerfile.name}"

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_the_all_option_builds_all_of_them(self, script):
        """"All" is the option that gets used, so it is the one that must be complete."""
        text = (ROOT / script).read_text(encoding="utf-8")
        images = re.findall(r'(?:build_and_push|Publish-Image) "([a-z-]+)"', text)
        every = {name for name in images}
        # The last block in each script is the "all" branch; every image named
        # anywhere in the script has to appear in it.
        tail = text[text.rindex("status-reporter") - 2000 :]
        for image in sorted(every):
            assert image in tail, f"{script}: '{image}' is missing from the all-images branch"

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_no_menu_number_is_used_twice(self, script):
        text = (ROOT / script).read_text(encoding="utf-8")
        chosen = re.findall(r'(?m)^\s*"?(\d)"?[\)]?\s*[\){]', text)
        assert len(chosen) == len(set(chosen)), f"{script} reuses a menu number: {chosen}"


class TestItIsItsOwnDeploy:
    def test_the_status_worker_is_not_the_apex_worker(self):
        """The whole argument for this page is the failures it does not share."""
        apex = (ROOT / "wrangler.toml").read_text(encoding="utf-8")
        status = WRANGLER.read_text(encoding="utf-8")
        apex_name = re.search(r'(?m)^name\s*=\s*"([^"]+)"', apex).group(1)
        status_name = re.search(r'(?m)^name\s*=\s*"([^"]+)"', status).group(1)
        assert apex_name != status_name
        assert 'directory = "./site"' in apex
        assert 'directory = "./public"' in status

    def test_the_cron_runs_every_minute(self):
        """The observation interval IS the resolution of every number on the page."""
        assert 'crons = ["* * * * *"]' in WRANGLER.read_text(encoding="utf-8")

    def test_the_apex_worker_still_serves_only_the_apex(self):
        """#170 must not have quietly bound status.vrcverify.com to the site."""
        apex = (ROOT / "wrangler.toml").read_text(encoding="utf-8")
        assert "status.vrcverify.com" not in apex
