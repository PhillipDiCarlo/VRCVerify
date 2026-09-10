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
  headlineWithOpenIncidents,
  humanDuration,
  uptimeOverDays,
  verdict,
} from "./logic.js";

const STATE_LABEL = {
  up: "Operational",
  degraded: "Degraded",
  down: "Down",
  unknown: "Unknown",
};

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
function historyStrip(days, rows) {
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
        excluded > 0 ? ` (${humanDuration(excluded * 60)} of maintenance not counted)` : "";
      const label =
        percent === null
          ? `${day}: ${state === "maintenance" ? "maintenance all day" : "no data"}`
          : `${day}: ${percent.toFixed(percent === 100 ? 0 : 2)}% up${note}`;
      return `<span class="bar is-${state}" title="${escapeHtml(label)}"></span>`;
    })
    .join("");
  return `<div class="bars" aria-hidden="true">${bars}</div>`;
}

/** "99.98% over 90 days", or an honest sentence when there is nothing to average. */
function uptimeLine(days, rows) {
  const present = days.map((day) => rows?.[day]).filter(Boolean);
  const percent = uptimeOverDays(present);
  const troubled = present.filter((row) => (row.down ?? 0) + (row.degraded ?? 0) > 0).length;
  if (percent === null) {
    return `<span class="row-uptime">No history yet</span>`;
  }
  // Two decimals, and never rounded UP to 100. A page that says 100% on a day
  // it was down for four minutes is a page that has started lying politely.
  const shown = percent >= 99.995 && percent < 100 ? "99.99" : percent.toFixed(2);
  const summary =
    troubled === 0
      ? `${shown}% uptime over the last ${days.length} days, with no incidents`
      : `${shown}% uptime over the last ${days.length} days, with ${troubled} ` +
        `${troubled === 1 ? "day" : "days"} affected`;
  return `<span class="row-uptime" title="${escapeHtml(summary)}">${shown}%</span>` +
    `<span class="visually-hidden">${escapeHtml(summary)}</span>`;
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
function barsLegend(dayCount) {
  // CLAMPED TO WHAT IS ACTUALLY THERE. `recentDays` always returns exactly
  // HISTORY_DAYS in production, so the window is always the smaller number --
  // but a strip holding four days under a label reading "60 days ago" would be
  // the page overstating its own evidence, and that is the one thing the axis
  // must never do. It costs a Math.min.
  const wide = Math.min(WIDE_DAYS, dayCount);
  const narrow = Math.min(NARROW_DAYS, dayCount);
  return (
    '<div class="bars-legend" aria-hidden="true"><span>' +
    `<span class="at-wide">${wide}</span>` +
    `<span class="at-narrow">${narrow}</span> days ago</span>` +
    "<span>Today</span></div>"
  );
}

function pill(state) {
  return `<span class="pill">${STATE_LABEL[state] ?? STATE_LABEL.unknown}</span>`;
}

function componentRow(component, entry, now, history) {
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
  const duration =
    timed && entry?.since
      ? `<span class="row-since">for ${escapeHtml(humanDuration(now - entry.since))}</span>`
      : "";
  return (
    `<div class="row is-${state}">` +
    `<span class="row-name">${glyph(state)}<span>${escapeHtml(component.name)}</span></span>` +
    `<span class="row-state">${duration}` +
    (history ? uptimeLine(history.days, history.byComponent[component.id]) : "") +
    `${pill(state)}</span>` +
    `<p class="row-desc">${escapeHtml(component.description)}</p>` +
    (history ? historyStrip(history.days, history.byComponent[component.id]) : "") +
    "</div>"
  );
}

function upstreamRow(upstream, entry) {
  const state = entry?.state ?? "unknown";
  return (
    `<div class="row is-${state}">` +
    `<span class="row-name">${glyph(state)}<span>${escapeHtml(upstream.name)}</span></span>` +
    `<span class="row-state">${pill(state)}</span>` +
    `<p class="row-desc">${escapeHtml(upstream.why)} ` +
    `<a class="dep-link" href="${escapeHtml(upstream.href)}" rel="noopener">` +
    `${escapeHtml(upstream.name)}'s own status page</a></p>` +
    "</div>"
  );
}

const IMPACT_STATE = { maintenance: "degraded", degraded: "degraded", down: "down" };

function updateItem(update) {
  return (
    '<li class="update">' +
    `<span class="update-status">${escapeHtml(update.status)}</span>` +
    `<time datetime="${new Date(update.at * 1000).toISOString()}">${utcStamp(update.at)}</time>` +
    `<p>${escapeHtml(update.body)}</p></li>`
  );
}

function updateList(updates) {
  return `<ol class="updates">${updates.map(updateItem).join("")}</ol>`;
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
function foldedUpdates(updates, label) {
  if (!updates.length) return "";
  const count = `${updates.length} ${label}${updates.length === 1 ? "" : "s"}`;
  return `<details class="timeline"><summary>${count}</summary>${updateList(updates)}</details>`;
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
function openIncident(incident, now) {
  const state = IMPACT_STATE[incident.impact] ?? "degraded";
  const [latest, ...earlier] = incident.updates;
  return (
    `<section class="card incident is-${state}">` +
    '<div class="card-head">' +
    `<h2>${escapeHtml(incident.title)}</h2>` +
    `<span class="pill">${STATE_LABEL[state] ?? "Degraded"}</span></div>` +
    `<p class="incident-meta">Ongoing for ${escapeHtml(humanDuration(now - incident.started_at))}. ` +
    `Started <time datetime="${new Date(incident.started_at * 1000).toISOString()}">` +
    `${utcStamp(incident.started_at)}</time>.</p>` +
    (latest ? updateList([latest]) : "") +
    foldedUpdates(earlier, "earlier update") +
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
function pastIncident(incident) {
  return (
    '<section class="card incident is-resolved">' +
    '<div class="card-head">' +
    `<h2>${escapeHtml(incident.title)}</h2>` +
    '<span class="pill">Resolved</span></div>' +
    `<p class="incident-meta">Resolved after ` +
    `${escapeHtml(humanDuration(incident.resolved_at - incident.started_at))}. Started ` +
    `<time datetime="${new Date(incident.started_at * 1000).toISOString()}">` +
    `${utcStamp(incident.started_at)}</time>.</p>` +
    foldedUpdates(incident.updates, "update") +
    "</section>"
  );
}

/**
 * @param components  {id: {state, since}} for the five capabilities. PRIVATE
 *                    detail fields are not read here and must not be passed.
 * @param upstreams   {id: {state}} for the four dependencies.
 * @param checkedAt   unix seconds of the last completed check, or null.
 * @param freshness   "fresh" | "stale" | "missing", from logic.dataFreshness.
 */
export function renderPage({
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
        headline:
          freshness === "missing"
            ? "No check has run yet"
            : freshness === "unavailable"
              ? "Status cannot be read right now"
              : "Status is out of date",
      };

  const checkedLine =
    checkedAt === null
      ? "No check has completed yet."
      : `Checked ${escapeHtml(humanDuration(now - checkedAt))} ago, at ` +
        `<time datetime="${new Date(checkedAt * 1000).toISOString()}">${utcStamp(checkedAt)}</time>.`;

  const WARNINGS = {
    // Each one says what is broken, what the page is doing about it, and what
    // it does NOT imply. The last part matters: a reader who sees a wall of
    // gray needs to be told that gray is a statement about this page rather
    // than about the bot.
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
  const staleWarning = trusted
    ? ""
    : `<p class="card-note">${WARNINGS[freshness] ?? WARNINGS.missing}</p>`;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VRCVerify Status</title>
<meta name="description" content="Whether VRCVerify's verification, bot, invites, dashboard and website are working, and whether the services they depend on are.">
<link rel="stylesheet" href="/style.css">
<script src="/theme.js"></script>
</head>
<body>

<header class="site">
  <div class="wrap">
    <a class="brand" href="/">VRCVerify Status</a>
    <nav>
      <a href="https://vrcverify.com/">Home</a>
      <a href="https://dashboard.vrcverify.com/">Dashboard</a>
    </nav>
    <div class="theme-picker" hidden></div>
  </div>
</header>

<main class="wrap">

  <div class="hero is-${overall.level}">
    ${glyph(overall.level, "hero-glyph")}
    <h1>${escapeHtml(overall.headline)}</h1>
    <p class="hero-checked">${checkedLine}</p>
  </div>

  ${open.map((incident) => openIncident(incident, now)).join("\n  ")}

  <div class="board">
    <div class="board-main">

      <section class="card">
        <div class="card-head">
          <h2>VRCVerify</h2>
          <span class="is-${overall.level} pill">${STATE_LABEL[overall.level]}</span>
        </div>
        ${COMPONENTS.map((c) => componentRow(c, shown[c.id], now, history)).join("\n        ")}
        ${history ? barsLegend(history.days.length) : ""}
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
          <h2>Services we depend on</h2>
        </div>
        ${UPSTREAMS.map((u) => upstreamRow(u, shownUpstreams[u.id])).join("\n        ")}
        <p class="card-note">Read from each company's own status feed. VRCVerify cannot
        fix these, and when one of them is down our rows will usually follow.</p>
      </section>

      <h2 class="section-heading">Recent incidents</h2>
      ${past.length
        ? past.map((incident) => pastIncident(incident)).join("\n      ")
        : '<p class="board-empty">Nothing has gone wrong in the last ' +
          `${HISTORY_DAYS} days. Anything that does is written up here, and stays.</p>`}

      <p class="caveat">Times are UTC. Everything is checked once a minute, and a
      problem has to show up twice in a row before it is published here, so a fault
      takes about two minutes to appear. Verification, the Discord bot and group
      invites report in on their own schedule rather than being reached directly,
      which can take about four. Recoveries are published as soon as they are seen.
      This page runs on Cloudflare, separately from everything it reports on, so
      that it stays up when they do not. Machine readable:
      <a href="/api/status.json">/api/status.json</a>.</p>
    </aside>
  </div>

</main>

<footer class="site">
  <div class="wrap">
    <nav>
      <a href="https://vrcverify.com/changelog">What's new</a>
      <a href="https://vrcverify.com/terms">Terms of Service</a>
      <a href="https://vrcverify.com/privacy">Privacy Policy</a>
      <a href="https://vrcverify.com/refunds">Refund Policy</a>
      <a href="mailto:contact@esattotech.com">Contact</a>
    </nav>
    <p>VRCVerify is operated by Esatto Technologies, United States.<br>
    Not affiliated with, endorsed by, or sponsored by VRChat Inc. or Discord Inc.</p>
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
    <a class="brand" href="/">VRCVerify Status</a>
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
