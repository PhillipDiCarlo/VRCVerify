/**
 * The page, as a string. No framework, no client-side rendering, no fetch.
 *
 * WHY IT IS SERVER RENDERED AND NOT A JSON FETCH ON THE CLIENT
 *
 * A page that renders empty and then asks for its own data is a page that
 * shows nothing to anyone whose JavaScript is off, blocked, or still loading
 * on the hotel wifi they are reading this from. It also cannot be read by
 * curl, which is the second most likely way anybody consults a status page.
 * The whole document arrives in the first response; /theme.js is the only
 * script, and every word here is legible without it.
 */

import { COMPONENTS, DAY_RED_BELOW_PERCENT, HISTORY_DAYS, UPSTREAMS } from "./config.js";
import {
  UPDATE_STATUSES,
  dayUptime,
  durationParts,
  headlineWithOpenIncidents,
  // Still used, by renderAdmin only. The admin page is one signed-in operator
  // and stays in English -- see the note on it below.
  humanDuration,
  uptimeOverDays,
  verdict,
} from "./logic.js";
import { DEFAULT_LOCALE, ENDONYMS, LOCALES, html, pathForLocale } from "./i18n.js";

/**
 * THE MARK, as it appears in both headers.
 *
 * A constant rather than markup typed twice, and a JS comment rather than an
 * HTML one, because this file's output is re-rendered on every request and the
 * reasoning does not need to travel to the reader.
 *
 * The same public/logo.svg the apex and the dashboard serve, byte for byte,
 * and the same arrangement: black artwork, inverted for dark by --logo-filter.
 * Three copies of the file for the reason the fonts and theme.js have three --
 * each surface has to keep working when the others are down, so none of them
 * may fetch the mark from another. `test_the_logo_is_the_same_file_everywhere`
 * fails if they drift.
 *
 * `width`/`height` are the viewBox's dimensions, not the rendered ones: they
 * give the browser the ratio so the wordmark does not jump sideways when the
 * image lands. The stylesheet sets the height it is drawn at. `alt=""` because
 * the word beside it says the same thing.
 */
const MARK =
  '<img class="brand-mark" src="/logo.svg" alt="" width="805" height="615">';

/**
 * THESE STAY ENGLISH, and that is not an oversight.
 *
 * They are msgids, which is how every catalog under locales/ is keyed, so the
 * lookup happens where the word is drawn rather than here. Translating this
 * object in place would also break /api/status.json, which reads the same
 * vocabulary and is a contract with whatever is parsing it.
 */
export const STATE_LABEL = {
  up: "Operational",
  degraded: "Degraded",
  down: "Down",
  unknown: "Unknown",
};

/**
 * The four update words as a reader sees them, apart from the four as stored.
 *
 * WHY NOT `text-transform: capitalize`, which is what this was.
 *
 * The stored values are lowercase because they are an enum: `UPDATE_STATUSES`
 * is what the admin form posts and what the database holds. Making them
 * presentable in CSS worked for exactly one language. `capitalize` upper-cases
 * EVERY word, so German's "wird beobachtet" came out as "Wird Beobachtet",
 * which is not how German capitalizes a verb phrase, and Dutch had the same
 * problem. Found in a 390px screenshot, which is the only place it was ever
 * going to be visible.
 *
 * So the presentation is a msgid and each language writes its own casing.
 * "Resolved" is deliberately the SAME msgid the pill on a finished incident
 * uses: it is the same word saying the same thing, and two entries would be
 * two chances for one language to render them differently.
 */
const UPDATE_STATUS_LABEL = {
  investigating: "Investigating",
  identified: "Identified",
  monitoring: "Monitoring",
  resolved: "Resolved",
};

export const UPDATE_STATUS_LABELS = Object.values(UPDATE_STATUS_LABEL);

/**
 * "3 hours", in the reader's language and with the reader's plural rule.
 *
 * `logic.durationParts` decides the unit and the number; this says it. The two
 * are split so that the page and the Discord alert cannot round differently --
 * see the note there.
 */
function duration(t, seconds) {
  const parts = durationParts(seconds);
  if (!parts) return t("less than a minute");
  const vars = { count: t.number(parts.count) };
  if (parts.unit === "day") return t.plural(parts.count, "%{count} day", "%{count} days", vars);
  if (parts.unit === "hour") return t.plural(parts.count, "%{count} hour", "%{count} hours", vars);
  return t.plural(parts.count, "%{count} minute", "%{count} minutes", vars);
}

/**
 * The four glyphs, so that state is never carried by color alone.
 *
 * Drawn rather than lettered, because a check and a cross survive being
 * shrunk to 17px in a way that a glyph from the body font does not -- the
 * lesson from the apex site's first browser pass, where a mark that read
 * perfectly in the CSS was illegible at the size it was actually drawn.
 * `stroke-width` is generous for the same reason.
 */
