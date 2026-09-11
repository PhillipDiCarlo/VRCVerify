/**
 * Twelve languages, and the four ways they go wrong quietly (#300).
 *
 * A missing translation is loud -- the English shows -- so it is the LEAST
 * dangerous failure here and the one this file spends the least on. The ones
 * worth tests are the silent ones:
 *
 *   * a msgid that reaches the page without reaching the catalogs, so eleven
 *     readers get an English sentence in the middle of their own,
 *   * a translation that dropped a placeholder, so a sentence about a number
 *     renders with no number in it and reads as though nothing happened,
 *   * a plural entry missing a category its language actually uses, which
 *     shows up only at the counts that select it, and
 *   * a value interpolated without escaping, which is a script tag typed into
 *     the admin form.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { CATALOGS } from "../src/locales/index.js";
import { MSGIDS, PLURAL_MSGIDS, SINGULAR_MSGIDS } from "../src/locales/msgids.js";
import {
  DEFAULT_LOCALE,
  ENDONYMS,
  LOCALES,
  direction,
  localeFromPath,
  negotiate,
  pathForLocale,
  translator,
} from "../src/i18n.js";
import { renderPage } from "../src/render.js";
import { COMPONENTS, HISTORY_DAYS, UPSTREAMS } from "../src/config.js";
import { recentDays } from "../src/logic.js";

const TRANSLATED = LOCALES.filter((code) => code !== DEFAULT_LOCALE);
const NOW = Date.UTC(2026, 7, 31, 18, 22, 0) / 1000;

/** `%{name}` placeholders in a message, as a sorted list. */
function placeholders(message) {
  return [...message.matchAll(/%\{(\w+)\}/g)].map((m) => m[1]).sort();
}

/**
 * Counts that actually select each of a language's plural categories.
 *
 * Asking `Intl` for `pluralCategories` over-demands: Spanish declares `many`,
 * which only fires for compact decimals like "1 millón" and can never be
 * reached by a count of days. So the requirement is what these integers
 * produce, which is what the page can produce, plus `other` -- the fallback
 * ../src/i18n.js drops to when a category is missing.
 */
const PROBES = [0, 1, 2, 3, 5, 7, 11, 21, 25, 30, 60, 90, 101];

function categoriesNeeded(locale) {
  const rules = new Intl.PluralRules(locale);
  return new Set([...PROBES.map((n) => rules.select(n)), "other"]);
}

// ---------------------------------------------------------------- catalogs

test("every catalog covers every msgid, and holds nothing else", () => {
  for (const locale of TRANSLATED) {
    const catalog = CATALOGS[locale];
    assert.ok(catalog, `${locale} has no catalog`);
    const missing = MSGIDS.filter((id) => !(id in catalog));
    assert.deepEqual(missing, [], `${locale} is missing ${missing.length} msgids`);
    // Extras are not harmless: a catalog entry nothing looks up is either a
    // typo in the key, in which case the real msgid is untranslated, or a
    // sentence deleted from the page that nobody swept up.
    const orphans = Object.keys(catalog).filter((id) => !MSGIDS.includes(id));
    assert.deepEqual(orphans, [], `${locale} has entries nothing asks for`);
  }
});

test("English has no catalog, because its catalog is the msgids", () => {
  assert.equal(CATALOGS[DEFAULT_LOCALE], undefined);
  const t = translator(DEFAULT_LOCALE);
  for (const id of SINGULAR_MSGIDS) assert.equal(t(id), id);
});

