/**
 * Hand-written incidents (issue #170 phase 5).
 *
 * This is the one place a person can put words on a page that is otherwise
 * entirely measured. THE HEADLINE, THE HERO COLOUR AND THE PILLS ARE NEVER
 * TYPED -- an incident is prose, shown in its own banner, and it must not move
 * any of those either direction. Not better than the rows say (a "resolved"
 * incident cannot paper over a real outage) and not worse either (an
 * operator who opens a "down" incident, out of habit or caution, must not
 * paint five working capabilities red for everyone reading the page). Found
 * by using the feature: the first version let an open incident's impact
 * override the headline, which turned "Verification is slow for some users"
 * into a page-wide red banner over four services that were fine.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { readIncidentForm, readUpdateForm } from "../src/logic.js";
import { renderAdmin, renderPage } from "../src/render.js";
import { COMPONENTS, UPSTREAMS } from "../src/config.js";

const NOW = Date.UTC(2026, 8, 1, 12, 0, 0) / 1000;

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

test("an open 'down' incident does not turn five working services red", () => {
  const html = pageWith([{ id: 1, title: "Slow for some", impact: "down", started_at: NOW - 60, resolved_at: null, updates: [] }]);
  assert.ok(html.includes('class="hero is-up"'), "the hero colour is measured, and nothing was measured down");
  // Every row still reads as measured. This is the whole point: one person's
  // word does not repaint working services.
  assert.equal(
    (html.match(/class="row is-up"/g) ?? []).length,
    COMPONENTS.length + UPSTREAMS.length,
    "every capability and dependency row stays as it was measured",
  );
  assert.ok(!html.includes('class="row is-down"'), "no row is repainted by prose");
  assert.ok(!html.includes('class="row is-degraded"'));
  assert.ok(html.includes("Slow for some"), "the incident is still shown, as information");
});

test("but the headline stops claiming all-clear over an open incident", () => {
  // A green "All systems operational" set directly above a red banner someone
  // wrote to say otherwise is a page arguing with itself, and the largest text
  // on it wins. Found in a screenshot, not in a test.
  const html = pageWith([{ id: 1, title: "Slow for some", impact: "down", started_at: NOW - 60, resolved_at: null, updates: [] }]);
  assert.ok(!html.includes("All systems operational"));
  assert.ok(html.includes("1 open incident"));
});

test("two open incidents are counted, not listed, in the headline", () => {
  const html = pageWith([
    { id: 1, title: "One", impact: "degraded", started_at: NOW - 60, resolved_at: null, updates: [] },
    { id: 2, title: "Two", impact: "down", started_at: NOW - 30, resolved_at: null, updates: [] },
  ]);
  assert.ok(html.includes("2 open incidents"));
  assert.ok(html.includes('class="hero is-up"'), "still measured, still green");
});

test("an open 'degraded' incident is informational only", () => {
  const html = pageWith([{ id: 1, title: "EU latency", impact: "degraded", started_at: NOW - 60, resolved_at: null, updates: [] }]);
  assert.ok(html.includes('class="hero is-up"'));
  assert.ok(html.includes("EU latency"));
});

test("planned maintenance is informational only", () => {
  const html = pageWith([{ id: 1, title: "Scheduled window", impact: "maintenance", started_at: NOW - 60, resolved_at: null, updates: [] }]);
  assert.ok(html.includes('class="hero is-up"'));
  assert.ok(html.includes("Scheduled window"));
});

test("with nothing open, the page says so plainly", () => {
  const html = pageWith([]);
  assert.ok(html.includes("All systems operational"));
});

test("a real outage keeps its own words rather than an incident count", () => {
  const components = {};
  COMPONENTS.forEach((c, i) => {
    components[c.id] = { state: i === 0 ? "down" : "up", since: NOW - 86400 };
  });
  const upstreams = {};
  for (const upstream of UPSTREAMS) upstreams[upstream.id] = { state: "up" };
  const html = renderPage({
    components,
    upstreams,
    history: { days: [], byComponent: {} },
    incidents: [{ id: 1, title: "Minor blip", impact: "degraded", started_at: NOW - 60, resolved_at: null, updates: [] }],
    checkedAt: NOW - 20,
    now: NOW,
    freshness: "fresh",
  });
  assert.ok(html.includes("Some services are down"), "the rows came from evidence, not from a keyboard");
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