const GLYPHS = {
  // The check gets the same ring as the other three. Without it the four
  // glyphs were not siblings: three enclosed marks and one loose tick, and the
  // tick read as lighter than the states it sits beside -- so the healthy row
  // looked less certain than the broken one. Only visible in a screenshot.
  up: '<path d="M6.2 10.4l2.6 2.6 5.2-5.6"/><circle cx="10" cy="10" r="8.25"/>',
  degraded: '<path d="M10 5v6"/><path d="M10 14.5v.5"/><circle cx="10" cy="10" r="8.25"/>',
  down: '<path d="M6.5 6.5l7 7"/><path d="M13.5 6.5l-7 7"/><circle cx="10" cy="10" r="8.25"/>',
  unknown: '<path d="M7.4 7.8a2.6 2.6 0 1 1 2.6 2.9V12"/><path d="M10 14.5v.5"/><circle cx="10" cy="10" r="8.25"/>',
};

function glyph(state, extraClass = "glyph") {
  const paths = GLYPHS[state] ?? GLYPHS.unknown;
  // aria-hidden because the state is already written out in the pill beside
  // it. A screen reader announcing "image, check" before the word
  // "Operational" is repetition, not access.
  return (
    `<span class="${extraClass}" aria-hidden="true">` +
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round">' +
    paths +
    "</svg></span>"
  );
}

