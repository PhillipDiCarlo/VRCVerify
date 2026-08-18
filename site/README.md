# The apex site — vrcverify.com

Four static pages: the landing page, Terms of Service, Privacy Policy and
Refund Policy. Stripe, Discord and the dashboard all link here, so these pages
need to stay up when the VPS does not.

Deliberately static, deliberately not part of the dashboard app:

- **A separate failure domain.** If the dashboard is down, its Terms and Privacy
  links must still resolve. Serving them from the same Flask app ties the two
  together for no benefit.
- **No new public route on the VPS.** `SECURITY_AUDIT.md` §2 assumes the public
  host is compromised. Adding unauthenticated pages to the box holding the bot
  API signing key widens that surface to publish four documents that never
  change.
- **No dependencies.** One stylesheet, no fonts, no scripts, no third-party
  requests. A legal page that needs a CDN is a legal page that can be
  unavailable at the moment somebody needs to read it.

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

## Deploying to Cloudflare Pages

Pages builds straight from this directory, so publishing is a merge.

1. Cloudflare dashboard → Workers & Pages → Create → Pages → Connect to Git.
2. Pick this repository. **Build command:** none. **Build output directory:**
   `site`. **Framework preset:** none.
3. Custom domains → add `vrcverify.com` and `www.vrcverify.com`. Cloudflare
   creates the DNS records itself, since the zone is already here.
4. Check both resolve over HTTPS, and that `/terms.html`, `/privacy.html` and
   `/refunds.html` all load.

The apex is not on the cloudflared tunnel and must not be added to it. The
tunnel serves `dashboard.vrcverify.com` from the VPS; this is the separation the
whole arrangement exists for.

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

## Editing

Plain HTML, one shared `style.css`, no build step. Keep the four footers and the
four navs in step by hand; there are only four files, and a template engine here
would be a build step for the sake of a build step.

Update the `Last updated` date at the top of any policy you change materially.