test("a translation keeps every placeholder its English has, and invents none", () => {
  // `%{count}` IS THE ONE EXEMPTION, and only inside a plural entry. Several
  // languages write small numbers out rather than setting them as a figure --
  // Arabic's `one` is "يوم واحد" and its `two` is "يومان", neither of which
  // has anywhere to put a numeral, and CLDR's own data does the same. Every
  // other placeholder is a value the sentence cannot be true without, so
  // dropping one is a bug in any language.
  for (const locale of TRANSLATED) {
    for (const [msgid, entry] of Object.entries(CATALOGS[locale])) {
      const wanted = placeholders(msgid);
      const plural = typeof entry === "object";
      const required = plural ? wanted.filter((name) => name !== "count") : wanted;
      const forms = plural ? entry : { "": entry };
      for (const [category, text] of Object.entries(forms)) {
        const got = placeholders(text);
        const where = `${locale} ${category ? `[${category}] ` : ""}${JSON.stringify(msgid)}`;
        for (const name of required) {
          assert.ok(got.includes(name), `${where} dropped %{${name}}`);
        }
        const invented = got.filter((name) => !wanted.includes(name));
        assert.deepEqual(invented, [], `${where} invented ${JSON.stringify(invented)}`);
      }
      // The exemption is per form, not per entry: a plural that spells the
      // number out in EVERY form has lost the count altogether, which is a
      // different bug and this catches it.
      if (plural && wanted.includes("count")) {
        assert.ok(
          Object.values(entry).some((text) => placeholders(text).includes("count")),
          `${locale} ${JSON.stringify(msgid)} has no form that shows the count`,
        );
      }
    }
  }
});

test("a plural entry carries every category its language can reach", () => {
  for (const locale of TRANSLATED) {
    const needed = categoriesNeeded(locale);
    for (const msgid of PLURAL_MSGIDS) {
      const entry = CATALOGS[locale][msgid];
      assert.equal(typeof entry, "object", `${locale} ${msgid} is not a plural entry`);
      for (const category of needed) {
        assert.ok(
          typeof entry[category] === "string" && entry[category].length > 0,
          `${locale} ${JSON.stringify(msgid)} has no "${category}" form`,
        );
      }
    }
  }
});

test("Arabic really does need six forms, and really does have them", () => {
  // A guard on the guard. If `categoriesNeeded` ever silently returned an
  // empty set the loop above would pass for every language at once, and this
  // is the case where that would be most obviously wrong.
  assert.deepEqual(
    [...categoriesNeeded("ar")].sort(),
    ["few", "many", "one", "other", "two", "zero"],
  );
});

