/**
 * What the page is allowed to say.
 *
 * Two rules are enforced here rather than by reading the template carefully:
 * no infrastructure name reaches a reader, and no reader is shown green that
 * the data does not support. Both are one careless line away at all times,
 * and neither is visible in a diff.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { renderPage, escapeHtml, utcStamp } from "../src/render.js";
import { COMPONENTS, UPSTREAMS } from "../src/config.js";

const NOW = Date.UTC(2026, 7, 31, 18, 22, 0) / 1000;

function page(overrides = {}) {
  const components = {};
  for (const component of COMPONENTS) {
    components[component.id] = { state: "up", since: NOW - 86400 };
  }
  const upstreams = {};
  for (const upstream of UPSTREAMS) {
    upstreams[upstream.id] = { state: "up" };
  }
  return renderPage({
    components,
    upstreams,
    history: { days: [], byComponent: {} },
    checkedAt: NOW - 20,
    now: NOW,
    freshness: "fresh",
    ...overrides,
  });
}

/**
 * The words that must never appear. Every one of them names something the
 * dashboard's whole design keeps off the public box (SECURITY_AUDIT section
 * 2), and a status page listing the estate would hand back exactly what that
 * design protects. The detail still exists -- it goes to the private alert.
 */
const FORBIDDEN = [
  "postgres",
  "postgresql",
  "rabbit",
  "rabbitmq",
  "tailscale",
  "tailnet",
  "mtls",
  "homelab",
  "vps",
  "docker",
  "container",
  "queue",
  "bot-api",
  "healthz",
  "cloudflared",
  "tunnel",
];

test("no infrastructure name reaches the page, in any state", () => {
  const states = ["up", "degraded", "down", "unknown"];
  for (const state of states) {
    const components = {};
    for (const component of COMPONENTS) {
      components[component.id] = { state, since: NOW - 3600 };
    }
    const html = page({ components }).toLowerCase();
    for (const word of FORBIDDEN) {
      assert.ok(!html.includes(word), `"${word}" appears on the page in the ${state} state`);
    }
  }
});

test("a private detail string cannot be rendered even if one is handed in", () => {
  // The renderer is given rows that carry `detail`, because that is what the
  // database holds. It must read `state` and `since` and nothing else.
  const components = {};
  for (const component of COMPONENTS) {
    components[component.id] = {
      state: "down",
      since: NOW - 600,
      detail: "queue: connection refused on the homelab",
    };
  }
  const html = page({ components });
  assert.ok(!html.includes("connection refused"));
  assert.ok(!html.includes("homelab"));
});

test("stale data is drawn as unknown, however good the stored state was", () => {
  // The single most important assertion in this file. Every stored row says
  // `up`; the checker stopped an hour ago; the page must not show green.
  const html = page({ checkedAt: NOW - 3600, freshness: "stale" });
  assert.ok(!html.includes("All systems operational"));
  assert.ok(html.includes("Status is out of date"));
  assert.ok(!html.includes('class="row is-up"'));
  // Including the dependency rows. They come out of the same storage, written
  // by the same checker, so a stale checker knows no more about Discord than
  // it does about us.
  assert.equal(
    (html.match(/class="row is-unknown"/g) ?? []).length,
    COMPONENTS.length + UPSTREAMS.length,
  );
});

test("storage being unreachable is reported as this page's fault, not the bot's", () => {
  const html = renderPage({
    components: {},
    upstreams: {},
    checkedAt: null,
    now: NOW,
    freshness: "unavailable",
  });
  assert.ok(html.includes("Status cannot be read right now"));
  assert.ok(html.includes("says nothing about whether the services are working"));
});

test("before the first run the page says so", () => {
  const html = renderPage({
    components: {},
    upstreams: {},
    checkedAt: null,
    now: NOW,
    freshness: "missing",
  });
  assert.ok(html.includes("No check has run yet"));
  assert.ok(html.includes("No check has completed yet."));
});

test("all clear says all clear", () => {
  const html = page();
  assert.ok(html.includes("All systems operational"));
  assert.equal((html.match(/class="row is-up"/g) ?? []).length, COMPONENTS.length + UPSTREAMS.length);
});

test("state is carried by a word and a glyph, never by colour alone", () => {
  const components = {};
  for (const component of COMPONENTS) {
    components[component.id] = { state: "down", since: NOW - 7200 };
  }
  const html = page({ components });
  // The word.
  assert.ok(html.includes(">Down<"));
  // The glyph, which is a drawn cross rather than a colour swap.
  assert.ok(html.includes("<svg"));
  // And how long, which is the question a reader has immediately after "Down".
  assert.ok(html.includes("for 2 hours"));
});

test("an unknown row does not advertise how long it has been unknown", () => {
  // Caught in a screenshot, not in review. "Unknown for 2 minutes" times how
  // long this page has been failing to find out, which is a fact about the
  // page rather than about the service, and on a row that has never been
  // measured it is really the age of the deployment.
  const components = {};
  for (const component of COMPONENTS) {
    components[component.id] = { state: "unknown", since: NOW - 120 };
  }
  assert.ok(!page({ components }).includes("for 2 minutes"));

  // Whereas a row that IS down says so, because that is the reader's next
  // question after "Down".
  const down = {};
  for (const component of COMPONENTS) {
    down[component.id] = { state: "down", since: NOW - 120 };
  }
  assert.ok(page({ components: down }).includes("for 2 minutes"));
});

test("every component and dependency is drawn, in configuration order", () => {
  const html = page();
  let cursor = -1;
  for (const component of COMPONENTS) {
    const at = html.indexOf(component.name);
    assert.ok(at > cursor, `${component.name} is missing or out of order`);
    cursor = at;
  }
  for (const upstream of UPSTREAMS) {
    assert.ok(html.includes(upstream.href), `${upstream.name} has no link to its own page`);
  }
});

