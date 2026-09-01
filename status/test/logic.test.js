/**
 * The status page's rules, exercised without a network or a database.
 *
 *   node --test status/test
 *
 * Node's own test runner, with no dependency added to anything. This project
 * has no JavaScript toolchain and should not grow one for four hundred lines
 * of Worker: `node --test` is in the runtime already, and a test suite that
 * needs an install step before it can be run is a test suite that stops being
 * run.
 *
 * WHAT THESE ARE FOR. Nearly every assertion below is a restatement of one
 * rule: the page must never say a service is fine because we failed to find
 * out. Green from missing data is the failure mode that makes a status page
 * worse than no status page, and it is not a thing you notice in review.
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  capabilitiesFromParts,
  classifyHttp,
  dataFreshness,
  dayKey,
  dayUptime,
  humanDuration,
  nextState,
  readStatuspage,
  readStripe,
  recentDays,
  uptimeOverDays,
  verdict,
  worst,
} from "../src/logic.js";
import { PART_CAPABILITIES, HEARTBEAT_STALE_SECONDS } from "../src/config.js";

test("worst() takes the worst state present", () => {
  assert.equal(worst(["up", "up"]), "up");
  assert.equal(worst(["up", "degraded"]), "degraded");
  assert.equal(worst(["degraded", "down"]), "down");
  assert.equal(worst([]), "unknown");
});

test("a real observation outranks not having looked", () => {
  // `unknown` sits below `degraded`, so one silent probe cannot repaint a row
  // full of good ones -- but it still beats `up`, so it can never be hidden
  // behind a healthy sibling either.
  assert.equal(worst(["unknown", "degraded"]), "degraded");
  assert.equal(worst(["unknown", "up"]), "unknown");
});

test("HTTP answers map to states, and a 4xx is not an outage", () => {
  assert.equal(classifyHttp({ ok: true, status: 200 }), "up");
  assert.equal(classifyHttp({ ok: true, status: 204 }), "up");
  assert.equal(classifyHttp({ ok: true, status: 500 }), "down");
  assert.equal(classifyHttp({ ok: true, status: 502 }), "down");
  assert.equal(classifyHttp({ ok: false }), "down");
  // A 403 from the edge is a story about bot protection, not proof the origin
  // has stopped. That confusion is SECURITY_AUDIT A-25 and it cost three days
  // of Stripe webhooks once already.
  assert.equal(classifyHttp({ ok: true, status: 403 }), "degraded");
});

test("Statuspage's four indicators, and anything else", () => {
  assert.equal(readStatuspage({ status: { indicator: "none" } }), "up");
  assert.equal(readStatuspage({ status: { indicator: "minor" } }), "degraded");
  assert.equal(readStatuspage({ status: { indicator: "major" } }), "down");
  assert.equal(readStatuspage({ status: { indicator: "critical" } }), "down");
  assert.equal(readStatuspage({ status: { indicator: "maintenance" } }), "unknown");
  assert.equal(readStatuspage({}), "unknown");
  assert.equal(readStatuspage(null), "unknown");
});

test("Stripe's undocumented feed, including the day it changes shape", () => {
  const services = ["api", "webhooks", "checkout"];
  const all = (value) => ({ statuses: { api: value, webhooks: value, checkout: value } });
  assert.equal(readStripe(all("up"), services), "up");
  assert.equal(readStripe(all("down"), services), "down");
  assert.equal(readStripe(all("degraded"), services), "degraded");
  assert.equal(
    readStripe({ statuses: { api: "up", webhooks: "degraded", checkout: "up" } }, services),
    "degraded",
  );
  // Only the services this project uses are read. Their support site being
  // down is not our outage.
  assert.equal(
    readStripe({ statuses: { api: "up", webhooks: "up", checkout: "up", supportsite: "down" } }, services),
    "up",
  );
  // status.stripe.com/current is undocumented and carries no promise. If it
  // stops answering in a shape we recognise, the answer is "we do not know",
  // never "fine".
  assert.equal(readStripe({ statuses: {} }, services), "unknown");
  assert.equal(readStripe({ largestatus: "up" }, services), "unknown");
  assert.equal(readStripe(null, services), "unknown");
});

test("going down takes two observations; coming back takes one", () => {
  const now = 1_000_000;
  const up = { state: "up", since: now - 500, pending: null, pendingN: 0 };

  const first = nextState(up, "down", now);
  assert.equal(first.state, "up", "one failed probe is not an outage");
  assert.equal(first.changed, false);
  assert.equal(first.pending, "down");

  const second = nextState({ ...up, pending: "down", pendingN: 1 }, "down", now + 60);
  assert.equal(second.state, "down");
  assert.equal(second.changed, true);
  assert.equal(second.since, now + 60);

  // Recovery is published immediately. Being slow to say "down" costs a
  // minute; being slow to say "back" costs the page its credibility with
  // somebody who can see the service working.
  const back = nextState(second, "up", now + 120);
  assert.equal(back.state, "up");
  assert.equal(back.changed, true);
});

test("a flap never reaches the page", () => {
  const now = 1_000_000;
  let state = { state: "up", since: now - 900, pending: null, pendingN: 0 };
  for (let i = 0; i < 6; i += 1) {
    // One bad minute, one good minute, forever. Every bad observation is the
    // first of its run, so nothing is ever published.
    state = nextState(state, "down", now + i * 120);
    assert.equal(state.state, "up");
    state = nextState(state, "up", now + i * 120 + 60);
    assert.equal(state.state, "up");
    assert.equal(state.changed, false, "recovering to the state it already held is not a change");
  }
});

test("leaving unknown is published at once", () => {
  const now = 2_000_000;
  const unknown = { state: "unknown", since: now - 300, pending: null, pendingN: 0 };
  // A row saying "we cannot tell" has no credibility to protect by waiting,
  // so even bad news replaces it immediately.
  const decided = nextState(unknown, "down", now);
  assert.equal(decided.state, "down");
  assert.equal(decided.changed, true);
});

test("a part that has never reported does not drag its capabilities down", () => {
  // This is what phase 1 looks like in production: the reporter does not exist
  // yet, so no part has ever spoken. Nothing should be claimed about anything.
  const result = capabilitiesFromParts({}, PART_CAPABILITIES, 1000, HEARTBEAT_STALE_SECONDS);
  assert.deepEqual(result, {});
});

test("a heartbeat that stops is an outage, not a mystery", () => {
  const now = 1_700_000_000;
  const beats = {
    "vrc-online-checker": { at: now - (HEARTBEAT_STALE_SECONDS + 1), up: true },
    "discord-bot": { at: now - 30, up: true },
  };
  const result = capabilitiesFromParts(beats, PART_CAPABILITIES, now, HEARTBEAT_STALE_SECONDS);
  // Silence is the only signal the homelab can send about being on fire, so it
  // has to mean `down` rather than `unknown`.
  assert.equal(result.verification.state, "down");
  assert.equal(result.bot.state, "up");
});

test("one broken part shows up as every capability a reader would notice", () => {
  const now = 1_700_000_000;
  const beats = {
    queue: { at: now - 10, up: false, detail: "connection refused" },
    "discord-bot": { at: now - 10, up: true },
    "vrc-online-checker": { at: now - 10, up: true },
    "vrc-group-inviter": { at: now - 10, up: true },
  };
  const result = capabilitiesFromParts(beats, PART_CAPABILITIES, now, HEARTBEAT_STALE_SECONDS);
  assert.equal(result.verification.state, "down");
  assert.equal(result.invites.state, "down");
  // The detail names the part, which is exactly why it is private and never
  // rendered. See render.test.js.
  assert.match(result.verification.detail, /queue/);
});

test("the homelab going quiet degrades the dashboard rather than downing it", () => {
  // Both facts are true at once: dashboard.vrcverify.com answers 200, and
  // every page needing the bot behind it fails. A reader watching the site
  // load while this page calls it Down stops believing the rows that are
  // right, so the public probe stays the authority on reachability and the
  // homelab can only pull the row to degraded.
  const now = 1_700_000_000;
  const dead = now - (HEARTBEAT_STALE_SECONDS + 100);
  const result = capabilitiesFromParts(
    { "bot-api": { at: dead, up: true }, database: { at: dead, up: true } },
    PART_CAPABILITIES,
    now,
    HEARTBEAT_STALE_SECONDS,
  );
  assert.equal(result.dashboard.state, "degraded");
  // The rows with nothing else speaking for them are still down, because for
  // those the homelab IS the only authority.
  assert.equal(result.verification.state, "down");
  assert.equal(result.bot.state, "down");
});

test("the headline never says everything is fine when something is unknown", () => {
  assert.equal(verdict(["up", "up", "up"]).headline, "All systems operational");
  assert.equal(verdict(["up", "unknown"]).level, "unknown");
  assert.notEqual(verdict(["up", "unknown"]).headline, "All systems operational");
  assert.equal(verdict(["up", "degraded"]).level, "degraded");
  assert.equal(verdict(["up", "down", "unknown"]).level, "down");
  assert.equal(verdict(["down", "down"]).headline, "Everything is down");
  assert.equal(verdict(["down", "up"]).headline, "Some services are down");
});

test("minutes we could not observe are not counted as downtime", () => {
  assert.deepEqual(dayUptime({ up: 1440, degraded: 0, down: 0, unknown: 0 }), {
    percent: 100,
    state: "up",
  });
  // 1380 observed, all up, 60 unknown. The day was not 95.8% up: it was 100%
  // up across every minute anybody actually looked.
  assert.deepEqual(dayUptime({ up: 1380, degraded: 0, down: 0, unknown: 60 }), {
    percent: 100,
    state: "up",
  });
  // A day with nothing but silence has no percentage at all. Averaging over no
  // observations produces a number that looks like measurement and is not.
  assert.deepEqual(dayUptime({ up: 0, degraded: 0, down: 0, unknown: 1440 }), {
    percent: null,
    state: "unknown",
  });
  assert.equal(dayUptime(undefined).state, "unknown");
  assert.equal(dayUptime({ up: 1400, degraded: 0, down: 40, unknown: 0 }).state, "down");
  assert.equal(dayUptime({ up: 1400, degraded: 40, down: 0, unknown: 0 }).state, "degraded");
});

test("uptime across days skips the days with no observations", () => {
  assert.equal(uptimeOverDays([{ up: 100, degraded: 0, down: 0, unknown: 0 }]), 100);
  assert.equal(
    uptimeOverDays([
      { up: 90, degraded: 0, down: 10, unknown: 0 },
      { up: 0, degraded: 0, down: 0, unknown: 1440 },
    ]),
    90,
  );
  assert.equal(uptimeOverDays([]), null);
  assert.equal(uptimeOverDays([{ up: 0, degraded: 0, down: 0, unknown: 5 }]), null);
});

test("days are UTC and run oldest first", () => {
  const noon = Date.UTC(2026, 7, 31, 12, 0, 0) / 1000;
  assert.equal(dayKey(noon), "2026-08-31");
  const days = recentDays(noon, 3);
  assert.deepEqual(days, ["2026-08-29", "2026-08-30", "2026-08-31"]);
});

test("data older than five minutes is not to be believed", () => {
  const now = 1_700_000_000;
  assert.equal(dataFreshness(now - 30, now), "fresh");
  assert.equal(dataFreshness(now - 299, now), "fresh");
  assert.equal(dataFreshness(now - 301, now), "stale");
  assert.equal(dataFreshness(null, now), "missing");
  assert.equal(dataFreshness(undefined, now), "missing");
});

test("durations are coarse, because the data is", () => {
  assert.equal(humanDuration(10), "less than a minute");
  assert.equal(humanDuration(60), "1 minute");
  assert.equal(humanDuration(3600), "1 hour");
  assert.equal(humanDuration(7200), "2 hours");
  assert.equal(humanDuration(86400 * 3), "3 days");
  assert.equal(humanDuration(NaN), "less than a minute");
});
