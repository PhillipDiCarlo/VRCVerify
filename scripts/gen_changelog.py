"""Generate site/changelog.html from the changelog constant (issue #137 phase 4).

    python scripts/gen_changelog.py          # rewrite site/changelog.html
    python scripts/gen_changelog.py --check  # exit 1 if it is out of date

WHY GENERATE RATHER THAN WRITE
------------------------------
Four surfaces render the same entries -- the in-app feed (#136), this page,
the announcement channel (#138) and the update emails (#139) -- and
`changelog.py` is the source of truth for all of them. A hand-maintained
public list would be a fifth copy, and the one strangers read, which is the
worst one to have disagree.

WHY A COMMITTED FILE RATHER THAN A ROUTE
----------------------------------------
The apex site is an assets-only Cloudflare Worker with no code on the request
path, deliberately: it is a separate failure domain from the dashboard's VPS,
so the legal pages resolve when the dashboard does not. Generating at release
time and committing the output keeps that property. The page is live even when
the dashboard is not.

The cost is that the committed file can go stale, because nothing regenerates
it on push -- `.github/workflows/` holds CodeQL and nothing else. That is what
`--check` is for, and what the drift test in tests/test_site.py runs. An entry
added to the constant without regenerating fails CI rather than shipping a
public page that is quietly behind the product.

WHY THE CHROME IS READ FROM ANOTHER PAGE
-----------------------------------------
The header and footer are copied verbatim out of an existing page rather than
written here. `tests/test_site.py` requires them byte-identical across every
page, and a third hand-written copy in a generator is precisely the drift that
test exists to catch. Read them, do not retype them.
"""

from __future__ import annotations

import argparse
import html
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SITE = REPO / "site"

# The VRCVerify Discord, so a reader of the public changelog can have these
# updates crossposted into their own server (#138).
#
# HARDCODED HERE, unlike the bot and the dashboard, which both read
# SUPPORT_INVITE_URL from the environment. This site is static files behind a
# CDN: no server renders them, so there is no environment to read at the
# moment anyone loads the page. Injecting it at generation time would mean the
# committed HTML depended on whose shell ran the script, and the drift test
# regenerates in memory and compares -- so the value has to be the same for
# everyone. A constant in the generator is that.
#
# It is a NON-EXPIRING invite. An expired link on a public page is a quiet,
# embarrassing failure, and nothing here would notice.
#
# THIS VALUE LIVES IN THREE PLACES -- here, the bot host's .env, and the VPS's
# dashboard.env. Nothing detects a miss, and this is the copy that gets
# forgotten: it is code rather than configuration, it needs a regeneration
# afterwards, and it is the PUBLIC page, so a stale link here is broken for
# people who have never signed in and cannot tell you. See "Rotating the
# invite" in site/README.md.
SUPPORT_INVITE_URL = "https://discord.gg/Vus4qxA52Q"

# The Buttondown account the signup form posts to (#139).
#
# THE CAPITAL I IS LOAD-BEARING. Measured 2026-08-28:
#
#   GET buttondown.com/Italiandogs   -> 200
#   GET buttondown.com/italiandogs   -> 302 to /Italiandogs
#
# so the slug is case-sensitive and lowercase is a redirect. That is the same
# shape as the host note below, and the same shape as the bug it points at: a
# redirect on a form POST is how "Subscribe does nothing" happened on the
# dashboard. Copy this string exactly rather than retyping it.
#
# tests/test_site.py refuses to let this disagree with the copy in index.html,
# which is the drift this would otherwise develop -- two hand-edited copies of
# one account name -- and refuses to let it go back to being a placeholder.
#
# Hardcoded for the same reason SUPPORT_INVITE_URL above is: these are static
# files behind a CDN and nothing renders them at request time.
#
# Canonical host is buttondown.com. buttondown.email still answers but 301s
# there, and a redirect on a form POST is the exact shape that produced the
# "Subscribe does nothing" bug on the dashboard -- see the form-action comment
# in src/dashboard/app.py. Nothing here has a CSP to refuse it, but pointing at
# the host that actually serves is free.
BUTTONDOWN_USERNAME = "Italiandogs"
SUBSCRIBE_ACTION = (
    f"https://buttondown.com/api/emails/embed-subscribe/{BUTTONDOWN_USERNAME}"
)
OUTPUT = SITE / "changelog.html"
# The page the chrome is lifted from. Any page would do -- the suite pins them
# equal -- so this names one rather than picking one at random each run.
CHROME_SOURCE = SITE / "terms.html"

sys.path.insert(0, str(REPO / "src"))

from dashboard import changelog  # noqa: E402


def _chrome(source: pathlib.Path = CHROME_SOURCE) -> tuple:
    """The shared header and footer, verbatim, from a page that already has them."""
    text = source.read_text(encoding="utf-8")
    header = re.search(r'<header class="site">.*?</header>', text, re.S)
    footer = re.search(r'<footer class="site">.*?</footer>', text, re.S)
    if not header or not footer:
        raise SystemExit(f"{source.name} has no site header/footer to copy")
    return header.group(0), footer.group(0)


