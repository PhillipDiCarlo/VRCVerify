"""Serve the apex site locally, for looking at it.

    Run and Debug -> "Apex site (local preview)"      or
    .venv/bin/python scripts/dev_site.py

    -> http://127.0.0.1:5002/

WHY THIS IS NOT `python -m http.server ./site`

Because that does not serve this site. Every internal link on these pages is
extensionless -- /terms, /privacy, /refunds, /changelog -- and there is no
terms file on disk, only terms.html. A plain static server 404s on all of them,
which means the one thing the preview is for, clicking through the pages, is
the one thing it cannot do.

In production that mapping is Cloudflare's, not ours: the Worker is
assets-only, so no code of ours runs on a request. This script reimplements the
two behaviours of it that a reader can see, and nothing else:

  * an extensionless path resolves to `<path>.html` if that file exists, which
    is what Cloudflare's default `html_handling` does; and
  * anything still unmatched serves 404.html WITH A 404 STATUS, which is what
    `not_found_handling = "404-page"` in wrangler.toml does.

THE STATUS CODE IS NOT A DETAIL. Serving the 404 page with a 200 is the usual
way a hand-rolled dev server differs from the edge, and it hides exactly the
bug this site cannot afford: Stripe and Discord both hold links into these
pages, and a soft 404 is indistinguishable from a working page to everything
except a human reading it.

WHAT THIS IS NOT

Not a deployment path and not a staging environment. `npx wrangler dev` is the
real thing and runs the actual asset handler; this exists because it needs
Docker and a node toolchain, and looking at a stylesheet should not.

It also does not serve the dashboard or the status page. Links to those are
absolute URLs to production and stay that way here, so clicking "Dashboard"
leaves the preview -- which is correct, and the same thing it does in a
browser on the real site.
"""

from __future__ import annotations

import functools
import http.server
import pathlib
import socketserver
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SITE = REPO / "site"
HOST, PORT = "127.0.0.1", 5002


class Handler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler plus the two Cloudflare behaviours above."""

    def send_head(self):
        # Resolve /terms -> terms.html before the base class looks for a
        # directory called "terms" and gives up.
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path not in ("/", "") and not pathlib.PurePosixPath(path).suffix:
            candidate = SITE / (path.lstrip("/") + ".html")
            if candidate.is_file():
                self.path = path.rstrip("/") + ".html"
        return super().send_head()

    def send_error(self, code, message=None, explain=None):
        """404.html, with a 404, exactly as the edge serves it."""
        page = SITE / "404.html"
        if code != 404 or not page.is_file():
            return super().send_error(code, message, explain)
        body = page.read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> None:
    if not (SITE / "index.html").is_file():
        sys.exit(f"no site to serve: {SITE / 'index.html'} does not exist")

    handler = functools.partial(Handler, directory=str(SITE))
    # Threading, so a page that pulls a stylesheet, two scripts and a font does
    # not serialise them behind one another and take a visible moment to paint.
    #
    # ON THE CLASS, NOT THE INSTANCE. TCPServer binds inside __init__, so
    # setting this on the object afterwards is too late to affect the socket it
    # already has -- which is a no-op that looks exactly like a fix. Without it,
    # stopping the preview and starting it again inside the TIME_WAIT window
    # fails with "Address already in use", which is the normal way somebody
    # uses this: stop it, edit, start it.
    server = type("Preview", (socketserver.ThreadingTCPServer,),
                  {"allow_reuse_address": True, "daemon_threads": True})
    try:
        httpd = server((HOST, PORT), handler)
    except OSError as error:
        sys.exit(f"cannot serve on {HOST}:{PORT}: {error}\n"
                 "Another copy of this preview is probably still running.")
    with httpd:
        home = f"http://{HOST}:{PORT}/"
        print(f"\n  Apex site preview: {home}")
        print("  Pages: / /terms /privacy /refunds /changelog, and /nothing "
              "for the 404.")
        print("  Files are read per request, so a save is visible on reload.")
        print("  Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped.")


if __name__ == "__main__":
    main()
