"""status.vrcverify.com (issue #170), and the three things it must not do.

The status page is a separate Worker, in JavaScript, deployed on its own. None
of that is reachable from pytest, and most of it is covered by
`node --test status/test` instead. What is covered HERE is the part that spans
files and would otherwise be checked by remembering:

  1. Its stylesheet is a COPY of the apex site's. Copies drift, and two
     surfaces that drift look like two products. The token values are pinned
     against their source.
  2. Its status colours are new to this project, and no one has ever drawn a
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


# The surfaces a status colour is ACTUALLY drawn on, and only those.
#
# `--bg` is the page ground, where the hero glyph sits. `--panel` is the card,
# where every row's glyph and pill sit. `--chrome` is deliberately absent: the
# header and footer carry no status colour, and asserting that pair would be
# inventing a requirement to satisfy a requirement -- which is what
# test_contrast.py's docstring says this suite will not do.
#
# Both surfaces, every colour, every time. `--ok` has now been moved three
# times in this project by measuring against one surface and then drawing on
# another, and the fourth was caught by this test on the day it was written.
SURFACES = ("bg", "panel")


class TestStatusColours:
    @pytest.mark.parametrize("token", ["ok", "notice", "down"])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_dark_clears_aa(self, token, surface):
        palette = _tokens(STATUS_CSS)
        ratio = contrast(palette[f"--{token}"], palette[f"--{surface}"])
        assert ratio >= 4.5, f"--{token} on --{surface} is {ratio:.2f}:1 on dark"

    @pytest.mark.parametrize("token", ["ok", "notice", "down"])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_light_clears_aa(self, token, surface):
        palette = _tokens(STATUS_CSS)
        ratio = contrast(palette[f"--light-{token}"], palette[f"--light-{surface}"])
        assert ratio >= 4.5, f"--light-{token} on --light-{surface} is {ratio:.2f}:1 on light"

    def test_discords_own_red_is_still_not_good_enough(self):
        """Pinned so the obvious candidate is not quietly adopted later.

        #ed4245 is the red every Discord-adjacent product reaches for, and on
        the dark card every status row sits on it is 3.29:1. This is here so
        that swapping it in fails with a number instead of looking right.
        """
        palette = _tokens(STATUS_CSS)
        assert contrast("#ed4245", palette["--panel"]) < 4.5

    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_unknown_state_is_legible_too(self, surface):
        """`--faint` is the fourth status colour, and the easiest to forget.

        It is the one a reader sees when the checker itself is broken, which is
        the moment the page most needs to be readable. It is pinned on the page
        ground as well as the card, because the hero glyph goes grey in exactly
        that case.
        """
        palette = _tokens(STATUS_CSS)
        dark = contrast(palette["--faint"], palette[f"--{surface}"])
        light = contrast(palette["--light-faint"], palette[f"--light-{surface}"])
        assert dark >= 4.5, f"--faint on --{surface} is {dark:.2f}:1 on dark"
        assert light >= 4.5, f"--light-faint on --light-{surface} is {light:.2f}:1 on light"

    def test_no_state_is_told_apart_by_colour_alone(self):
        """Each state ships a word and a drawn glyph as well as a colour."""
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
