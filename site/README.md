# The apex site — vrcverify.com

Six static pages: the landing page, the changelog, Terms of Service, Privacy
Policy, Refund Policy and a 404. Stripe, Discord and the dashboard all link
here, so these pages need to stay up when the VPS does not.

Deliberately static, deliberately not part of the dashboard app:

- **A separate failure domain.** If the dashboard is down, its Terms and Privacy
  links must still resolve. Serving them from the same Flask app ties the two
  together for no benefit.
- **No new public route on the VPS.** `SECURITY_AUDIT.md` §2 assumes the public
  host is compromised. Adding unauthenticated pages to the box holding the bot
  API signing key widens that surface to publish documents that barely change.
- **No third-party dependencies.** One stylesheet, one same-origin script, one
  same-origin font, and no request that leaves this origin. A legal page that
  needs a CDN is a legal page that can be unavailable at the moment somebody
  needs to read it.

  This line used to read "no fonts", and #195 phase 2 amended it rather than
  breaking it. The rule's stated reason is about a *third party*, and that
  reason is untouched: `fonts/inter-latin-var.woff2` is served by the same
  Worker as the page, so it cannot be unavailable while the page is available.
  The font is also declared `font-display: swap`, so if it is slow or never
  arrives the page renders in the system stack — which is exactly what this
  site looked like before it was added. There is no failure this introduces
  that is worse than the state it replaced.

  What the rule still forbids, and always will: a font CDN, which would let a
  third party see who reads the Privacy Policy. That is the same reasoning
  that vendored Inter into the dashboard in the first place.

  The script is `theme.js` and it is the theme toggle, added in #137 phase 1.
  It is the only one, it is served from this origin, and every page renders
  completely without it — blocked or disabled, you get the default dark theme
  and no picker. `tests/test_site.py` refuses any script that is inline or
  loaded from anywhere else.

## Before this goes live

Two things.

1. **`contact@esattotech.com` must be monitored, not just deliverable.** Every
   page names it and the Privacy Policy promises a 30-day response to access and
   erasure requests, which is a commitment to read the mailbox rather than to
   own it.

   The contact deliberately sits on the *company* domain rather than the product
   one: the seller named on the site is Esatto Technologies, the seller named on
   the Stripe invoice is Esatto Technologies, and a contact address on a third
   name would invite exactly the question a disputing customer asks. It also
   matches where the bot's own `/vrcverify_support` already points.

2. **Confirm the operating entity name.** These pages say *Esatto Technologies*,
   taken from the Stripe account. If the legal seller is a different entity, or
   an individual, every page's footer and the "Who we are" section need to match
   what Stripe holds — a mismatch between the seller named on the site and the
   one named on the invoice is exactly what a payment dispute turns on.

## Deploying

Cloudflare no longer offers Pages for new projects on this account -- the
Workers & Pages screen has only "Create application", and that flow deploys a
Worker with `npx wrangler deploy`. So this is an **assets-only Worker**: no
`main`, no code on the request path, Cloudflare just serves `./site`. Same
outcome Pages would have given, with the config in `wrangler.toml` where a diff
can show it.

1. Workers & Pages -> Create application -> import `PhillipDiCarlo/VRCVerify`.
2. **Project name** `vrcverify`. **Build command** empty. **Deploy command**
   `npx wrangler deploy`. **Path** `/`.
3. Let it create the API token. Note that the token is account-wide -- Workers
   Scripts, KV, R2, D1, Queues, Containers, and Workers Routes for every zone.
   That is a broad grant to publish a handful of static pages, and it is worth
   trimming
   in My Profile -> API Tokens once the first deploy has proved the flow.
4. After the first deploy: the Worker -> Settings -> Domains & Routes -> add
   `vrcverify.com` and `www.vrcverify.com`.

`dashboard.vrcverify.com` is untouched by any of this and must stay that way.
It is the Flask app on the VPS behind the cloudflared tunnel, and the whole
argument for a separate apex is that the two do not share a failure domain.

