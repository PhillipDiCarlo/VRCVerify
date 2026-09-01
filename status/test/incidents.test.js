/**
 * Hand-written incidents (issue #170 phase 5).
 *
 * This is the one place a person can put words on a page that is otherwise
 * entirely measured. The rules below are about keeping those two things in the
 * right relationship: prose may make the page look WORSE than the
 * measurements, because a person can see things the probes cannot, and it may
 * never make it look better.
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  readIncidentForm,
  readUpdateForm,
  verdictWithIncidents,
} from "../src/logic.js";
import { renderAdmin, renderPage } from "../src/render.js";
import { COMPONENTS, UPSTREAMS } from "../src/config.js";

const NOW = Date.UTC(2026, 8, 1, 12, 0, 0) / 1000;
const ALL_UP = COMPONENTS.map(() => "up");

function pageWith(incidents) {
  const components = {};
  for (const component of COMPONENTS) components[component.id] = { state: "up", since: NOW - 86400 };
  const upstreams = {};
  for (const upstream of UPSTREAMS) upstreams[upstream.id] = { state: "up" };
  return renderPage({
    components,
    upstreams,
    history: { days: [], byComponent: {} },
    incidents,
    checkedAt: NOW - 20,
    now: NOW,
    freshness: "fresh",
  });
}

test("an open incident stops the page claiming everything is fine", () => {
  // "Verification is slow for European members" is true, invisible from here,
  // and exactly what somebody types in at 3am.
  const result = verdictWithIncidents(ALL_UP, [
    { impact: "degraded", resolved_at: null },
  ]);
  assert.equal(result.level, "degraded");
  assert.notEqual(result.headline, "All systems operational");
});

test("prose cannot make the page look better than the measurements", () => {
  // A "degraded" incident during a real outage leaves the outage showing. The
  // rows came from evidence; the incident came from a keyboard.
  const measured = ["down", "up", "up", "up", "up"];
  const result = verdictWithIncidents(measured, [{ impact: "degraded", resolved_at: null }]);
  assert.equal(result.level, "down");
});

test("a resolved incident stops affecting the headline", () => {
  const result = verdictWithIncidents(ALL_UP, [
    { impact: "down", resolved_at: NOW - 3600 },
  ]);
  assert.equal(result.headline, "All systems operational");
});

test("planned maintenance says maintenance rather than implying a fault", () => {
  const result = verdictWithIncidents(ALL_UP, [{ impact: "maintenance", resolved_at: null }]);
  assert.equal(result.headline, "Maintenance in progress");
  assert.equal(result.level, "degraded");
});

test("the banner shows the newest update first and says how long it has run", () => {
  const html = pageWith([
    {
      id: 1,
      title: "Verification is slow",
      impact: "degraded",
      started_at: NOW - 7200,
      resolved_at: null,
      updates: [
        { at: NOW - 600, status: "identified", body: "A queue backlog. Draining now." },
        { at: NOW - 7200, status: "investigating", body: "We are looking into it." },
      ],
    },
  ]);
  assert.ok(html.includes("Verification is slow"));
  assert.ok(html.includes("Ongoing for 2 hours"));
  assert.ok(
    html.indexOf("Draining now") < html.indexOf("We are looking into it"),
    "somebody arriving mid-incident wants the current position, not the archaeology",
  );
});

test("a resolved incident moves out of the banner and into the history", () => {
  const html = pageWith([
    {
      id: 1,
      title: "The dashboard was unreachable",
      impact: "down",
      started_at: NOW - 10800,
      resolved_at: NOW - 3600,
      updates: [{ at: NOW - 3600, status: "resolved", body: "Fixed." }],
    },
  ]);
  assert.ok(html.includes("Recent incidents"));
  assert.ok(html.includes("Resolved after 2 hours"));
  assert.ok(html.includes("All systems operational"), "a closed incident is not a live one");
  // And it is drawn neutral rather than in the colour of the trouble it used
  // to be. A red card for something fixed hours ago is alarming at a glance
  // and wrong the moment anybody reads the date.
  assert.ok(html.includes("incident is-resolved"));
  assert.ok(!html.includes('class="card incident is-down"'));
  assert.ok(html.includes(">Resolved<"));
});

test("an incident's words are escaped like anything else a person typed", () => {
  const html = pageWith([
    {
      id: 1,
      title: '<script>alert("x")</script>',
      impact: "down",
      started_at: NOW - 60,
      resolved_at: null,
      updates: [{ at: NOW - 60, status: "investigating", body: "<img src=x onerror=y>" }],
    },
  ]);
  assert.ok(!html.includes("<script>alert"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("an incident form is validated rather than trusted", () => {
  assert.deepEqual(
    readIncidentForm({ title: " Slow ", body: " Looking into it ", impact: "degraded" }),
    { title: "Slow", body: "Looking into it", impact: "degraded" },
  );
  assert.equal(readIncidentForm({ title: "", body: "x", impact: "down" }), null);
  assert.equal(readIncidentForm({ title: "   ", body: "x", impact: "down" }), null, "whitespace is not a title");
  assert.equal(readIncidentForm({ title: "x", body: "", impact: "down" }), null);
  assert.equal(readIncidentForm({ title: "x", body: "y", impact: "catastrophe" }), null);
  assert.equal(readIncidentForm({ title: "x".repeat(121), body: "y", impact: "down" }), null);
  assert.equal(readIncidentForm({ title: "x", body: "y".repeat(2001), impact: "down" }), null);
});

test("an update form is validated rather than trusted", () => {
  assert.deepEqual(
    readUpdateForm({ incident_id: "7", body: "Draining", status: "identified" }),
    { incidentId: 7, body: "Draining", status: "identified" },
  );
  assert.equal(readUpdateForm({ incident_id: "nope", body: "x", status: "identified" }), null);
  assert.equal(readUpdateForm({ incident_id: "-1", body: "x", status: "identified" }), null);
  assert.equal(readUpdateForm({ incident_id: "1", body: "x", status: "made-up" }), null);
  assert.equal(readUpdateForm({ incident_id: "1", body: "  ", status: "resolved" }), null);
});

test("the admin page works with no JavaScript and says who is signed in", () => {
  const html = renderAdmin({ incidents: [], who: "someone@example.com", now: NOW });
  assert.ok(html.includes("someone@example.com"));
  assert.ok(html.includes('<form method="post"'));
  // One script, and it is the theme picker. The form must not need it.
  assert.equal((html.match(/<script/g) ?? []).length, 1);
  assert.ok(html.includes('<meta name="robots" content="noindex">'));
});

test("the admin page warns that posting is immediately public", () => {
  const html = renderAdmin({ incidents: [], who: "someone@example.com", now: NOW });
  assert.ok(html.includes("public"));
});
