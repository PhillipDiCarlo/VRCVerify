/**
 * Who gets woken up, and for what (issue #170 phase 4).
 *
 * The failure mode for alerting is not missing an outage. It is firing often
 * enough that somebody mutes the channel, after which it misses every outage
 * and nobody notices it has. Most of what follows is about not doing that.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { composeAlert, isAlertable } from "../src/logic.js";
import { COMPONENT_IDS, COMPONENTS, UPSTREAMS } from "../src/config.js";

const NAMES = Object.fromEntries([
  ...COMPONENTS.map((c) => [c.id, c.name]),
  ...UPSTREAMS.map((u) => [u.id, u.name]),
]);
const NOW = 1788233609;

test("our own services alert in both directions", () => {
  // Recovery matters as much as failure. An alert that only fires on the way
  // down leaves somebody refreshing a page to find out whether it is over.
  assert.ok(isAlertable({ component: "verification", from: "up", state: "down" }, COMPONENT_IDS));
  assert.ok(isAlertable({ component: "verification", from: "down", state: "up" }, COMPONENT_IDS));
  assert.ok(isAlertable({ component: "bot", from: "up", state: "degraded" }, COMPONENT_IDS));
});

test("somebody else's minor incident does not wake anybody", () => {
  // Cloudflare's page sits at `minor` for hours over things that never touch
  // us. An alert for those is an alert that gets muted.
  assert.ok(!isAlertable({ component: "cloudflare", from: "up", state: "degraded" }, COMPONENT_IDS));
  // Down, and back from down, still count.
  assert.ok(isAlertable({ component: "cloudflare", from: "up", state: "down" }, COMPONENT_IDS));
  assert.ok(isAlertable({ component: "discord", from: "down", state: "up" }, COMPONENT_IDS));
});

test("not being able to look is never on its own worth an alert", () => {
  // `unknown` means the checker could not see. A page that pages you because
  // it briefly could not see is a page you turn off.
  assert.ok(!isAlertable({ component: "verification", from: "up", state: "unknown" }, COMPONENT_IDS));
  assert.ok(!isAlertable({ component: "discord", from: "up", state: "unknown" }, COMPONENT_IDS));
});

test("one event is one message, however many rows it moved", () => {
  // The database going down takes four capabilities with it. Four alerts for
  // one event is how somebody learns to ignore the fourth.
  const alert = composeAlert(
    [
      { component: "verification", from: "up", state: "down", detail: "database: refused" },
      { component: "bot", from: "up", state: "down", detail: "database: refused" },
      { component: "invites", from: "up", state: "down", detail: "database: refused" },
      { component: "dashboard", from: "up", state: "degraded", detail: "database: refused" },
    ],
    NAMES,
    NOW,
  );
  assert.equal(alert.severity, "down", "the title names the worst thing that happened");
  // And ONLY what matches the word it uses. The dashboard is degraded here,
  // not down, and a title asserting an outage for a service that is still
  // serving pages is a title that gets argued with.
  assert.equal(alert.title, "Down: Verification, Discord bot, Group invites (+1 degraded)");
  assert.equal(alert.lines.length, 4, "and the body lists all of it");
  assert.match(alert.lines[0], /Verification: up -> down -- database: refused/);
});

test("the alert carries the infrastructure detail the page refuses to", () => {
  // This is the whole reason for a private channel. It is also the only part
  // of the message that saves anybody a login.
  const alert = composeAlert(
    [{ component: "invites", from: "up", state: "down", detail: "queue: connection refused" }],
    NAMES,
    NOW,
  );
  assert.match(alert.lines[0], /queue: connection refused/);
});

test("a recovery says so rather than being a silent absence", () => {
  const alert = composeAlert(
    [{ component: "verification", from: "down", state: "up", detail: null }],
    NAMES,
    NOW,
  );
  assert.match(alert.title, /^Recovered/);
  assert.equal(alert.severity, "up");
});

test("a run that only degrades things says degraded", () => {
  const alert = composeAlert(
    [{ component: "dashboard", from: "up", state: "degraded", detail: null }],
    NAMES,
    NOW,
  );
  assert.equal(alert.title, "Degraded: Dashboard and sign-in");
  assert.ok(!alert.title.includes("+0"));
});

test("a run that recovers some things while others stay broken leads with the break", () => {
  const alert = composeAlert(
    [
      { component: "verification", from: "down", state: "up", detail: null },
      { component: "bot", from: "up", state: "down", detail: null },
    ],
    NAMES,
    NOW,
  );
  assert.equal(alert.title, "Down: Discord bot");
  assert.equal(alert.lines.length, 2, "the recovery is still reported, in the body");
  assert.match(alert.lines[0], /Verification: down -> up/);
});

test("nothing changed means nothing is sent", () => {
  assert.equal(composeAlert([], NAMES, NOW), null);
});

test("a component with no friendly name still produces a readable line", () => {
  const alert = composeAlert(
    [{ component: "something-new", from: "up", state: "down", detail: null }],
    NAMES,
    NOW,
  );
  assert.match(alert.lines[0], /^something-new: up -> down/);
});
