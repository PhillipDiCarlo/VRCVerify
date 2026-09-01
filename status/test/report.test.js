/**
 * The homelab's report, as the Worker sees it (issue #170 phase 2).
 *
 * This endpoint is the only route on the whole Worker that writes, and it is
 * reachable by anybody. Everything below is about what it refuses.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

import { parseSignatureHeader, readReport, signatureIsTimely } from "../src/logic.js";
import { PART_CAPABILITIES } from "../src/config.js";

const ALLOWED = Object.keys(PART_CAPABILITIES);

test("a signature header is understood completely or not at all", () => {
  assert.deepEqual(parseSignatureHeader(`t=1788233609,v1=${"a".repeat(64)}`), {
    timestamp: 1788233609,
    signature: "a".repeat(64),
  });
  // Every one of these is "nearly right", which is not a thing a signature
  // gets credit for being.
  assert.equal(parseSignatureHeader(`v1=${"a".repeat(64)}`), null, "no timestamp");
  assert.equal(parseSignatureHeader("t=1788233609"), null, "no signature");
  assert.equal(parseSignatureHeader(`t=nope,v1=${"a".repeat(64)}`), null, "unparseable time");
  assert.equal(parseSignatureHeader(`t=1,v1=${"a".repeat(63)}`), null, "wrong length");
  assert.equal(parseSignatureHeader(`t=1,v1=${"A".repeat(64)}`), null, "not lowercase hex");
  assert.equal(parseSignatureHeader(`t=1,v1=${"z".repeat(64)}`), null, "not hex at all");
  assert.equal(parseSignatureHeader(null), null);
  assert.equal(parseSignatureHeader(""), null);
});

test("a signature expires, in both directions", () => {
  const now = 1788233609;
  assert.ok(signatureIsTimely(now, now));
  assert.ok(signatureIsTimely(now - 299, now));
  assert.ok(!signatureIsTimely(now - 301, now), "a captured report must not replay forever");
  // The homelab's clock is not ours to trust, so the window is symmetric.
  assert.ok(signatureIsTimely(now + 299, now));
  assert.ok(!signatureIsTimely(now + 301, now));
});

test("the Python signer and this verifier agree on the exact bytes", async () => {
  // PINNED IN tests/test_status_reporter.py TOO. The two halves of this
  // protocol are written in different languages and deployed on different
  // schedules by different steps; the only thing keeping them in agreement is
  // that both sides pin the same vector. Change how either signs and both
  // suites fail, which is the point.
  const secret = "not-a-real-secret";
  const timestamp = 1788233609;
  const body = '{"parts":{"discord-bot":{"detail":null,"up":true}}}';
  const expected = "425d79342d2a683b9268d6fe76947fbb3c66389e376494f0b3e833c2833e1f37";

  const encoder = new TextEncoder();
  const key = await webcrypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
  const signed = await webcrypto.subtle.sign("HMAC", key, encoder.encode(`${timestamp}.${body}`));
  const hex = [...new Uint8Array(signed)].map((b) => b.toString(16).padStart(2, "0")).join("");
  assert.equal(hex, expected);

  // And the verify path the Worker actually uses accepts it.
  const bytes = Uint8Array.from(expected.match(/../g).map((b) => parseInt(b, 16)));
  assert.ok(
    await webcrypto.subtle.verify("HMAC", key, bytes, encoder.encode(`${timestamp}.${body}`)),
  );
});

test("a report may only speak about parts this Worker knows", () => {
  const rows = readReport(
    {
      parts: {
        "discord-bot": { up: true, detail: "gateway ready" },
        // Whatever holds the signing key must not be able to invent rows on a
        // public page. Dropped silently rather than rejected: see logic.js.
        "totally-made-up": { up: false, detail: "hello" },
      },
    },
    ALLOWED,
  );
  assert.deepEqual(rows, [{ part: "discord-bot", up: true, detail: "gateway ready" }]);
});

test("a newer reporter's unknown part does not take the known ones down with it", () => {
  // The upgrade case: the reporter learns a new part before the Worker does.
  // Rejecting the whole report would stop the reporting of everything else,
  // which is an outage in the monitoring caused by deploying an improvement.
  const rows = readReport(
    { parts: { "brand-new-thing": { up: true }, queue: { up: false, detail: "closed" } } },
    ALLOWED,
  );
  assert.deepEqual(rows, [{ part: "queue", up: false, detail: "closed" }]);
});

test("malformed reports are rejected rather than half-read", () => {
  assert.equal(readReport(null, ALLOWED), null);
  assert.equal(readReport({}, ALLOWED), null);
  assert.equal(readReport({ parts: "up" }, ALLOWED), null);
  assert.equal(readReport({ parts: {} }, ALLOWED), null);
  // `up` has to be a boolean. "false" and 0 are the two values most likely to
  // arrive from a hand-rolled client and be read as truthy or falsy by
  // accident, so neither is accepted at all.
  assert.equal(readReport({ parts: { queue: { up: "false" } } }, ALLOWED), null);
  assert.equal(readReport({ parts: { queue: { up: 1 } } }, ALLOWED), null);
  assert.equal(readReport({ parts: { queue: {} } }, ALLOWED), null);
});

test("a detail is truncated rather than trusted", () => {
  const rows = readReport(
    { parts: { queue: { up: false, detail: "x".repeat(5000) } } },
    ALLOWED,
  );
  assert.equal(rows[0].detail.length, 200);
  // A non-string detail is dropped, not coerced: `[object Object]` in an alert
  // is worse than no detail at all.
  const coerced = readReport({ parts: { queue: { up: false, detail: { a: 1 } } } }, ALLOWED);
  assert.equal(coerced[0].detail, null);
});
