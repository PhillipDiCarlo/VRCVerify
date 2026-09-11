/**
 * Twelve languages on a page that has to work when everything else does not.
 *
 * WHY NOT GETTEXT, WHICH IS WHAT THE REST OF THE PROJECT USES
 *
 * The dashboard reads committed .mo files with Python's `gettext`. Neither is
 * available here: this is a Cloudflare Worker, there is no filesystem at the
 * moment a request arrives, and adding a runtime fetch for a catalog would put
 * the one page that reports outages behind a thing that can be part of one.
 *
 * So the catalogs are ES modules, bundled into the script at deploy time by
 * wrangler. They cost bytes and nothing else: no I/O, no cache, no second
 * origin, and a locale that fails to load is a build failure rather than a
 * blank page at 3am.
 *
 * WHAT IS KEPT FROM GETTEXT is the part that matters, which is the failure
 * mode: entries are keyed by the ENGLISH SOURCE STRING, exactly as a msgid,
 * and a lookup that misses falls back to its own key. Change an English
 * sentence without re-translating it and every locale shows the new English,
 * which is wrong but true. The alternative -- short symbolic keys -- shows the
 * OLD translation of a sentence that no longer says that, and nothing detects
 * it. On a status page, stale-but-fluent is the worse of the two.
 *
 * It also means a translation memory can read these catalogs against
 * src/dashboard/translations/*.po, because the two surfaces share msgids
 * wherever they share a sentence.
 */

import { CATALOGS } from "./locales/index.js";

/**
 * The source language. Its catalog is the msgids themselves, so there is no
 * locales/en.js and there should never be one -- see the same note in
 * src/dashboard/i18n.py, which this deliberately mirrors.
 *
 * English is served at `/`, not at `/en`, because `/` is the URL on the DNS
 * record, in Stripe's dashboard, in every link ever posted and in whatever
 * anybody has bookmarked. A default that lives one redirect away from the
 * canonical address is a default that breaks curl.
 */
export const DEFAULT_LOCALE = "en";

/**
 * The eleven, in the dashboard's order and with the dashboard's codes.
 *
 * SAME SPELLINGS AS `UI_LANGUAGES` in src/dashboard/i18n.py, hyphenated rather
 * than underscored: these are BCP 47 tags that go in a URL and in `lang`, not
 * gettext directory names. `test_the_status_page_offers_the_dashboards_
 * languages` fails if the two lists stop agreeing, because a reader who sets
 * Japanese on the dashboard and finds no Japanese here has been told the
 * product speaks a language that half of it does not.
 */
export const LOCALES = [
  DEFAULT_LOCALE,
  "es-ES",
  "zh-CN",
  "ja",
  "de",
  "nl",
  "hi-IN",
  "ar",
  "bn",
  "pt-BR",
  "ru",
  "pa-IN",
];

/**
 * Names in the language they name, copied from `ENDONYMS` in
 * src/dashboard/i18n.py along with its reasoning: this list is read by the
 * person who cannot read the page, and "Japanese" is no help to somebody
 * looking for the word they would recognize.
 */
export const ENDONYMS = {
  en: "English",
  "es-ES": "Español",
  "zh-CN": "简体中文",
  ja: "日本語",
  de: "Deutsch",
  nl: "Nederlands",
  "hi-IN": "हिन्दी",
  ar: "العربية",
  bn: "বাংলা",
  "pt-BR": "Português (Brasil)",
  ru: "Русский",
  "pa-IN": "ਪੰਜਾਬੀ",
};

/**
 * Written right to left.
 *
 * ONLY `dir` IS SET FROM THIS, and that is the same scope the dashboard chose
 * (see the note above `RTL` in src/dashboard/i18n.py). Mirroring the LAYOUT is
 * a stylesheet job neither surface has been asked to do, and it is tracked
 * apart from the translation work.
 *
 * `dir` is worth setting on day one regardless, because it is a different
 * question: it governs where a line starts, which end punctuation lands on,
 * and how a mixed string like "VRCVerify Premium" is ordered inside an Arabic
 * sentence. Getting that wrong is unreadable in a way an unmirrored margin is
 * not.
 */
const RTL = new Set(["ar"]);

export function direction(locale) {
  return RTL.has(locale) ? "rtl" : "ltr";
}

/** The locale for a URL path, or null when the path names no locale. */
export function localeFromPath(pathname) {
  const first = pathname.split("/")[1] ?? "";
  if (!first) return null;
  // Case-insensitively, because `pt-br` is the same request as `pt-BR` and a
  // 404 for a capital letter is a bad answer to a correct URL. The canonical
  // spelling is what the picker links to and what `hreflang` advertises.
  const wanted = first.toLowerCase();
  return LOCALES.find((code) => code.toLowerCase() === wanted) ?? null;
}

/** `/`, `/ja`, `/pt-BR`. The path a locale is served at. */
export function pathForLocale(locale) {
  return locale === DEFAULT_LOCALE ? "/" : `/${locale}`;
}

/**
 * The best of the twelve for an `Accept-Language` header.
 *
 * Region is dropped before matching, so `pt-PT` reaches the Brazilian catalog
 * and `es-419` reaches the Spanish one. That is not ideal Portuguese for a
 * reader in Lisbon and it is a great deal better than English, which is the
 * only other thing on offer.
 *
 * Quality values are honored because browsers actually send them: a reader
 * whose list is `de;q=0.9, en;q=1.0` has said they prefer English, and a page
 * that hands them German anyway has ignored the one instruction it was given.
 */