### Checking it worked

**Not from the LAN.** Local DNS answers `vrcverify.com` with `10.53.1.89`, so a
browser on that network is sent somewhere else entirely and a working deploy
looks broken. Use mobile data, or pin the address:

```
curl -sI --resolve vrcverify.com:443:<cloudflare-ip> https://vrcverify.com/terms.html
```

That override is worth fixing separately -- as it stands, nobody on the home
network can see the finished site.

## The canonical URLs

Cloudflare's default `html_handling` answers `/terms.html` with a **307 to
`/terms`**. Both work, which is what makes it easy to miss. Internal links use
the extension-less form, and so should anything published elsewhere:

| Page | URL to publish |
|---|---|
| Terms of Service | `https://vrcverify.com/terms` |
| Privacy Policy | `https://vrcverify.com/privacy` |
| Refund Policy | `https://vrcverify.com/refunds` |
| Changelog | `https://vrcverify.com/changelog` |

These go into Stripe and Discord, live outside this repository, and are the
kind of thing nobody revisits. Point them at the address that answers rather
than the one that forwards. `tests/test_site.py::test_internal_links_are_canonical`
keeps the pages themselves honest.

## After it is live

- **Stripe** → Settings → Billing → Customer portal → Business information: set
  the Terms of Service and Privacy Policy URLs. They are currently unset, which
  `VPS_RUNBOOK.md` §15 records as blocked on this site existing.
- **Discord** → Developer Portal → your application → General Information: set
  the Terms of Service and Privacy Policy URLs. These are also a prerequisite of
  the privileged-intents review, so this unblocks more than #88.
- **The bot's `support_info` string** still points at
  `https://esattotech.com/contact-us/`. Now that VRCVerify has its own contact
  route, that is worth revisiting — a person querying a VRCVerify charge should
  not land on a different company's contact form.

## Theming