test("the page needs no JavaScript to say anything", () => {
  const html = page();
  // One script, same origin, and it only builds the theme picker. Strip it and
  // every word of the verdict is still there.
  assert.equal((html.match(/<script/g) ?? []).length, 1);
  assert.ok(html.includes('<script src="/theme.js"></script>'));
  const withoutScripts = html.replace(/<script[\s\S]*?<\/script>/g, "");
  assert.ok(withoutScripts.includes("All systems operational"));
});

test("history draws one bar a day, and no-data days are their own state", () => {
  const days = ["2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31"];
  const rows = {
    "2026-08-28": { up: 1400, degraded: 0, down: 40, unknown: 0 },
    "2026-08-29": { up: 1440, degraded: 0, down: 0, unknown: 0 },
    // 2026-08-30 is deliberately absent: the page has to survive a day the
    // checker never ran at all, which is every day before it was deployed.
    "2026-08-31": { up: 1240, degraded: 0, down: 200, unknown: 0 },
  };
  const history = { days, byComponent: { verification: rows } };
  const html = page({ history });
  assert.ok(html.includes('title="2026-08-29: 100% up"'));
  assert.ok(html.includes('title="2026-08-30: no data"'));
  assert.ok(html.includes('class="bar is-unknown"'), "a day nobody observed is not green");
  // 40 minutes lost is 97.22%: a rough patch, not a bad day. It used to draw
  // the same red as an outage lasting the whole afternoon, which is what made
  // the strip worth nothing -- see logic.dayUptime.
  assert.ok(html.includes('class="bar is-degraded"'), "a short outage is not a whole red day");
  // 200 minutes is 86.11%, and still is one.
  assert.ok(html.includes('class="bar is-down"'));
  assert.ok(html.includes("4 days ago"));
});

test("a declared maintenance window is drawn and named, not silently dropped", () => {
  const days = ["2026-08-30", "2026-08-31"];
  const rows = {
    // A deploy: fifteen minutes, declared, on an otherwise perfect day.
    "2026-08-30": { up: 1425, degraded: 0, down: 0, unknown: 0, maintenance: 15 },
    "2026-08-31": { up: 0, degraded: 0, down: 0, unknown: 0, maintenance: 1440 },
  };
  const history = { days, byComponent: { verification: rows } };
  const html = page({ history });
  // The day reads 100%, and says why in the same breath. A percentage that
  // quietly skipped a quarter of an hour is how a status page loses its credit.
  assert.ok(html.includes("2026-08-30: 100% up (15 minutes of maintenance not counted)"));
  assert.ok(html.includes('class="bar is-maintenance"'), "planned work has its own colour");
  assert.ok(html.includes('title="2026-08-31: maintenance all day"'));
});

test("the uptime figure is never rounded up to a clean 100%", () => {
  // 99.9965% is not 100%, and a status page that says it is has started lying
  // politely -- on the exact figure people quote at each other.
  const days = ["2026-08-31"];
  const history = {
    days,
    byComponent: {
      verification: { "2026-08-31": { up: 143995, degraded: 0, down: 5, unknown: 0 } },
    },
  };
  const html = page({ history });
  assert.ok(html.includes("99.99%"));
  assert.ok(!html.includes(">100%<"));
});

test("a perfect record is allowed to say 100%", () => {
  const days = ["2026-08-31"];
  const history = {
    days,
    byComponent: { verification: { "2026-08-31": { up: 1440, degraded: 0, down: 0, unknown: 0 } } },
  };
  assert.ok(page({ history }).includes("100.00%"));
});

test("with no history at all the page says so instead of showing a zero", () => {
  const html = page({ history: { days: ["2026-08-31"], byComponent: {} } });
  assert.ok(html.includes("No history yet"));
  assert.ok(!html.includes("0.00%"), "no history is not zero uptime");
});

test("the bars are hidden from assistive technology, and the facts are not", () => {
  const days = ["2026-08-30", "2026-08-31"];
  const history = {
    days,
    byComponent: {
      verification: {
        "2026-08-30": { up: 1440, degraded: 0, down: 0, unknown: 0 },
        "2026-08-31": { up: 1400, degraded: 0, down: 40, unknown: 0 },
      },
    },
  };
  const html = page({ history });
  assert.ok(html.includes('<div class="bars" aria-hidden="true">'));
  // The same information, in a sentence, for anyone the shape is useless to.
  assert.match(html, /visually-hidden">[^<]*uptime over the last 2 days, with 1 day affected/);
});

test("the page does not understate how long a fault takes to appear", () => {
  // It claimed "up to a minute", which was wrong for every row on the page.
  // Nothing is published without two consecutive bad checks, and the rows that
  // report in on their own schedule are slower still -- which is exactly the
  // gap that makes someone stop a service, watch for a minute, see green, and
  // conclude the page is broken.
  const html = page();
  assert.ok(!html.includes("up to a minute old"));
  assert.ok(html.includes("twice in a row"));
  assert.ok(html.includes("about two minutes"));
  assert.ok(html.includes("about four"));
  // And that recovery is not subject to the same wait, because a reader
  // staring at a fixed service wants to know the page is not just slow.
  assert.ok(html.includes("Recoveries are published as soon as they are seen"));
});

test("escaping", () => {
  assert.equal(escapeHtml('<a href="x">&</a>'), "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;");
});

test("one timezone, named", () => {
  assert.equal(utcStamp(NOW), "2026-08-31 18:22 UTC");
});
