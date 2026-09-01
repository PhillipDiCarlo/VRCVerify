/**
 * What the page is willing to say, and what it is willing to name.
 *
 * THE ONE RULE THIS FILE EXISTS TO ENFORCE
 *
 * The public surface names capabilities. It never names infrastructure. There
 * is no row for Postgres, none for RabbitMQ, none for the bot API, the tunnel,
 * the tailnet or either host, and none of those words appear in the rendered
 * HTML or in /api/status.json. That is not tidiness. SECURITY_AUDIT.md section
 * 2 spends the dashboard's entire design on the public box knowing nothing
 * about the private one, and a status page that helpfully lists the estate
 * would hand back what that design is protecting.
 *
 * The infrastructure detail still exists -- it is what makes an alert useful
 * at 3am -- and it travels to a private Discord channel instead. Public page,
 * private alert. tests/test_status_page.py fails if an infrastructure name
 * reaches the public side.
 */

/**
 * The five rows a reader sees, in the order they are drawn.
 *
 * Ordered by what a person who is having a bad time is looking for. Someone
 * arrives here because verification did not work, so verification is first.
 * The website is last because a reader who can see this page has strong
 * evidence about it already.
 */
export const COMPONENTS = [
  {
    id: "verification",
    name: "Verification",
    description: "Age checks requested in Discord and answered against VRChat.",
  },
  {
    id: "bot",
    name: "Discord bot",
    description: "The bot responding to commands and handing out roles.",
  },
  {
    id: "invites",
    name: "Group invites",
    description: "Invites to a server's VRChat group after a check passes.",
  },
  {
    id: "dashboard",
    name: "Dashboard and sign-in",
    description: "dashboard.vrcverify.com, where servers are configured.",
  },
  {
    id: "website",
    name: "Website",
    description: "vrcverify.com and the documents linked from it.",
  },
];

export const COMPONENT_IDS = COMPONENTS.map((c) => c.id);

/**
 * The private parts the homelab reporter speaks about, and the public
 * capability each one breaks (phase 2).
 *
 * A part may appear against more than one capability, and that is the point of
 * the mapping existing at all: the queue being down is not a row on the page,
 * it is verification AND invites both going dark, which is what a reader would
 * actually experience. Doing this in the Worker rather than in the reporter
 * keeps it testable and keeps the reporter dumb.
 */
export const PART_CAPABILITIES = {
  "discord-bot": ["bot", "verification", "invites"],
  "vrc-online-checker": ["verification"],
  "vrc-group-inviter": ["invites"],
  // NOT "down". A capability may be listed as `{ capability, as: "degraded" }`,
  // meaning this part failing caps that row at degraded however badly the part
  // itself is doing.
  //
  // The dashboard is the case that needs it, and the first version got it
  // wrong. When the homelab went quiet the dashboard row went red -- while a
  // live probe of dashboard.vrcverify.com was answering 200 in the same
  // minute. Both facts were true: the site loads, signing in works, and every
  // page that needs the bot behind it fails. "Down" is the wrong word for
  // that, because a reader who can see the page loading will simply conclude
  // the status page is broken, and stop believing the rows that are right.
  //
  // The public probe stays the authority on whether the dashboard is
  // reachable. What the homelab can do is pull it down to degraded.
  "bot-api": [{ capability: "dashboard", as: "degraded" }],
  database: ["verification", "bot", "invites", { capability: "dashboard", as: "degraded" }],
  queue: ["verification", "invites"],
};

/**
 * How stale a heartbeat may be before silence is treated as an outage.
 *
 * The reporter posts every 60 seconds. Three missed reports is the threshold
 * rather than one, for the same reason a single failed probe is not an outage:
 * a restart, a slow DNS answer or one dropped request should not put a red row
 * on a public page. Below this the reported state stands; above it the part is
 * `down`, not `unknown`, because the reporter going quiet is itself a fact
 * about the homelab and pretending otherwise would be the comfortable lie.
 */
export const HEARTBEAT_STALE_SECONDS = 195;

/**
 * The services this project depends on, drawn in their own section.
 *
 * Three of the four are Statuspage and answer the same /api/v2/status.json
 * shape. Stripe is NOT: status.stripe.com/api/v2/status.json is a 404 (checked
 * 2026-08-31) and its feed is /current, with a different shape and no
 * documentation promising it will stay. Both readers are in logic.js and both
 * answer `unknown` rather than guessing when they meet something unfamiliar.
 *
 * Gmail is deliberately absent. It is a genuine runtime dependency -- the
 * checker fetches VRChat's 2FA codes from it -- and issue #170 decided it is
 * listed nowhere and alerts nowhere. The consequence, written down so it is a
 * choice rather than a surprise: a Gmail outage will show here as verification
 * degrading with no named cause.
 *
 * Docker Hub was considered and dropped. It is a dependency of deploying, not
 * of running, and a status page answers "is it working now".
 */
export const UPSTREAMS = [
  {
    id: "discord",
    name: "Discord",
    kind: "statuspage",
    url: "https://discordstatus.com/api/v2/status.json",
    href: "https://discordstatus.com",
    why: "Everything the bot does happens here.",
  },
  {
    id: "vrchat",
    name: "VRChat",
    kind: "statuspage",
    url: "https://status.vrchat.com/api/v2/status.json",
    href: "https://status.vrchat.com",
    why: "Age checks are answered by VRChat's API.",
  },
  {
    id: "stripe",
    name: "Stripe",
    kind: "stripe",
    url: "https://status.stripe.com/current",
    href: "https://status.stripe.com",
    why: "Premium subscriptions and the billing portal.",
    // Only the three Stripe services this project actually uses. `largestatus`
    // would also fold in their support site and Stripe.js, neither of which
    // can affect anything here, and a page that goes amber for someone else's
    // documentation being slow is a page people stop believing.
    services: ["api", "webhooks", "checkout"],
  },
  {
    id: "cloudflare",
    name: "Cloudflare",
    kind: "statuspage",
    url: "https://www.cloudflarestatus.com/api/v2/status.json",
    href: "https://www.cloudflarestatus.com",
    why: "DNS, the route to the dashboard, and this page itself.",
  },
];

/** How long the rendered page may be cached at the edge, in seconds. */
export const PAGE_CACHE_SECONDS = 30;

/** Days of history the page draws and the pruner keeps. */
export const HISTORY_DAYS = 90;