export function negotiate(header) {
  if (!header) return DEFAULT_LOCALE;
  const ranked = header
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.find((p) => p.trim().startsWith("q="));
      const weight = q ? Number.parseFloat(q.trim().slice(2)) : 1;
      return { tag: tag.trim().toLowerCase(), q: Number.isFinite(weight) ? weight : 0 };
    })
    .filter((entry) => entry.tag && entry.q > 0)
    // Stable within a weight, so a tie keeps the order the reader sent.
    .sort((a, b) => b.q - a.q);

  for (const { tag } of ranked) {
    if (tag === "*") return DEFAULT_LOCALE;
    const exact = LOCALES.find((code) => code.toLowerCase() === tag);
    if (exact) return exact;
    const base = tag.split("-")[0];
    const loose = LOCALES.find((code) => code.toLowerCase().split("-")[0] === base);
    if (loose) return loose;
  }
  return DEFAULT_LOCALE;
}

/**
 * Locales whose numbers are formatted as some OTHER locale.
 *
 * There is one, and it is a deliberate departure from CLDR. `Intl` defaults
 * Arabic to Arabic-Indic digits, which is correct in general and wrong on this
 * page in particular: the strings a percentage sits next to are not
 * translatable and cannot follow it. The day a bar describes is an ISO date,
 * the timestamps are `2026-08-31 18:22 UTC`, and both are ASCII in every
 * language because they are machine formats. A tooltip reading
 * "2026-08-31: ٩٩٫٩٨٪" is one line carrying two numeral systems, and the
 * mismatch reads as a rendering fault rather than as a choice.
 *
 * `-u-nu-latn` swaps the whole numbering system, separators included, so an
 * Arabic percentage reads "99.98" rather than "٩٩٫٩٨". That is more than the
 * digits alone, and it is still the right trade here: the figure has to match
 * the ISO date sitting two characters away from it, and half-swapping would
 * leave a Latin-digit number with an Arabic decimal comma, which is a notation
 * nobody uses.
 *
 * IT IS ONE LOCALE. Every other language keeps its own convention, so German
 * still reads 1.234,5 and Russian still reads 99,98.
 */
const NUMBER_LOCALE = { ar: "ar-u-nu-latn" };

/** Values wrapped in this are inserted into a message without escaping. */
const RAW = Symbol("raw");

/**
 * Mark an already-built fragment of HTML as safe to interpolate.
 *
 * There is exactly one reason this exists: a handful of sentences wrap a
 * timestamp in `<time datetime="...">`, and splitting them into "Started" plus
 * an element plus "." would hand translators three fragments whose order they
 * cannot change. One message with one placeholder is the translatable version.
 */
export function html(value) {
  return { [RAW]: String(value) };
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * `%{name}`, filled in and escaped.
 *
 * THE MESSAGE IS TRUSTED AND THE VALUES ARE NOT. Messages come from the
 * catalogs, which are source files in this repository; values come from D1,
 * which holds what an operator typed into the admin form. So the template goes
 * through untouched and every substitution is escaped, unless it was wrapped
 * in `html()` above. The result is safe to place in an element or an attribute.
 */
function interpolate(message, vars) {
  if (!vars) return message;
  return message.replace(/%\{(\w+)\}/g, (whole, name) => {
    if (!(name in vars)) return whole;
    const value = vars[name];
    if (value && typeof value === "object" && RAW in value) return value[RAW];
    return escapeHtml(value);
  });
}

/**
 * The translator for one request.
 *
 * `t(msgid, vars)` for a sentence, `t.plural(n, one, other, vars)` for one that
 * counts something, `t.number(value, options)` for a figure.
 *
 * PLURALS GO THROUGH `Intl.PluralRules` rather than through `n === 1`, because
 * that test is right for English and wrong for most of this list: Russian has
 * three forms and picks by the last digit, Arabic has six, and Japanese and
 * Chinese have one and want no agreement at all. V8 ships the rules, the
 * Workers runtime has full ICU, and the catalog simply stores whichever
 * categories its language uses. Hand-rolling this would be a second, worse
 * copy of data the platform already has.
 *
 * A missing category falls back to `other`, then to the English `one`/`other`
 * that was passed in. A reader never sees a blank.
 */
export function translator(locale) {
  const catalog = CATALOGS[locale] ?? {};
  // `en` deliberately has no catalog, so this is also the English path: every
  // lookup misses and every msgid is its own answer.
  const rules = new Intl.PluralRules(locale);
  const numbers = new Intl.NumberFormat(NUMBER_LOCALE[locale] ?? locale);

  function t(msgid, vars) {
    const entry = catalog[msgid];
    const message = typeof entry === "string" ? entry : msgid;
    return interpolate(message, vars);
  }

  t.locale = locale;
  t.dir = direction(locale);

  t.plural = (count, one, other, vars) => {
    const entry = catalog[one];
    const category = rules.select(count);
    let message;
    if (entry && typeof entry === "object") {
      message = entry[category] ?? entry.other ?? entry.one;
    }
    if (!message) message = category === "one" ? one : other;
    return interpolate(message, vars);
  };

  /**
   * Digits and separators in the reader's convention. 99.98% is 99,98% in
   * German and Russian, and a status page quoting a figure in a notation its
   * reader does not use is asking them to re-read it.
   */
  const numberLocale = NUMBER_LOCALE[locale] ?? locale;
  t.number = (value, options) =>
    options ? new Intl.NumberFormat(numberLocale, options).format(value) : numbers.format(value);

  return t;
}
