/**
 * What the adversarial pass found, pinned so it cannot come back (#170).
 *
 * Everything here was written AFTER the feature worked, by attacking it rather
 * than by re-running its tests. Each test fails against the code as it was
 * before the fix beside it.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { isDuplicateRun, isSameOriginPost } from "../src/logic.js";

const URL_HERE = "https://status.vrcverify.com/admin";

test("an Access assertion is not consent: a cross-origin POST is refused", () => {
  // Access injects a valid JWT header on any request carrying its cookie,
  // including a form POST from an attacker's page. Without this check, that
  // publishes an announcement on a page people trust -- and the interesting
  // payload is not vandalism, it is "we have been breached, sign in again at".
  assert.ok(
    !isSameOriginPost({ origin: "https://evil.example", referer: null, url: URL_HERE }),
  );
  assert.ok(
    !isSameOriginPost({
      // A prefix of our own name is not our own name.
      origin: "https://status.vrcverify.com.evil.example",
      referer: null,
      url: URL_HERE,
    }),
  );
  assert.ok(!isSameOriginPost({ origin: "http://status.vrcverify.com", referer: null, url: URL_HERE }),
    "the scheme is part of the origin");
});

test("our own form still works", () => {
  assert.ok(
    isSameOriginPost({ origin: "https://status.vrcverify.com", referer: null, url: URL_HERE }),
  );
});

test("referer is the fallback, and it is matched as an origin not a substring", () => {
  assert.ok(
    isSameOriginPost({
      origin: null,
      referer: "https://status.vrcverify.com/admin",
      url: URL_HERE,
    }),
  );
  assert.ok(
    !isSameOriginPost({
      origin: null,
      // The classic near-miss: our origin appears, as somebody else's path.
      referer: "https://evil.example/https://status.vrcverify.com/",
      url: URL_HERE,
    }),
  );
});

test("a request carrying neither header is refused rather than assumed friendly", () => {
  // One person uses this endpoint, from a browser, a few times a year. There
  // is no compatibility worth buying by being generous here.
  assert.ok(!isSameOriginPost({ origin: null, referer: null, url: URL_HERE }));
});

test("the same scheduled minute delivered twice is counted once", () => {
  // Cloudflare's cron delivery is at least once. Without this, a redelivery
  // counts the minute twice and can send the alert twice, and an alert that
  // sometimes arrives in pairs is one people stop reading carefully.
  const now = 1788242330;
  assert.ok(isDuplicateRun(String(now), now), "the redelivery is recognised");
  assert.ok(!isDuplicateRun(String(now - 60), now), "the next minute is not a duplicate");
  assert.ok(!isDuplicateRun(null, now), "the first run ever is not a duplicate");
  assert.ok(!isDuplicateRun(undefined, now));
});
