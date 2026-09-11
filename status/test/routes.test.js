/**
 * The request path, which until #307 nothing had ever exercised.
 *
 * `hardening.test.js` imports `securityHeaders` out of index.js and stops
 * there. Every other suite tests a pure function or a rendered string, so the
 * routing -- which URL answers, which redirects, which 404s, and what headers
 * come back -- had no coverage at all.
 *
 * That is how #307 shipped. `localeFromPath` resolves a path to a locale
 * case-insensitively and has a unit test proving it; the guard in index.js
 * then compared the ORIGINAL path against the CANONICAL spelling with `===`
 * and threw the answer away, so /pt-br returned 404 in production while the
 * test for the helper passed. A correct function nothing calls looks exactly
 * like a working feature.
 *
 * NO D1 HERE, AND NONE NEEDED. `present()` catches everything the database
 * does and answers `freshness: "unavailable"`, which is the state the page is
 * designed to render honestly -- so a binding that throws on contact is a
 * legitimate fixture, and every assertion below is about the route rather than
 * about the data.
 */

import test from "node:test";
import assert from "node:assert/strict";

import worker from "../src/index.js";
import { DEFAULT_LOCALE, LOCALES, pathForLocale } from "../src/i18n.js";

/** A DB binding that fails the way a real outage would. */
const ENV = {
  DB: {
    prepare() {
      throw new Error("no database in tests, which the page is built to survive");
    },
  },
};

const CTX = { waitUntil() {}, passThroughOnException() {} };

function get(path, headers = {}) {
  return worker.fetch(new Request(`https://status.vrcverify.com${path}`, { headers }), ENV, CTX);
}

// ------------------------------------------------------------ the routes

test("every locale answers at its canonical path", async () => {
  for (const locale of LOCALES) {
    const res = await get(pathForLocale(locale));
    assert.equal(res.status, 200, `${pathForLocale(locale)} did not answer`);
    const body = await res.text();
    assert.ok(
      body.includes(`<html lang="${locale}"`),
      `${pathForLocale(locale)} rendered some other language`,
    );
  }
});

test("a locale path resolves in any casing, by redirecting to the canonical one", async () => {
  // THE #307 REGRESSION. Every one of these 404'd in production while
  // `localeFromPath` had a passing test saying they should not.
  for (const locale of LOCALES) {
    const canonical = pathForLocale(locale);
    for (const spelling of [`/${locale.toLowerCase()}`, `/${locale.toUpperCase()}`]) {
      const res = await get(spelling);
      if (spelling === canonical) {
        assert.equal(res.status, 200, `${spelling} is canonical and should be served`);
        continue;
      }
      assert.equal(res.status, 301, `${spelling} should redirect, not ${res.status}`);
      assert.equal(
        res.headers.get("location"),
        canonical,
        `${spelling} should land on ${canonical}`,
      );
    }
  }
});

test("English has one address and it is the root", async () => {
  // `/en` is a URL somebody constructs by hand after seeing `/ja`. It is
  // answered, but never with a second copy of the page.
  for (const spelling of ["/en", "/EN", "/en/"]) {
    const res = await get(spelling);
    assert.equal(res.status, 301, `${spelling} should redirect`);
    assert.equal(res.headers.get("location"), "/");
  }
  assert.equal((await get("/")).status, 200);
});

test("a trailing slash is the same page, not a redirect", async () => {
  const res = await get("/ja/");
  assert.equal(res.status, 200);
  assert.ok((await res.text()).includes('<html lang="ja"'));
});

test("a path that merely starts with a locale is still a 404", async () => {
  // The trap in the fix: a `path !== canonical` test would redirect these to
  // /ja instead, quietly swallowing a wrong URL rather than reporting it.
  for (const path of ["/ja/api/status.json", "/ja/admin", "/ja/anything", "/de/ja"]) {
    assert.equal((await get(path)).status, 404, `${path} should not resolve`);
  }
});

test("something that is not a locale is a 404", async () => {
  for (const path of ["/nope", "/fr", "/e", "/jaa", "/-", "/%20"]) {
    assert.equal((await get(path)).status, 404, `${path} should not resolve`);
  }
});

// ----------------------------------------------------------- negotiation

test("the root reads Accept-Language and the locale paths do not", async () => {
  const root = await get("/", { "accept-language": "ja,en;q=0.8" });
  assert.ok((await root.text()).includes('<html lang="ja"'));

  // A reader who asked for /de asked for /de. A page that second-guesses that
  // from a header they did not set is a page whose picker does not work.
  const explicit = await get("/de", { "accept-language": "ja,en;q=0.8" });
  assert.ok((await explicit.text()).includes('<html lang="de"'));
});

test("only the root varies on the header it reads", async () => {
  // Vary on a per-language path would split its cache by a header that
  // changes nothing about the answer, on the one page read by a crowd during
  // an outage.
  assert.equal((await get("/")).headers.get("vary"), "Accept-Language");
  assert.equal((await get("/ja")).headers.get("vary"), null);
});

test("every rendered page is cacheable and carries the security headers", async () => {
  for (const path of ["/", "/ja", "/ar"]) {
    const res = await get(path);
    assert.match(res.headers.get("cache-control"), /max-age=\d+/, `${path} is not cacheable`);
    assert.equal(res.headers.get("content-type"), "text/html; charset=utf-8");
    assert.equal(res.headers.get("x-content-type-options"), "nosniff");
  }
});

// ----------------------------------------------- the rest of the surface

test("the JSON API is not a locale route and stays English", async () => {
  const res = await get("/api/status.json");
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /application\/json/);
  const body = await res.json();
  // A contract with whatever parses it. The names are the English ones even
  // when the page beside it is not.
  assert.ok(body.services.some((s) => s.name === "Verification"));
});

test("a method the page does not answer is refused", async () => {
  const res = await worker.fetch(
    new Request("https://status.vrcverify.com/ja", { method: "DELETE" }),
    ENV,
    CTX,
  );
  assert.equal(res.status, 405);
});

test("the page renders when its own storage does not", async () => {
  // The fixture above is a database that throws on contact, so every test in
  // this file has been asserting this implicitly. Said once, explicitly,
  // because it is rule 3 of the page.
  const body = await (await get("/")).text();
  assert.ok(body.includes("Status cannot be read right now"));
  assert.ok(body.includes("says nothing about whether the services are working"));
  assert.ok(!body.includes('class="row is-up"'), "nothing may be drawn green from no data");
});

test("the language in force is the one the picker marks, on every route", async () => {
  for (const locale of LOCALES) {
    const body = await (await get(pathForLocale(locale))).text();
    assert.ok(
      body.includes(`href="${pathForLocale(locale)}" lang="${locale}" class="menu-item current"`),
      `${locale} does not mark itself current`,
    );
  }
  assert.equal(DEFAULT_LOCALE, "en");
});