export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** `2026-08-31 18:22 UTC`. One timezone, named, everywhere on the page. */
export function utcStamp(unixSeconds) {
  const iso = new Date(unixSeconds * 1000).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/**
 * HOW MANY DAYS THE STRIP DRAWS, which is not how many days the page keeps.
 *
 * `HISTORY_DAYS` is 90 and stays 90: it is what the uptime percentage averages
 * over and what the nightly delete measures retention against, so lowering it
 * would throw history away permanently to fix a drawing problem.
 *
 * What changes is how many of those days are drawn, because ninety bars in a
 * 320px column are two pixels each and a bad day inside them is invisible --
 * which is the width a status page is actually read at, during an outage, on a
 * phone. The oldest bars are hidden in CSS, so the markup is the same at every
 * width and there is no second query, no JavaScript and no layout shift.
 *
 * THESE TWO NUMBERS ARE DUPLICATED IN style.css as `nth-child` selectors and
 * cannot be handed to it: CSS cannot read a JavaScript constant, and a strip
 * that draws sixty days under a label reading "90 days ago" is a page lying
 * about its own evidence. `historyWindowsAgree` in render.test.js reads both
 * files and fails when they drift.
 */
export const WIDE_DAYS = 60;
export const NARROW_DAYS = 30;

/**
 * Ninety days, one bar each, oldest on the left.
 *
 * THE BARS ARE NOT THE ACCESSIBLE VERSION OF ANYTHING. They are a shape, and a
 * shape is exactly what a reader wants at a glance: is this a wall of green
 * with one notch in it, or a mess. The facts underneath -- the percentage, and
 * how many days had trouble -- are written out in text beside them, and the
 * strip itself is hidden from assistive technology rather than being narrated
 * as ninety anonymous list items.
 *
 * A day with no observations is drawn in the track color, distinct from both
 * green and red. It has to be: this page will have exactly that for its first
 * eighty-nine days, and drawing "we did not exist yet" as either health or
 * failure would be inventing history.
 *
 * Every one of the ninety is written out. CSS hides the oldest of them, and
 * how many depends on the width -- see WIDE_DAYS above.
 */
function historyStrip(t, days, rows) {
  const bars = days
    .map((day) => {
      const row = rows?.[day];
      const { percent, state } = dayUptime(row, {
        redBelowPercent: DAY_RED_BELOW_PERCENT,
      });
      // Excluded minutes are SAID OUT LOUD rather than silently left out of the
      // denominator. A percentage that quietly skipped an hour is the kind of
      // number a status page loses its credit for once, permanently.
      const excluded = row?.maintenance ?? 0;
      const note =
        excluded > 0
          ? t(" (%{duration} of maintenance not counted)", {
              duration: duration(t, excluded * 60),
            })
          : "";
      // ALREADY ESCAPED BY `t`, which escapes every substitution it makes and
      // passes the message itself through -- so this goes straight into the
      // attribute. Running escapeHtml over it again would double-encode the
      // apostrophe in a language that uses one.
      const label =
        percent === null
          ? state === "maintenance"
            ? t("%{day}: maintenance all day", { day })
            : t("%{day}: no data", { day })
          : t("%{day}: %{percent}% up", {
              day,
              percent: t.number(percent, {
                minimumFractionDigits: percent === 100 ? 0 : 2,
                maximumFractionDigits: percent === 100 ? 0 : 2,
              }),
            }) + note;
      return `<span class="bar is-${state}" title="${label}"></span>`;
    })
    .join("");
  return `<div class="bars" aria-hidden="true">${bars}</div>`;
}

/** "99.98% over 90 days", or an honest sentence when there is nothing to average. */
function uptimeLine(t, days, rows) {
  const present = days.map((day) => rows?.[day]).filter(Boolean);
  const percent = uptimeOverDays(present);
  const troubled = present.filter((row) => (row.down ?? 0) + (row.degraded ?? 0) > 0).length;
  if (percent === null) {
    return `<span class="row-uptime">${t("No history yet")}</span>`;
  }
  // Two decimals, and never rounded UP to 100. A page that says 100% on a day
  // it was down for four minutes is a page that has started lying politely.
  const clamped = percent >= 99.995 && percent < 100 ? 99.99 : percent;
  const shown = t.number(clamped, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  // ONE MESSAGE PER SENTENCE, not a stem plus a tail. "with no incidents" and
  // "with 3 days affected" read as one clause in English and as a different
  // shape in most of the eleven, and a translator handed the halves separately
  // cannot reorder them. The day COUNT is the plural here; the window (60 or
  // 90) never is, so it interpolates.
  const summary =
    troubled === 0
      ? t("%{percent}% uptime over the last %{days} days, with no incidents", {
          percent: shown,
          days: t.number(days.length),
        })
      : t.plural(
          troubled,
          "%{percent}% uptime over the last %{days} days, with %{count} day affected",
          "%{percent}% uptime over the last %{days} days, with %{count} days affected",
          { percent: shown, days: t.number(days.length), count: t.number(troubled) },
        );
  return `<span class="row-uptime" title="${summary}">${shown}%</span>` +
    `<span class="visually-hidden">${summary}</span>`;
}

/**
 * The x-axis for the strips, drawn ONCE under the last row rather than under
 * each of the five.
 *
 * All five strips are the same ninety days, flexed to the same width, in the
 * same order, so five copies of "90 days ago / Today" label one axis five
 * times. It read as part of each row and it is not: it belongs to the card.
 *
 * Only the number swaps between widths. Which of the two spans is shown is
 * decided in CSS by the same media query that hides the bars, so the label can
 * never name a range the strip is not drawing.
 */
function barsLegend(t, dayCount) {
  // CLAMPED TO WHAT IS ACTUALLY THERE. `recentDays` always returns exactly
  // HISTORY_DAYS in production, so the window is always the smaller number --
  // but a strip holding four days under a label reading "60 days ago" would be
  // the page overstating its own evidence, and that is the one thing the axis
  // must never do. It costs a Math.min.
  const wide = Math.min(WIDE_DAYS, dayCount);
  const narrow = Math.min(NARROW_DAYS, dayCount);
  // THE WHOLE PHRASE GOES IN EACH SPAN, where it used to be two numbers
  // sharing one trailing " days ago". English puts the count first and most of
  // the eleven do not, so a shared tail is a sentence a translator cannot
  // assemble. Nothing about the swap is visible: the two spans still differ
  // only in which one CSS is showing.
  const ago = (n) => t.plural(n, "%{count} day ago", "%{count} days ago", { count: t.number(n) });
  return (
    '<div class="bars-legend" aria-hidden="true"><span>' +
    `<span class="at-wide">${ago(wide)}</span>` +
    `<span class="at-narrow">${ago(narrow)}</span></span>` +
    `<span>${t("Today")}</span></div>`
  );
}

function pill(t, state) {
  return `<span class="pill">${t(STATE_LABEL[state] ?? STATE_LABEL.unknown)}</span>`;
}

function componentRow(t, component, entry, now, history) {
  const state = entry?.state ?? "unknown";
  // "Down for 3 hours" is the question a reader has after "Down", and it is
  // the one thing a status page can answer that a refresh cannot.
  // A DURATION ONLY WHERE IT MEANS SOMETHING. "Down for 3 hours" answers the
  // question a reader has immediately after "Down". "Unknown for 2 minutes"
  // answers nothing: it times how long this page has been failing to find out,
  // which is a fact about the page, and on a row that has never once been
  // measured it is really the age of the deployment. It read as though the
  // service had been unknown-since-something, and nothing had happened.
  const timed = state === "down" || state === "degraded";
  const since =
    timed && entry?.since
      ? `<span class="row-since">${t("for %{duration}", {
          duration: duration(t, now - entry.since),
        })}</span>`
      : "";
  return (
    `<div class="row is-${state}">` +
    `<span class="row-name">${glyph(state)}<span>${t(component.name)}</span></span>` +
    `<span class="row-state">${since}` +
    (history ? uptimeLine(t, history.days, history.byComponent[component.id]) : "") +
    `${pill(t, state)}</span>` +
    `<p class="row-desc">${t(component.description)}</p>` +
    (history ? historyStrip(t, history.days, history.byComponent[component.id]) : "") +
    "</div>"
  );
}

function upstreamRow(t, upstream, entry) {
  const state = entry?.state ?? "unknown";
  // The four company names are NOT translated and are not msgids. They are
  // proper nouns, they are what is written on the page being linked to, and a
  // reader looking for Discord is looking for the word Discord.
  return (
    `<div class="row is-${state}">` +
    `<span class="row-name">${glyph(state)}<span>${escapeHtml(upstream.name)}</span></span>` +
    `<span class="row-state">${pill(t, state)}</span>` +
    `<p class="row-desc">${t(upstream.why)} ` +
    `<a class="dep-link" href="${escapeHtml(upstream.href)}" rel="noopener">` +
    `${t("%{company}'s own status page", { company: upstream.name })}</a></p>` +
    "</div>"
  );
}

const IMPACT_STATE = { maintenance: "degraded", degraded: "degraded", down: "down" };

function updateItem(t, update) {
  // The STATUS is one of four fixed words and is translated. The BODY is not:
  // it is what an operator typed into the admin form during an outage, in
  // whatever language they wrote it in, and machine-translating a person's
  // account of what is broken is how a status page ends up saying something
  // nobody agreed to. It is escaped and shown as written.
  return (
    '<li class="update">' +
    // THE FALLBACK IS ESCAPED, THE LOOKUP IS NOT, and the asymmetry is the
    // point. A msgid is trusted -- it comes from this repository and `t`
    // passes it through so a translated sentence can carry its own markup.
    // `update.status` is a column, and while `readUpdateForm` will only ever
    // write one of the four, a row that somehow holds something else must not
    // reach `t` as a msgid: that is a string from the database rendered
    // unescaped. Defense in depth, found by attacking this file rather than
    // by a failing test.
    '<span class="update-status">' +
    (UPDATE_STATUS_LABEL[update.status]
      ? t(UPDATE_STATUS_LABEL[update.status])
      : escapeHtml(update.status)) +
    "</span>" +
    `<time datetime="${new Date(update.at * 1000).toISOString()}">${utcStamp(update.at)}</time>` +
    `<p>${escapeHtml(update.body)}</p></li>`
  );
}

function updateList(t, updates) {
  return `<ol class="updates">${updates.map((u) => updateItem(t, u)).join("")}</ol>`;
}

/**
 * The updates that are not the current position, folded away.
 *
 * `<details>` and nothing else: no JavaScript, no second route, no per-incident
 * render function, and it works with scripting off, which is the discipline the
 * rest of this page keeps. Closed by default, and the ONE thing that stays
 * open is the latest update, because that is the current position and the
 * archaeology is what the reader is being spared.
 *
 * TWO LISTS RATHER THAN ONE, deliberately. `<details>` is flow content and is
 * not allowed as a child of `<ol>`, so a single list cannot have its tail
 * folded. Both carry `.updates`, both read newest first, and neither is
 * numbered, so nothing about the split is visible.
 */
function foldedUpdates(t, updates, kind) {
  if (!updates.length) return "";
  const n = updates.length;
  // TWO MESSAGES RATHER THAN A NOUN SLOTTED INTO ONE. The English was
  // "%{count} earlier update" built from the word "update" plus an "s", which
  // works in exactly one of the twelve languages here: nothing else agrees a
  // noun with a number by appending a letter to it, and several change the
  // noun's case as well. Each of the two summaries is now its own plural
  // message.
  const count =
    kind === "earlier"
      ? t.plural(n, "%{count} earlier update", "%{count} earlier updates", { count: t.number(n) })
      : t.plural(n, "%{count} update", "%{count} updates", { count: t.number(n) });
  return `<details class="timeline"><summary>${count}</summary>${updateList(t, updates)}</details>`;
}

/**
 * An incident that is happening now. Full width, above the two columns.
 *
 * IT DOES NOT GO IN THE SIDE COLUMN with the resolved ones, and the reason is
 * the reader: somebody arriving mid-outage is here for this, and a live
 * incident set in a 288px rail beside five green rows is a page burying its own
 * lead. "Beside, not below" was decided about the HISTORY, which is what was
 * pushing the methodology note off the bottom of the page.
 *
 * What it does lose is the archaeology. The banner used to print every update
 * in full, so one incident with three of them filled the entire first screen
 * and the five component states began around 700px down -- the reader had to
 * scroll past the narrative to reach the facts. The latest update shows; the
 * rest fold.
 */
function openIncident(t, incident, now) {
  const state = IMPACT_STATE[incident.impact] ?? "degraded";
  const [latest, ...earlier] = incident.updates;
  const started = `<time datetime="${new Date(incident.started_at * 1000).toISOString()}">` +
    `${utcStamp(incident.started_at)}</time>`;
  return (
    `<section class="card incident is-${state}">` +
    '<div class="card-head">' +
    `<h2>${escapeHtml(incident.title)}</h2>` +
    `<span class="pill">${t(STATE_LABEL[state] ?? "Degraded")}</span></div>` +
    // ONE SENTENCE, not "Ongoing for" plus a duration plus "Started" plus an
    // element. `html()` lets the <time> travel as a placeholder so the
    // translator gets a whole sentence they can reorder.
    `<p class="incident-meta">${t("Ongoing for %{duration}. Started %{time}.", {
      duration: duration(t, now - incident.started_at),
      time: html(started),
    })}</p>` +
    (latest ? updateList(t, [latest]) : "") +
    foldedUpdates(t, earlier, "earlier") +
    "</section>"
  );
}

/**
 * An incident that is over, in the side column.
 *
 * A FINISHED INCIDENT IS DRAWN NEUTRAL, not in the color of the trouble it
 * used to be. The history section was shipping a red-bordered, red-tinted card
 * for something that had been fixed hours earlier -- alarming at a glance, and
 * wrong the moment anybody read the date. The state color is for things that
 * are happening now; what a closed one needs to say is how long it lasted.
 *
 * Every update folds, including the last. The summary line already says the
 * outcome and the duration, which is what a reader scanning the history wants;
 * the timeline is what they open when one of them turns out to matter.
 */
function pastIncident(t, incident) {
  const started = `<time datetime="${new Date(incident.started_at * 1000).toISOString()}">` +
    `${utcStamp(incident.started_at)}</time>`;
  return (
    '<section class="card incident is-resolved">' +
    '<div class="card-head">' +
    `<h2>${escapeHtml(incident.title)}</h2>` +
    `<span class="pill">${t("Resolved")}</span></div>` +
    `<p class="incident-meta">${t("Resolved after %{duration}. Started %{time}.", {
      duration: duration(t, incident.resolved_at - incident.started_at),
      time: html(started),
    })}</p>` +
    foldedUpdates(t, incident.updates, "update") +
    "</section>"
  );
}

/**
 * The canonical origin, for `canonical` and `hreflang`.
 *
 * A CONSTANT RATHER THAN `request.url`, which is what the first version used
 * and which was wrong in the one case these tags exist for: the Worker also
 * answers on its preview URL during a `wrangler dev`, and a page that
 * advertises the address it happened to be fetched from will happily tell a
 * crawler that the Japanese page lives on localhost. The hostname is in
 * wrangler.toml's [[routes]] and it is this.
 */
const ORIGIN = "https://status.vrcverify.com";

/**
 * The headline when the data may not be believed, by why it may not be.
 *
 * A NAMED TABLE RATHER THAN A NESTED TERNARY INSIDE renderPage, for the reason
 * `logic.HEADLINES` is one: locales/index.js inventories the msgids from the
 * source, and a sentence buried in an expression is a sentence the inventory
 * cannot see. It was a ternary until this file grew twelve languages.
 */
export const FRESHNESS_HEADLINE = {
  missing: "No check has run yet",
  unavailable: "Status cannot be read right now",
  stale: "Status is out of date",
};

/**
 * What the page says about itself when it is the broken thing.
 *
 * Each one says what is broken, what the page is doing about it, and what it
 * does NOT imply. The last part matters: a reader who sees a wall of gray
 * needs to be told that gray is a statement about this page rather than about
 * the bot.
 *
 * These are the longest msgids on the page and they are kept whole rather than
 * split into clauses, for the reason every other sentence here is: a
 * translator handed three fragments cannot put them in their language's order.
 * The `+` below is this file wrapping at 96 columns and nothing more -- the
 * msgid is the joined string, so a stray space here is a lookup miss in eleven
 * languages at once.
 */
export const FRESHNESS_WARNING = {
  stale:
    "The checker last reported more than five minutes ago, so every row above is " +
    "shown as unknown rather than as whatever it said last. The services themselves " +
    "may well be fine.",
  unavailable:
    "This page cannot reach its own storage, so it has nothing to report. That is a " +
    "fault in the status page and says nothing about whether the services are working.",
  missing:
    "No check has completed yet. This is what the page looks like before its first " +
    "run, and it should correct itself within a minute.",
};

/**
 * The language picker: a globe, and twelve links.
 *
 * LINKS, NOT A FORM, which is where this differs from the dashboard's. That
 * one posts a preference because it has a session to write it to; this page
 * has no session and wants none. A link is better here for three reasons that
 * happen to line up: it works with scripting off (the discipline the whole
 * page keeps), each locale is its own URL so the edge can cache twelve pages
 * instead of varying one, and a reader can send somebody "the status page, in
 * Japanese" and have it arrive in Japanese.
 *
 * Everything else is lifted from the dashboard's picker deliberately, down to
 * the globe: a flag is a country and these are languages, a letter is drawn in
 * the script the reader cannot read, and a globe means the same thing to all
 * twelve. See the long note in src/dashboard/templates/base.html.
 */
function languagePicker(t) {
  const current = t.locale;
  const items = LOCALES.map((code) => {
    const here = code === current;
    // `lang` on each link, not just on <html>: this is twelve languages
    // rendered inside one page, and it is what tells a screen reader to say
    // 日本語 in Japanese rather than spelling it out in the page's voice.
    return (
      `<li><a href="${pathForLocale(code)}" lang="${code}"` +
      ` class="menu-item${here ? " current" : ""}"${here ? ' aria-current="true"' : ""}>` +
      `<span class="menu-item-label">${escapeHtml(ENDONYMS[code])}</span>` +
      (here
        ? '<svg class="menu-tick" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" ' +
          'fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" ' +
          'stroke-linejoin="round"><path d="M3 8.5l3.5 3.5L13 4.5"/></svg>'
        : "") +
      "</a></li>"
    );
  }).join("");

  const name = escapeHtml(ENDONYMS[current] ?? current);
  return (
    '<details class="langpick">' +
    `<summary class="lang-button" title="Language: ${name}"` +
    ` aria-label="Language: ${name}. Choose a different one.">` +
    '<svg class="menu-mark" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" ' +
    'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
    'stroke-linejoin="round"><circle cx="8" cy="8" r="6.2"/><path d="M1.8 8h12.4"/>' +
    '<path d="M8 1.8a9.2 9.2 0 0 1 0 12.4a9.2 9.2 0 0 1 0-12.4z"/></svg>' +
    "</summary>" +
    // NOT TRANSLATED, and the only string on this page that is not. The label
    // sits above a list somebody opened *because* the page is in a language
    // they do not want, so rendering it in that language explains the menu to
    // everyone except the person reading it. Same call the dashboard made.
    '<div class="lang-panel"><p class="menu-label" id="lang-menu-label">Language</p>' +
    `<ul aria-labelledby="lang-menu-label">${items}</ul></div>` +
    "</details>"
  );
}

/**
 * `hreflang` for all twelve, plus `canonical` for this one.
 *
 * Without these, twelve URLs carrying the same page in different languages are
 * twelve competing duplicates to a crawler -- the same problem workers.dev
 * caused for the apex site, arriving by a different road. `x-default` points
 * at `/`, which is where a reader with no stated preference is served.
 */
function alternates(locale) {
  const links = LOCALES.map(
    (code) => `<link rel="alternate" hreflang="${code}" href="${ORIGIN}${pathForLocale(code)}">`,
  ).join("\n");
  return (
    `<link rel="canonical" href="${ORIGIN}${pathForLocale(locale)}">\n` +
    links +
    `\n<link rel="alternate" hreflang="x-default" href="${ORIGIN}${pathForLocale(DEFAULT_LOCALE)}">`
  );
}

/**
 * @param t           the translator for this request, from i18n.translator.
 * @param components  {id: {state, since}} for the five capabilities. PRIVATE
 *                    detail fields are not read here and must not be passed.
 * @param upstreams   {id: {state}} for the four dependencies.
 * @param checkedAt   unix seconds of the last completed check, or null.
 * @param freshness   "fresh" | "stale" | "missing", from logic.dataFreshness.
 */
export function renderPage({
  t,
  components,
  upstreams,
  history,
  incidents,
  checkedAt,
  now,
  freshness,
}) {
  // THE RULE, applied at the last possible moment so nothing can route around
  // it: if the data is not fresh, no component is drawn as up. A page that
  // shows green because the checker stopped running is the exact failure this
  // whole design is built to avoid, and it is worse than showing nothing.
  const trusted = freshness === "fresh";
  const shown = {};
  for (const component of COMPONENTS) {
    const entry = components[component.id];
    shown[component.id] = trusted ? entry : { state: "unknown", since: entry?.since };
  }
  // THE DEPENDENCY ROWS GET THE SAME GATE, which the first version of this
  // function forgot. They are not read live: they are read out of the same
  // storage, written by the same checker, and a checker that stopped an hour
  // ago knows no more about Discord than it does about us. Half a page of gray
  // rows beside four confident green ones would be worse than either, because
  // it reads as "our stuff is unknown, theirs is fine" -- a claim nobody made.
  const shownUpstreams = {};
  for (const upstream of UPSTREAMS) {
    shownUpstreams[upstream.id] = trusted ? upstreams[upstream.id] : { state: "unknown" };
  }

  const open = (incidents ?? []).filter((incident) => !incident.resolved_at);
  // The side column's contents. It is never the only thing in there: the
  // methodology note follows, so a quiet month leaves a column with something
  // in it rather than half a page of nothing.
  const past = (incidents ?? []).filter((incident) => incident.resolved_at);
  // THE COLOR IS MEASURED, NEVER TYPED. An incident is a person's words, and
  // it must not move the hero's state or one pill in either direction: not
  // better than the rows say (that was always true) and not worse either -- an
  // operator opening a "down" incident by habit, or over-stating one to be
  // safe, must not paint five working capabilities red for everyone reading
  // the page. Only the checks vote on state.
  //
  // The SENTENCE is allowed to acknowledge that the banner exists, because a
  // green "All systems operational" set directly above an open incident is a
  // page arguing with itself and winning in the wrong direction.
  const overall = trusted
    ? headlineWithOpenIncidents(
        verdict(COMPONENTS.map((c) => shown[c.id]?.state ?? "unknown")),
        open.length,
      )
    : {
        level: "unknown",
        headline: FRESHNESS_HEADLINE[freshness] ?? FRESHNESS_HEADLINE.missing,
      };

  // Four of the five headlines are fixed phrases and look up as msgids. The
  // fifth counts something, so `headlineWithOpenIncidents` hands the count
  // over beside its English and the plural rule is applied here.
  const headline = overall.openIncidents
    ? t.plural(
        overall.openIncidents,
        "%{count} open incident",
        "%{count} open incidents",
        { count: t.number(overall.openIncidents) },
      )
    : t(overall.headline);

  const checkedLine =
    checkedAt === null
      ? t("No check has completed yet.")
      : t("Checked %{ago} ago, at %{time}.", {
          ago: duration(t, now - checkedAt),
          time: html(
            `<time datetime="${new Date(checkedAt * 1000).toISOString()}">` +
              `${utcStamp(checkedAt)}</time>`,
          ),
        });

  const staleWarning = trusted
    ? ""
    : `<p class="card-note">${t(FRESHNESS_WARNING[freshness] ?? FRESHNESS_WARNING.missing)}</p>`;

  return `<!doctype html>
<html lang="${t.locale}" dir="${t.dir}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${t("VRCVerify Status")}</title>
<meta name="description" content="${t("Whether VRCVerify's verification, bot, invites, dashboard and website are working, and whether the services they depend on are.")}">
${alternates(t.locale)}
<link rel="stylesheet" href="/style.css">
<script src="/theme.js"></script>
</head>
<body>

<header class="site">
  <div class="wrap">
    <a class="brand" href="${pathForLocale(t.locale)}">${MARK}<span>${t("VRCVerify Status")}</span></a>
    <nav>
      <a href="https://vrcverify.com/">${t("Home")}</a>
      <a href="https://dashboard.vrcverify.com/">${t("Dashboard")}</a>
    </nav>
    ${languagePicker(t)}
    <div class="theme-picker" hidden></div>
  </div>
</header>

<main class="wrap">

  <div class="hero is-${overall.level}">
    ${glyph(overall.level, "hero-glyph")}
    <h1>${headline}</h1>
    <p class="hero-checked">${checkedLine}</p>
  </div>

  ${open.map((incident) => openIncident(t, incident, now)).join("\n  ")}

  <div class="board">
    <div class="board-main">

      <section class="card">
        <div class="card-head">
          <h2>VRCVerify</h2>
          <span class="is-${overall.level} pill">${t(STATE_LABEL[overall.level])}</span>
        </div>
        ${COMPONENTS.map((c) => componentRow(t, c, shown[c.id], now, history)).join("\n        ")}
        ${history ? barsLegend(t, history.days.length) : ""}
        ${staleWarning}
      </section>

    </div>

    <!-- WHAT IS IN THE SIDE COLUMN, AND WHY IT IS NOT WHAT #287 LISTED.

         The issue put dependencies in the main column and only the incidents
         beside. Built that way, the right column ran out about 900px above the
         left and the page had a tall blank rail down one side, which is
         problem 1 on that issue restated rather than solved: the methodology
         note alone does not fill a column, it just stops it being empty.

         Dependencies move across because they are the same KIND of thing as
         the incident history and the note: context for the verdict on the
         left, rather than part of it. That reads as "us" beside "everything
         else you might want to know", and it balances the two columns.

         It also promotes them. They used to sit about 1200px down the page,
         which is a strange place to keep the answer to "is this Discord's
         fault", the question every reader has about ninety seconds after the
         headline.

         The stacked order the issue asked for is unchanged, because this is
         the first thing in the column: components, dependencies, incidents,
         note. Exactly what shipped before. -->
    <aside class="board-side">

      <section class="card">
        <div class="card-head">
          <h2>${t("Services we depend on")}</h2>
        </div>
        ${UPSTREAMS.map((u) => upstreamRow(t, u, shownUpstreams[u.id])).join("\n        ")}
        <p class="card-note">${t(
          "Read from each company's own status feed. VRCVerify cannot fix these, and " +
            "when one of them is down our rows will usually follow.",
        )}</p>
      </section>

      <h2 class="section-heading">${t("Recent incidents")}</h2>
      ${past.length
        ? past.map((incident) => pastIncident(t, incident)).join("\n      ")
        : '<p class="board-empty">' +
          t(
            "Nothing has gone wrong in the last %{days} days. Anything that does is " +
              "written up here, and stays.",
            { days: t.number(HISTORY_DAYS) },
          ) +
          "</p>"}

      <p class="caveat">${t(
        "Times are UTC. Everything is checked once a minute, and a problem has to show " +
          "up twice in a row before it is published here, so a fault takes about two " +
          "minutes to appear. Verification, the Discord bot and group invites report in " +
          "on their own schedule rather than being reached directly, which can take " +
          "about four. Recoveries are published as soon as they are seen. This page runs " +
          "on Cloudflare, separately from everything it reports on, so that it stays up " +
          "when they do not. Machine readable: %{link}.",
        { link: html('<a href="/api/status.json">/api/status.json</a>') },
      )}</p>
    </aside>
  </div>

</main>

<footer class="site">
  <div class="wrap">
    <nav>
      <a href="https://vrcverify.com/changelog">${t("What's new")}</a>
      <a href="https://vrcverify.com/terms">${t("Terms of Service")}</a>
      <a href="https://vrcverify.com/privacy">${t("Privacy Policy")}</a>
      <a href="https://vrcverify.com/refunds">${t("Refund Policy")}</a>
      <a href="mailto:contact@esattotech.com">${t("Contact")}</a>
    </nav>
    <p>${t("VRCVerify is operated by Esatto Technologies, United States.")}<br>
    ${t("Not affiliated with, endorsed by, or sponsored by VRChat Inc. or Discord Inc.")}</p>
  </div>
</footer>

</body>
</html>
`;
}

/**
 * The admin form. Three fields and a button, and no JavaScript at all.
 *
 * WHAT IT IS FOR: somebody who has just been woken up, holding a phone, on a
 * connection they do not trust, who needs the page to stop saying everything
 * is fine. Every decision below follows from that and from nothing else.
 * There is no rich text, no preview, no incident templates and no attachment.
 * The elaborate version of this feature is the version that does not work at
 * the hour it exists for.
 *
 * It reuses the public stylesheet rather than carrying its own, so it inherits
 * the theme, the type scale and the focus rings, and so that changing a color
 * on the status page cannot leave this one looking like a different product's
 * admin panel.
 */
export function renderAdmin({ incidents, who, now }) {
  const open = incidents.filter((incident) => !incident.resolved_at);

  const openForms = open
    .map(
      (incident) => `
    <section class="card">
      <div class="card-head"><h2>${escapeHtml(incident.title)}</h2>
      <span class="row-since">${escapeHtml(humanDuration(now - incident.started_at))} old</span></div>
      <form method="post" class="admin-form">
        <input type="hidden" name="action" value="update">
        <input type="hidden" name="incident_id" value="${incident.id}">
        <label for="status-${incident.id}">Status</label>
        <select id="status-${incident.id}" name="status">
          ${UPDATE_STATUSES.map(
            (status) => `<option value="${status}">${escapeHtml(status)}</option>`,
          ).join("")}
        </select>
        <label for="body-${incident.id}">What is happening</label>
        <textarea id="body-${incident.id}" name="body" rows="3" required maxlength="2000"></textarea>
        <button type="submit">Post update</button>
        <p class="admin-note">Choosing <strong>resolved</strong> closes the incident and
        clears the banner. There is no separate resolve button on purpose: an incident
        closed by a button nobody typed into ends with no last word on it.</p>
      </form>
    </section>`,
    )
    .join("");

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Post an incident</title>
<meta name="robots" content="noindex">
<link rel="stylesheet" href="/style.css">
<script src="/theme.js"></script>
</head>
<body>

<header class="site">
  <div class="wrap">
    <a class="brand" href="/">${MARK}<span>VRCVerify Status</span></a>
    <nav><a href="/">Public page</a></nav>
    <div class="theme-picker" hidden></div>
  </div>
</header>

<main class="wrap admin-page">
  <h1>Post an incident</h1>
  <p class="lede">Signed in as ${escapeHtml(who)}. Anything posted here is public
  immediately, on the page anybody can read.</p>

  ${openForms}

  <section class="card">
    <div class="card-head"><h2>New incident</h2></div>
    <form method="post" class="admin-form">
      <input type="hidden" name="action" value="open">
      <label for="title">Title</label>
      <input id="title" name="title" required maxlength="120"
             placeholder="Verification is slow">
      <label for="impact">Impact</label>
      <select id="impact" name="impact">
        <option value="degraded">Degraded, it partly works</option>
        <option value="down">Down, it does not work</option>
        <option value="maintenance">Maintenance, this is planned</option>
      </select>
      <label for="new-body">First update</label>
      <textarea id="new-body" name="body" rows="4" required maxlength="2000"
                placeholder="We are looking into it."></textarea>
      <button type="submit">Post incident</button>
    </form>
  </section>

  <p class="caveat">An open incident shows as its own banner on the public page, as
  information. It never changes the status of any service, or the color of anything:
  those come only from what was measured. While one is open the headline says how many
  there are, instead of claiming all is well above your own banner.</p>
</main>

</body>
</html>
`;
}