Dark by default, with a Dark/Light/System picker in the header — the same three
states the dashboard offers (#123), reached differently because there is no
server here to render the choice into the first paint.

The mechanism, and the one thing to keep in mind when editing:

- **No `data-theme` attribute means dark.** That is the floor of the cascade in
  `style.css`, so it is what a first visit paints, and what every visit paints
  with JavaScript off. The dashboard can treat "no attribute" as *System*
  because its server always knows what to stamp; here, no attribute is also the
  pre-script state, so *System* has to be an explicit `data-theme="system"`.
- `theme.js` reads `localStorage` and stamps the attribute from a **blocking**
  `<script>` in `<head>`. That is deliberate: `defer` or `async` would let the
  body paint before the attribute lands, which is a visible flash on every
  navigation for anyone who chose Light. A test asserts neither is present.
- The picker ships `hidden` and is revealed by the script, so a control that
  cannot work without JavaScript is never painted before JavaScript arrives.

The colour values are **copied from `src/dashboard/static/style.css`** rather
than shared — different origin, different deploy, and the whole point of this
site is that it does not depend on the dashboard's host. Both files carry a
comment saying so. Change a colour in one, change it in the other.

The same is now true of **the typeface and the type ramp** (#195). `Inter var`
is copied into `fonts/` rather than fetched from the dashboard, for exactly the
reason the colours are copied. The ramps are *not* identical — a console is
denser than a document — but `--text-display`, the marketing size, is, and a
test fails if the two declarations drift apart.

## The changelog is generated — do not edit it

`site/changelog.html` is rendered from `ENTRIES` in
`src/dashboard/changelog.py`, filtered through `public_entries()`. It is the
one file here that is not written by hand, and the file itself says so at the
top.

```
python scripts/gen_changelog.py          # rewrite it
python scripts/gen_changelog.py --check  # exit 1 if it is out of date
```

**Re-run it in the same commit as any change to `ENTRIES`.** Nothing
regenerates it on push — `.github/workflows/` holds CodeQL and nothing else —
so the guard is `tests/test_site.py::test_the_committed_changelog_matches_the_constant`,
which regenerates in memory and fails if the committed file disagrees. That is
the price of committing generated output, and it is worth paying: the page
stays a static asset, so it is live even when the dashboard is not.

The generator copies the header and footer out of an existing page rather than
holding its own copy, for the same reason the five pages are checked against
each other — a third hand-written copy is a third thing to drift.

Entries marked `public=False` never reach this page. That flag is the only
thing separating an entry written for a signed-in admin from one strangers
read, so it is tested against a fabricated private entry rather than waiting
for the first real one.

## Announcing a release in Discord

Admins can *follow* VRCVerify's announcement channel, which crossposts every
post we make into their own servers (issue #138). That makes the Discord post
a **fourth rendering of `ENTRIES`**, alongside this page, the dashboard feed
and the in-app bell — and the only one with a human in the loop.

So it belongs in the same commit-and-release habit as the generator above:

1. Add the entry to `ENTRIES` in `src/dashboard/changelog.py`.
2. `python scripts/gen_changelog.py` in the same commit.
3. Post it in the announcement channel, **copied from the entry rather than
   rewritten**, and publish it so followers receive it.

**Do not let the Discord copy and the changelog copy drift.** Nothing enforces
this one — the other three renderings share a constant and a test, this one
shares a person. If it proves error-prone, that is the argument for driving it
from `ENTRIES` with a webhook, not for accepting two versions of the same
sentence.

Two things worth knowing before automating it: the bot must be *in* that server
and hold Manage Messages to crosspost, and crossposting is rate limited. At a
handful of posts a year neither matters, which is exactly why it is worth
writing down now rather than rediscovering under pressure.

Posts land in **other people's servers, in front of their members** rather than
only their admins. Keep them short, keep them useful, and assume a non-admin is
reading. Entries marked `public=False` are for signed-in admins and must not be
posted here at all.

### Rotating the invite means changing three things

The invite is meant to be non-expiring, so this should be rare — but if it ever
does change, **nothing detects a miss.** The three live in different places on
purpose and none of them can see the others:

| where | what | who reads it |
|---|---|---|
| the bot host's `.env` | `SUPPORT_INVITE_URL` | `/vrcverify_support`, `/vrcverify_setup` |
| the VPS's `dashboard.env` | `SUPPORT_INVITE_URL` | the bell panel, the dashboard changelog |
| `scripts/gen_changelog.py` | `SUPPORT_INVITE_URL` constant | this page — **regenerate after changing it** |

The third is the one that gets forgotten, because it is code rather than
configuration and because it is the only one that needs a second step. It is
also the worst one to get wrong: it is the **public** page, so a stale link
there is broken for people who have never signed in and cannot tell you.

It is a constant rather than an environment read for a reason — see the comment
on it. These are static files behind a CDN, so nothing renders them at request
time, and reading the environment at generation time would make the committed
HTML depend on whose shell ran the script while `--check` regenerates in memory
and compares.

## Editing

Plain HTML, one shared `style.css`, no build step. Keep the footers and navs in
step by hand; there are only a handful of files, and a template engine here
would be a build step for the sake of a build step. `tests/test_site.py` fails
if a header or footer drifts from the others, which is what makes that safe to
do by hand.

The exception is `changelog.html` — see above. Style it in `style.css` like any
other page; never edit its markup.

Update the `Last updated` date at the top of any policy you change materially.

### Renaming a section of a legal page

Terms, Privacy and Refunds each carry an "On this page" list at the top, and
that list is a second copy of every section name. Rename a heading and the copy
several hundred lines above it still says the old thing, pointing at an `id`
that no longer exists.

So change both, and change the `id` only if you mean to: `id`s are a public
contract, held by Stripe, by Discord and by support replies that link into a
specific clause. `tests/test_site.py` derives the expected list from the page's
own headings and fails if the two disagree, so this cannot ship half done, but
the test tells you to regenerate the list rather than doing it for you.
