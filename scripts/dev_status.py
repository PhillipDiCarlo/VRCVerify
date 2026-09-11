"""Serve the status page locally, for looking at it.

    Run and Debug -> "Status page (local preview)"    or
    .venv/bin/python scripts/dev_status.py

    -> http://127.0.0.1:5003/

WHY THIS IS NOT `wrangler dev`

The README has the one command that starts the real Worker, and that is the
real thing: real routing, real D1, the cron, /api/status.json and the signed
report endpoint. Use it when the question is about any of those.

It is the wrong tool for the question this script answers, which is "what does
the page look like". It installs wrangler on every start, migrates a local
database, and then shows a page with no history in it, because a fresh D1 has
none -- so the ninety-day strip, the incident banner and the folded timelines,
which are most of what there is to look at, are all absent until somebody
fabricates rows in SQL. This renders the page with an incident open, with days
that went badly, and with no install step.

WHY IT SHELLS OUT TO DOCKER

status/src/render.js is JavaScript and there is no node on this machine, which
is the same reason the tests run in a container (see status/README.md). The
page is rendered by scripts/dev_status_page.mjs inside node:22-bookworm-slim
with the repository mounted READ ONLY, and this script serves what it printed.

Rendered per request, so a save to render.js or style.css is visible on reload
the way it is for the other two previews. That costs about a second a page, all
of it container start-up, and it is paid only by the HTML: the stylesheet, the
theme script and the font are read straight off disk.

WHAT IT DOES NOT DO

No /api/status.json and no POST /report. Both live in status/src/index.js and
both need D1, so neither can be answered without the real Worker. The link in
the page's own footnote goes nowhere here, deliberately: an invented status
feed is a thing somebody would eventually believe.
"""

from __future__ import annotations

import http.server
import mimetypes
import os
import pathlib
import socketserver
import subprocess
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PUBLIC = REPO / "status" / "public"
RENDERER = "scripts/dev_status_page.mjs"
IMAGE = "node:22-bookworm-slim"
HOST, PORT = "127.0.0.1", 5003

# "incident" | "clear" | "stale". The launch configurations set it; the default
# is the busy page, because an all-clear page is the one that shows least.
STATE = os.environ.get("PREVIEW_STATE", "incident")

# The twelve, read out of the Worker rather than typed here, so a language
# added there is previewable without touching this file (#300). A crude regex
# on purpose: this is a development script, and parsing JavaScript properly to
# find a list of two-letter strings would be a worse trade than being wrong
# loudly.
_I18N = (REPO / "status" / "src" / "i18n.js").read_text(encoding="utf-8")
LOCALES = ["en", *re.findall(r'"([a-z]{2}(?:-[A-Za-z]{2,4})?)"',
                             re.search(r"export const LOCALES = \[(.*?)\];",
                                       _I18N, re.S).group(1))]

# The language `/` is drawn in. The Worker negotiates it from Accept-Language;
# here it is an environment variable, because the point of this preview is
# choosing what to look at rather than simulating a browser.
LOCALE = os.environ.get("PREVIEW_LOCALE", "en")

PAGES = {"/": "page", "/admin": "admin"}


def render(which: str, locale: str = LOCALE) -> bytes:
    """Run the renderer in a container and hand back what it printed.

    Failures come back AS THE PAGE, in a <pre>, rather than as a 500 with the
    reason in the terminal behind the browser. Everything that goes wrong here
    goes wrong in JavaScript -- a syntax error in render.js, a bad import --
    and a preview that answers those with a blank tab is one that sends you
    looking in the wrong file.
    """
    proc = subprocess.run(
        ["docker", "run", "--rm", "-i",
         "-v", f"{REPO}:/repo:ro", "-w", "/repo",
         "-e", f"PREVIEW_STATE={STATE}",
         "-e", f"PREVIEW_LOCALE={locale}",
         IMAGE, "node", RENDERER, which],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace")
        sys.stderr.write(f"\n  render failed:\n{detail}\n")
        body = ("<!doctype html><meta charset=utf-8><title>Preview failed</title>"
                "<h1>The renderer exited non-zero</h1><pre>"
                + detail.replace("&", "&amp;").replace("<", "&lt;")
                + "</pre>")
        return body.encode("utf-8")
    return proc.stdout


class Handler(http.server.BaseHTTPRequestHandler):
    """Two rendered pages, and the real static files behind them."""

    def do_GET(self):  # noqa: N802 -- the base class names it
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in PAGES:
            return self.reply(render(PAGES[path]), "text/html; charset=utf-8")

        # `/ja`, `/pt-BR`, and the other nine, exactly as the Worker routes
        # them. Worth mirroring rather than approximating with a query string:
        # the language picker in the header renders these paths, so a preview
        # that answered something else would have a picker that 404s and a
        # reviewer who concludes the picker is broken.
        wanted = path.rstrip("/").lstrip("/")
        match = next((code for code in LOCALES if code.lower() == wanted.lower()), None)
        if match == "en":
            # The Worker answers /en with a permanent redirect rather than a
            # second copy of English, so this does too. A preview that serves
            # a page where production serves a 301 is a preview that hides the
            # one thing anybody would check /en for.
            self.send_response(301)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if match:
            return self.reply(render("page", match), "text/html; charset=utf-8")

        # Everything else is a real file from status/public, which is exactly
        # what the Worker serves for these paths. Resolved before the prefix is
        # checked, because a path that starts inside a directory can still
        # climb out of it with a "..".
        full = os.path.normpath(os.path.join(str(PUBLIC), path.lstrip("/")))
        if not full.startswith(str(PUBLIC) + os.sep) or not os.path.isfile(full):
            return self.reply(b"not found here -- see the WHAT IT DOES NOT DO "
                              b"note in scripts/dev_status.py",
                              "text/plain; charset=utf-8", status=404)
        kind = mimetypes.guess_type(full)[0] or "application/octet-stream"
        return self.reply(pathlib.Path(full).read_bytes(), kind)

    def reply(self, body: bytes, kind: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # The preview re-renders per request and the browser must not decide
        # otherwise, or a save stops being visible on reload and the reason is
        # invisible too.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> None:
    if not (REPO / RENDERER).is_file():
        sys.exit(f"no renderer to run: {REPO / RENDERER} does not exist")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("  Docker is not running, and the page is rendered by node "
                 "inside it.\n  See status/README.md.")

    # See dev_site.py: allow_reuse_address has to be on the CLASS, because
    # TCPServer binds inside __init__ and setting it afterwards is a no-op that
    # looks exactly like a fix.
    server = type("Preview", (socketserver.ThreadingTCPServer,),
                  {"allow_reuse_address": True, "daemon_threads": True})
    try:
        httpd = server((HOST, PORT), Handler)
    except OSError as error:
        sys.exit(f"cannot serve on {HOST}:{PORT}: {error}\n"
                 "Another copy of this preview is probably still running.")
    with httpd:
        # flush, because Run and Debug watches this line to open a browser
        # and a buffered stdout means it never sees it.
        print(f"\n  Status page preview: http://{HOST}:{PORT}/", flush=True)
        print(f"  Showing: {STATE}  (incident | clear | stale, via PREVIEW_STATE)")
        print(f"  Language: {LOCALE} at /, and all twelve at /<code>, e.g. "
              f"http://{HOST}:{PORT}/ja and /ar for right to left.")
        print(f"  Also: http://{HOST}:{PORT}/admin, the incident form.")
        print("  Rendered per request, so a save is visible on reload.")
        print("  Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped.")


if __name__ == "__main__":
    main()