def _entry_html(entry) -> str:
    """One entry, as a list item.

    Bodies are plain text by contract -- `changelog.py` forbids markup in them
    and `validate_entries` fails the build on any -- so everything is escaped
    on the way out. The dashboard gets that from Jinja; here it is explicit,
    because this file is the only place on the apex site where text from
    somewhere else becomes HTML.
    """
    return (
        '    <li class="entry">\n'
        '      <p class="entry-meta">\n'
        f'        <span class="tag{" premium" if entry.premium else ""}">'
        f"{html.escape(entry.tag)}</span>\n"
        f'        <time datetime="{entry.date.isoformat()}">'
        f"{html.escape(entry.display_date)}</time>\n"
        "      </p>\n"
        f"      <h2>{html.escape(entry.title)}</h2>\n"
        f'      <p class="entry-body">{html.escape(entry.body)}</p>\n'
        "    </li>"
    )


def render(entries=None) -> str:
    """The whole page, as a string. Pure, so the drift test can call it."""
    if entries is None:
        entries = changelog.public_entries()
    header, footer = _chrome()
    items = "\n".join(_entry_html(entry) for entry in entries)
    invite = SUPPORT_INVITE_URL
    subscribe = SUBSCRIBE_ACTION
    if not items:
        # Not expected, and not a crash either. A page that renders nothing is
        # better than a deploy that fails at the moment somebody empties the
        # constant to fix something.
        items = '    <li class="entry empty">Nothing yet.</li>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What&#39;s new &mdash; VRCVerify</title>
<meta name="description" content="Everything that has shipped in VRCVerify, newest first.">
<link rel="stylesheet" href="/style.css">
<!-- Blocking, and in <head>, so a stored Light choice paints light on the
     first frame rather than flashing dark and correcting itself. Same
     origin, ~30 lines, and nothing on this page needs it to work. -->
<script src="/theme.js"></script>
</head>
<!-- GENERATED BY scripts/gen_changelog.py -- DO NOT EDIT THIS FILE.
     It is rendered from ENTRIES in src/dashboard/changelog.py, filtered
     through public_entries(). Editing it here would be overwritten by the
     next run, and tests/test_site.py fails when the committed file and the
     constant disagree. Change the constant and re-run the script. -->
<body class="changelog-page">

{header}

<main class="wrap">

  <h1>What&#39;s new</h1>

  <p class="lede">Everything that has shipped in VRCVerify, newest first.</p>

  <ol class="entries">
{items}
  </ol>

  <p class="entry-follow">
    Want these in your own server?
    <a href="{invite}" target="_blank" rel="noopener noreferrer">Join the VRCVerify Discord</a>
    and follow the announcements channel &mdash; every update here gets posted
    there, and Discord crossposts it to any channel you pick.
  </p>

  <h2 id="updates">Get them by email</h2>

  <form class="subscribe" method="post" action="{subscribe}">
    <p class="subscribe-blurb">The same updates, a few times a year. No roadmap,
    no marketing, and nothing else.</p>
    <div class="subscribe-row">
      <label class="visually-hidden" for="bd-email">Email address</label>
      <input class="subscribe-input" id="bd-email" type="email" name="email"
             required autocomplete="email" placeholder="you@example.com">
      <button class="cta" type="submit">Subscribe</button>
    </div>
    <p class="subscribe-consent">By subscribing you agree to receive product
    update emails from VRCVerify. Unsubscribe any time with the link in any
    email. Your address is stored by Buttondown for this alone and is never
    linked to your verification record. See the
    <a href="/privacy#what-we-collect">Privacy Policy</a>.</p>
  </form>

</main>

{footer}

</body>
</html>
"""


def _display(path: pathlib.Path):
    """A repo-relative path where that means anything, the full path otherwise.

    `relative_to` raises for a path outside the repo, which is every path in a
    test using tmp_path -- and a reporting helper should not be the thing that
    crashes the run.
    """
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed page is out of date, and write nothing",
    )
    args = parser.parse_args(argv)

    fresh = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == fresh:
            print(f"{_display(OUTPUT)} is up to date")
            return 0
        print(
            f"{_display(OUTPUT)} is out of date.\n"
            "Run: python scripts/gen_changelog.py",
            file=sys.stderr,
        )
        return 1

    # newline="\n" rather than the platform default. Every other file in site/
    # is LF, .gitattributes has no *.html rule, and this repo carries PowerShell
    # scripts -- so a run on Windows would otherwise commit the one CRLF page in
    # the directory. It would fail loudly rather than silently (the drift test
    # and the identical-footer test both break), but failing not at all is
    # better.
    OUTPUT.write_text(fresh, encoding="utf-8", newline="\n")
    total = len(changelog.ENTRIES)
    shown = len(changelog.public_entries())
    print(
        f"wrote {_display(OUTPUT)} "
        f"({shown} public {'entry' if shown == 1 else 'entries'}"
        + (f", {total - shown} withheld" if total != shown else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
