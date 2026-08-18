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
   That is a broad grant to publish four static pages, and it is worth trimming
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
