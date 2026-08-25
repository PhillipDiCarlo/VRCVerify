"""Does the browser follow the 303 to Stripe, or silently refuse it? (#141)

    python scripts/check_stripe_redirect.py

Drives scripts/dev_dashboard.py, so every guarantee that file makes holds
here: no .env, fake credentials, loopback only, a stubbed bot and a stubbed
Stripe client. Needs playwright, like scripts/shoot_pages.py, and is
DELIBERATELY NOT A TEST for the same reason -- and for one more: what it
measures cannot be observed from the server at all.


`form-action` governs where a submission may end up INCLUDING AFTER A
REDIRECT. When it is wrong the browser sends the POST, receives the 303, and
declines to navigate: no error page, nothing in the server log, the button
simply does nothing. That was the "Subscribe does nothing" bug of 2026-08-15,
and no server-side test can see it.

WHAT IS MEASURED, after four instruments that lied
--------------------------------------------------
Whether the browser issues a GET to a stripe.com origin after the POST. That
is the whole question, and the raw request trace is the only thing that
answered it honestly.

The four that did not, all failing in the SAME direction -- reporting "the
button did nothing" while the browser was in fact reaching Stripe:

  * `page.route` with a `https://*.stripe.com/**` glob -- fired for
    subresources only, and the fact that js.stripe.com loaded was itself proof
    the navigation had succeeded.
  * `page.route` with a `https://**` glob -- matched nothing at all, and
    silently let every request through to real Stripe while reporting none.
  * the same with a compiled regex -- routing does not fire for the navigation
    a 303 produces.
  * `securitypolicyviolation` with the context offline -- the event never
    fires for an aborted navigation, and offline blocks the request before it
    can be observed, so a green result and a red one looked identical.

A verification tool that fails closed on its own bug is worse than none, which
is why CONTROL exists: it runs the same click against a policy with Stripe
removed from `form-action` and requires the result to flip. Nothing here is
trusted until that control goes red.

THIS ONE DOES REACH STRIPE -- one GET to a public checkout URL carrying an
obviously fake session id, which is exactly what a person clicking the button
to check this would do. No key, no account, no personal data. Every local
alternative was tried above and none of them can tell the two outcomes apart.
"""
import os, pathlib, subprocess, sys, time, urllib.parse, urllib.request
REPO = pathlib.Path("/Users/italiandogs/Documents/Git-Repos/VRCVerify")
BASE="http://127.0.0.1:5001"; FREE="700000000000000002"

def is_stripe(url):
    """Host check, not a substring one: evil.com/?x=stripe.com is not Stripe."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "stripe.com" or host.endswith(".stripe.com")

WATCH = """
window.__csp = [];
document.addEventListener('securitypolicyviolation',
  e => window.__csp.push(e.violatedDirective + ' -> ' + e.blockedURI));
"""

def run(state, click, label, break_csp=False):
    env = dict(os.environ, PREVIEW_SUB=state)
    if break_csp:
        env["PREVIEW_BREAK_FORM_ACTION"] = "1"
    proc = subprocess.Popen([sys.executable, str(REPO/"scripts"/"dev_dashboard.py")],
                            cwd=REPO, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try: urllib.request.urlopen(BASE, timeout=1); break
            except Exception: time.sleep(0.5)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b=p.chromium.launch(); ctx=b.new_context(); page=ctx.new_page()
            page.add_init_script(WATCH)
            seen=[]
            page.on("request", lambda r: seen.append((r.method, r.url)))
            page.on("response", lambda r: seen.append((str(r.status), r.url)))
            page.goto(f"{BASE}/guild/{FREE}/subscription", wait_until="networkidle")
            page.click(click)
            page.wait_for_timeout(1200)
            posted = any(m == "POST" for m, _ in seen)
            hops = [u for m, u in seen if m == "GET" and is_stripe(u)]
            verdict = "followed" if hops else "BLOCKED -- the button did nothing"
            print(f"  {label:22} POST={posted} -> {verdict}")
            if hops: print(f"    {hops[0][:76]}")
            b.close()
    finally:
        proc.terminate(); proc.wait()

print("LIVE POLICY (must follow):")
run("lapsed", ".plan-card button[type=submit]", "Subscribe")
run("card", "form[action$='/portal'] button[type=submit]", "Manage billing")
print("\nCONTROL, Stripe removed from form-action (must be blocked):")
run("lapsed", ".plan-card button[type=submit]", "Subscribe", break_csp=True)
run("card", "form[action$='/portal'] button[type=submit]", "Manage billing", break_csp=True)
