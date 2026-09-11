/**
 * The status page, rendered to stdout with invented data.
 *
 * Driven by scripts/dev_status.py, which runs it in a container because there
 * is no node on the development machine. Runs happily under a local node too:
 *
 *     node scripts/dev_status_page.mjs > /tmp/status.html
 *
 * WHY THIS EXISTS RATHER THAN `wrangler dev`
 *
 * The real Worker is the real thing and the README has the one command that
 * starts it. It also installs wrangler, migrates a local D1 and starts with an
 * empty database, so the first thing it shows is a page with no history in it.
 * This renders the page the way somebody looking at the CSS needs to see it:
 * with an incident open, with days that went badly, and instantly.
 *
 * NOTHING HERE IS SHIPPED. It imports the same render.js the Worker does and
 * invents the argument, so a change to the page shows up here on the next
 * reload and a change here cannot affect the page.
 */

import { COMPONENTS, HISTORY_DAYS, UPSTREAMS } from "../status/src/config.js";
import { recentDays } from "../status/src/logic.js";
import { renderPage, renderAdmin } from "../status/src/render.js";
import { DEFAULT_LOCALE, LOCALES, translator } from "../status/src/i18n.js";

// "incident" | "clear" | "stale", from dev_status.py.
const STATE = process.env.PREVIEW_STATE ?? "incident";

/**
 * Which of the twelve to draw (#300).
 *
 * The point of previewing a language is the LAYOUT, not the words: German
 * runs about ten percent longer than English and is what overflows a pill,
 * Japanese is short enough to leave a row looking empty, and Arabic is the
 * one that arrives under dir="rtl". Reading them in a browser is the only
 * way to see any of that, and `wrangler dev` will not show it without a
 * populated D1.
 */
const LOCALE = process.env.PREVIEW_LOCALE ?? DEFAULT_LOCALE;
if (!LOCALES.includes(LOCALE)) {
  process.stderr.write(`unknown PREVIEW_LOCALE ${LOCALE}; try one of ${LOCALES.join(", ")}\n`);
  process.exit(2);
}
const WHICH = process.argv[2] ?? "page";

// Fixed, not `Date.now()`. A preview whose timestamps move every reload is one
// where "did that just change" can never be answered by looking twice.
const now = Math.floor(Date.parse("2026-09-10T14:20:00Z") / 1000);
const days = recentDays(now, HISTORY_DAYS);

/**
 * Days that went badly, keyed by how many days ago they were.
 *
 * A WALL OF GREEN PROVES NOTHING. The strip's whole job is telling a bad day
 * from a busy one, and a preview with a perfect record cannot show whether it
 * still does. These sit at different depths on purpose, so the ones inside the
 * narrow window and the ones outside it are both represented.
 */
const TROUBLE = {
  verification: { 12: { up: 1200, down: 240 }, 41: { up: 1430, degraded: 10 },
                  73: { up: 900, down: 540 } },
  bot: { 41: { up: 1438, down: 2 }, 42: { up: 1200, maintenance: 240 } },
  invites: { 5: { up: 1100, degraded: 340 }, 6: { up: 1439, degraded: 1 } },
  dashboard: { 73: { up: 1340, down: 100 } },
  website: {},
};

const byComponent = {};
for (const component of COMPONENTS) {
  const rows = {};
  days.forEach((day, index) => {
    // The oldest few are left absent, which is the state the page really
    // shipped in and the only way to see the no-data colour.
    if (index < 6) return;
    rows[day] = {
      up: 1440, degraded: 0, down: 0, unknown: 0, maintenance: 0,
      ...(TROUBLE[component.id][index] ?? {}),
    };
  });
  byComponent[component.id] = rows;
}

const incidents = [
  {
    id: 4,
    title: "Age checks are slow to come back",
    impact: "degraded",
    started_at: now - 5400,
    resolved_at: null,
    updates: [
      { status: "monitoring", at: now - 900,
        body: "Checks are completing again. We are watching the queue before calling this fixed." },
      { status: "identified", at: now - 3600,
        body: "VRChat's API is answering slowly. Requests are queued, not lost, and will complete." },
      { status: "investigating", at: now - 5400,
        body: "We are looking into reports that verification is taking several minutes." },
    ],
  },
  {
    id: 3,
    title: "Group invites paused",
    impact: "down",
    started_at: now - 86400 * 3,
    resolved_at: now - 86400 * 3 + 7200,
    updates: [
      { status: "resolved", at: now - 86400 * 3 + 7200,
        body: "Invites have been sent to everyone who passed during the outage. Nothing needs to be re-run." },
      { status: "identified", at: now - 86400 * 3 + 1800,
        body: "The invite account was rate limited. Invites are being retried on a slower schedule." },
      { status: "investigating", at: now - 86400 * 3,
        body: "Invites are not being delivered after a passing check." },
    ],
  },
  {
    id: 2,
    title: "Dashboard sign-in failing",
    impact: "down",
    started_at: now - 86400 * 11,
    resolved_at: now - 86400 * 11 + 1500,
    updates: [
      { status: "resolved", at: now - 86400 * 11 + 1500, body: "Sign-in is working again." },
      { status: "investigating", at: now - 86400 * 11,
        body: "Signing in with Discord returns an error." },
    ],
  },
];

const allUp = (items) =>
  Object.fromEntries(items.map((item) => [item.id, { state: "up", since: now - 86400 * 30 }]));

const LIVE = {
  incident: {
    components: {
      verification: { state: "degraded", since: now - 5400 },
      bot: { state: "up", since: now - 86400 * 9 },
      invites: { state: "up", since: now - 86400 * 3 },
      dashboard: { state: "up", since: now - 86400 * 11 },
      website: { state: "up", since: now - 86400 * 30 },
    },
    upstreams: { ...allUp(UPSTREAMS), vrchat: { state: "degraded" } },
    incidents,
    freshness: "fresh",
  },
  clear: {
    components: allUp(COMPONENTS),
    upstreams: allUp(UPSTREAMS),
    incidents: [],
    freshness: "fresh",
  },
  // The rule the whole page is built around: nothing is drawn as up when the
  // checker has stopped. There is no other way to see it locally, and it is
  // the state most likely to be wrong after a change.
  stale: {
    components: allUp(COMPONENTS),
    upstreams: allUp(UPSTREAMS),
    incidents: [],
    freshness: "stale",
  },
};

const live = LIVE[STATE] ?? LIVE.incident;

process.stdout.write(
  WHICH === "admin"
    ? renderAdmin({ incidents, who: "you@example.com", now })
    : renderPage({
        t: translator(LOCALE),
        components: live.components,
        upstreams: live.upstreams,
        history: { days, byComponent },
        incidents: live.incidents,
        checkedAt: live.freshness === "fresh" ? now - 40 : now - 3600,
        now,
        freshness: live.freshness,
      }),
);