test("every literal handed to t() in render.js is in the inventory", () => {
  // The half of the inventory that cannot be derived. msgids.js reads the
  // component rows, the verdict sentences and the state words out of the
  // modules that own them; the rest are typed into render.js, and this is what
  // stops one being typed there and nowhere else.
  const source = readFileSync(new URL("../src/render.js", import.meta.url), "utf8");
  const STRING = /"((?:[^"\\]|\\.)*)"/g;
  const RUN = /\bt(?:\.plural)?\(\s*(?:[^,()]+,\s*)?((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+)/g;
  const found = new Set();
  for (const match of source.matchAll(RUN)) {
    const joined = [...match[1].matchAll(STRING)]
      .map((m) => m[1].replace(/\\"/g, '"').replace(/\\\\/g, "\\"))
      .join("");
    if (joined) found.add(joined);
  }
  assert.ok(found.size > 20, `the extractor found only ${found.size} strings, so it is broken`);
  const unlisted = [...found].filter((id) => !MSGIDS.includes(id));
  assert.deepEqual(unlisted, [], "render.js says things no catalog knows about");
});

// ------------------------------------------------------------ negotiation

test("Accept-Language picks the best of the twelve, or English", () => {
  assert.equal(negotiate("ja"), "ja");
  assert.equal(negotiate("ja-JP"), "ja");
  assert.equal(negotiate("de-AT,de;q=0.9,en;q=0.8"), "de");
  // Region is dropped before matching, so European Portuguese reaches the
  // Brazilian catalog. Not ideal Portuguese for a reader in Lisbon, and a
  // great deal better than English, which is the only other thing on offer.
  assert.equal(negotiate("pt-PT"), "pt-BR");
  assert.equal(negotiate("zh-Hans-CN"), "zh-CN");
  // A language nobody here speaks.
  assert.equal(negotiate("fr-CA,fr"), DEFAULT_LOCALE);
  assert.equal(negotiate("*"), DEFAULT_LOCALE);
  assert.equal(negotiate(""), DEFAULT_LOCALE);
  assert.equal(negotiate(null), DEFAULT_LOCALE);
});

test("a quality value the reader actually sent is honored", () => {
  // "German, but I would rather have English" is an instruction, and a page
  // that hands over German anyway has ignored the only one it was given.
  assert.equal(negotiate("de;q=0.9, en;q=1.0"), DEFAULT_LOCALE);
  assert.equal(negotiate("en;q=0.1, ja;q=0.9"), "ja");
  // q=0 means "not this one".
  assert.equal(negotiate("ja;q=0"), DEFAULT_LOCALE);
});

test("a malformed header is answered, not thrown at", () => {
  for (const header of [";;;", "ja;q=", "ja;q=banana", ",,", "   "]) {
    assert.ok(LOCALES.includes(negotiate(header)), `${JSON.stringify(header)} broke negotiation`);
  }
});

test("paths and locales round trip, case insensitively", () => {
  for (const locale of TRANSLATED) {
    assert.equal(localeFromPath(pathForLocale(locale)), locale);
    assert.equal(localeFromPath(pathForLocale(locale).toLowerCase()), locale);
  }
  assert.equal(pathForLocale(DEFAULT_LOCALE), "/");
  assert.equal(localeFromPath("/"), null);
  assert.equal(localeFromPath("/api/status.json"), null);
  assert.equal(localeFromPath("/admin"), null);
});

test("every locale has an endonym, in its own script", () => {
  for (const locale of LOCALES) {
    assert.ok(ENDONYMS[locale], `${locale} has no endonym`);
  }
  assert.equal(Object.keys(ENDONYMS).length, LOCALES.length, "an endonym names no locale");
  // Not the English name. "Japanese" is no help to somebody looking for the
  // word they would recognize, which is the entire argument for endonyms.
  assert.equal(ENDONYMS.ja, "日本語");
  assert.equal(ENDONYMS.ar, "العربية");
});

test("Arabic is the one language drawn right to left", () => {
  assert.equal(direction("ar"), "rtl");
  for (const locale of LOCALES.filter((c) => c !== "ar")) {
    assert.equal(direction(locale), "ltr", `${locale} should be ltr`);
  }
});

// --------------------------------------------------------------- the page

function page(locale, overrides = {}) {
  const components = {};
  for (const component of COMPONENTS) components[component.id] = { state: "up", since: NOW - 7200 };
  const upstreams = {};
  for (const upstream of UPSTREAMS) upstreams[upstream.id] = { state: "up" };
  const days = recentDays(NOW, HISTORY_DAYS);
  const byComponent = {};
  for (const component of COMPONENTS) {
    byComponent[component.id] = Object.fromEntries(
      days.map((day, i) => [day, i === 3 ? { up: 1200, down: 240 } : { up: 1440 }]),
    );
  }
  return renderPage({
    t: translator(locale),
    components,
    upstreams,
    history: { days, byComponent },
    incidents: [],
    checkedAt: NOW - 45,
    now: NOW,
    freshness: "fresh",
    ...overrides,
  });
}

test("every locale renders, and says which one it is", () => {
  for (const locale of LOCALES) {
    const html = page(locale);
    assert.ok(
      html.includes(`<html lang="${locale}" dir="${direction(locale)}">`),
      `${locale} did not stamp lang and dir`,
    );
    // `lang="en"` on a page rendered in Japanese is not a harmless
    // inaccuracy: it is what a screen reader picks its voice from, and what
    // the browser picks hyphenation and quote marks from.
    assert.ok(html.length > 20000, `${locale} rendered ${html.length} bytes, which is too few`);
  }
});

test("no English prose survives into a translated page", () => {
  // Derived rather than listed: every msgid this locale actually translates
  // differently must be absent from its rendered page. Short msgids are
  // skipped because they collide with markup and URLs -- "Down" is a
  // substring of nothing here, but "Home" and "Contact" are the kind of word
  // that turns this into a flaky test rather than a useful one.
  for (const locale of TRANSLATED) {
    const html = page(locale);
    const leaked = SINGULAR_MSGIDS.filter((msgid) => {
      if (msgid.length < 12) return false;
      if (placeholders(msgid).length) return false;
      const translated = CATALOGS[locale][msgid];
      if (typeof translated !== "string" || translated === msgid) return false;
      return html.includes(msgid);
    });
    assert.deepEqual(leaked, [], `${locale} still shows English for ${leaked.length} messages`);
  }
});

test("the picker offers all twelve and marks the one in force", () => {
  for (const locale of LOCALES) {
    const html = page(locale);
    for (const other of LOCALES) {
      assert.ok(
        html.includes(`href="${pathForLocale(other)}" lang="${other}"`),
        `${locale}'s picker does not offer ${other}`,
      );
    }
    assert.equal(
      (html.match(/aria-current="true"/g) ?? []).length,
      1,
      `${locale} marks something other than exactly one language as current`,
    );
    assert.ok(
      html.includes(`href="${pathForLocale(locale)}" lang="${locale}" class="menu-item current"`),
      `${locale} does not mark itself current`,
    );
  }
});

test("the picker needs no JavaScript", () => {
  // The whole page is held to this and the picker is the newest thing on it.
  // A <details> and twelve links; anything else here would be a control that
  // cannot be used during the outage this page exists to describe.
  const html = page("ja");
  const picker = html.slice(html.indexOf("<details class=\"langpick\""), html.indexOf("</details>"));
  assert.ok(picker.includes("<summary"));
  assert.ok(!picker.includes("<script"));
  assert.ok(!picker.includes("onclick"));
  assert.ok(!picker.includes("<form"), "a form would need a route; a link needs nothing");
});

test("each language advertises the other eleven to a crawler", () => {
  // Without these, twelve URLs carrying the same page are twelve competing
  // duplicates -- the problem `workers_dev = false` was written about on the
  // apex Worker, arriving by a different road.
  for (const locale of LOCALES) {
    const html = page(locale);
    assert.ok(
      html.includes(
        `<link rel="canonical" href="https://status.vrcverify.com${pathForLocale(locale)}">`,
      ),
      `${locale} has the wrong canonical`,
    );
    for (const other of LOCALES) {
      assert.ok(
        html.includes(
          `<link rel="alternate" hreflang="${other}" ` +
            `href="https://status.vrcverify.com${pathForLocale(other)}">`,
        ),
        `${locale} does not point at ${other}`,
      );
    }
    assert.ok(html.includes('hreflang="x-default" href="https://status.vrcverify.com/">'));
  }
});

test("a person's words are escaped in every language", () => {
  // An incident title and body are what an operator typed into the admin form
  // at 3am. They are interpolated into translated sentences now, which is a
  // new path for them to travel, so it gets its own test.
  const nasty = '<script>alert("x")</script>';
  for (const locale of ["en", "ja", "ar", "ru"]) {
    const html = page(locale, {
      incidents: [
        {
          id: 1,
          title: nasty,
          impact: "degraded",
          started_at: NOW - 3600,
          resolved_at: null,
          updates: [{ status: "investigating", at: NOW - 3500, body: nasty }],
        },
      ],
    });
    assert.ok(!html.includes(nasty), `${locale} let a script tag through`);
    assert.ok(html.includes("&lt;script&gt;"), `${locale} did not escape it`);
  }
});

test("a message is trusted markup, so no catalog may contain any", () => {
  // `t` passes the MESSAGE through untouched and escapes only the values, so a
  // sentence can carry `<a href=...>` where the English does. That makes every
  // catalog value trusted markup by construction, and the guard on that is
  // that none of them contains any: an unbalanced quote in one of eleven
  // translation files would otherwise end an attribute, and the two places
  // this lands in one are the history strip's and the uptime row's `title`.
  for (const locale of TRANSLATED) {
    for (const [msgid, entry] of Object.entries(CATALOGS[locale])) {
      const texts = typeof entry === "string" ? [entry] : Object.values(entry);
      for (const text of texts) {
        assert.ok(
          !/["<>&]/.test(text),
          `${locale} ${JSON.stringify(msgid)} contains markup: ${JSON.stringify(text)}`,
        );
      }
    }
  }
});

test("a status the form could never have written is escaped, not looked up", () => {
  // The four are an enum and `readUpdateForm` rejects anything else, so this
  // is defense in depth: a row holding something unexpected must not reach
  // `t` as a msgid, because a msgid is inserted unescaped.
  const html = page("de", {
    incidents: [
      {
        id: 1,
        title: "Something",
        impact: "degraded",
        started_at: NOW - 60,
        resolved_at: null,
        updates: [{ status: '<img src=x onerror=alert(1)>', at: NOW - 30, body: "hi" }],
      },
    ],
  });
  assert.ok(!html.includes("<img src=x"), "an unknown status was rendered as markup");
  assert.ok(html.includes("&lt;img src=x"));
});

test("an incident's own words are never translated", () => {
  // Machine translating a person's account of what is broken is how a status
  // page ends up saying something nobody agreed to. Only the four status
  // words are looked up; the body is shown as written.
  const html = page("ja", {
    incidents: [
      {
        id: 1,
        title: "Verification is slow",
        impact: "degraded",
        started_at: NOW - 3600,
        resolved_at: null,
        updates: [{ status: "investigating", at: NOW - 3500, body: "Looking into it." }],
      },
    ],
  });
  assert.ok(html.includes("Verification is slow"));
  assert.ok(html.includes("Looking into it."));
  assert.ok(html.includes("調査中"), "the status word is one of four and is translated");
});

test("a count is said with the reader's own plural rule", () => {
  const ru = translator("ru");
  // Russian picks by the last digit: 1 day, 2 days (few), 5 days (many).
  assert.equal(ru.plural(1, "%{count} day", "%{count} days", { count: 1 }), "1 день");
  assert.equal(ru.plural(2, "%{count} day", "%{count} days", { count: 2 }), "2 дня");
  assert.equal(ru.plural(5, "%{count} day", "%{count} days", { count: 5 }), "5 дней");
  assert.equal(ru.plural(21, "%{count} day", "%{count} days", { count: 21 }), "21 день");

  // Japanese agrees with nothing, and should not be made to.
  const ja = translator("ja");
  assert.equal(ja.plural(1, "%{count} day", "%{count} days", { count: 1 }), "1 日");
  assert.equal(ja.plural(7, "%{count} day", "%{count} days", { count: 7 }), "7 日");

  // English has no catalog, so it falls back to the two forms it was handed.
  const en = translator(DEFAULT_LOCALE);
  assert.equal(en.plural(1, "%{count} day", "%{count} days", { count: 1 }), "1 day");
  assert.equal(en.plural(3, "%{count} day", "%{count} days", { count: 3 }), "3 days");
});

test("Arabic numerals on this page are the ones the timestamps use", () => {
  // A deliberate departure from CLDR, which would give Arabic-Indic digits.
  // The strings a figure sits beside here are ISO dates and UTC stamps, which
  // are machine formats and ASCII in every language, and one tooltip carrying
  // two numeral systems reads as a rendering fault. See NUMBER_LOCALE.
  const ar = translator("ar");
  assert.equal(ar.number(99.98, { minimumFractionDigits: 2, maximumFractionDigits: 2 }), "99.98");
  assert.ok(!/[٠-٩]/.test(ar.number(1234)), "Arabic-Indic digits are back");
  // Every other language keeps its own convention. This is one override, not
  // a decision to format the whole page in American.
  assert.equal(translator("de").number(1234.5), "1.234,5");
  assert.equal(translator("ru").number(99.98), "99,98");
});

test("an unknown message answers with itself rather than with nothing", () => {
  for (const locale of LOCALES) {
    const t = translator(locale);
    assert.equal(t("a sentence nobody has translated"), "a sentence nobody has translated");
    assert.equal(t("Hello %{who}", { who: "world" }), "Hello world");
    // A placeholder with no value is left alone, so the gap is visible rather
    // than silently becoming "undefined".
    assert.equal(t("Hello %{who}"), "Hello %{who}");
  }
});
