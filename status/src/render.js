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

import { COMPONENTS, DAY_RED_BELOW_PERCENT, UPSTREAMS } from "./config.js";
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
 * The four glyphs, so that state is never carried by colour alone.
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
 * Ninety days, one bar each, oldest on the left.
 *
 * THE BARS ARE NOT THE ACCESSIBLE VERSION OF ANYTHING. They are a shape, and a
 * shape is exactly what a reader wants at a glance: is this a wall of green
 * with one notch in it, or a mess. The facts underneath -- the percentage, and
 * how many days had trouble -- are written out in text beside them, and the
 * strip itself is hidden from assistive technology rather than being narrated
 * as ninety anonymous list items.
 *
 * A day with no observations is drawn in the track colour, distinct from both
 * green and red. It has to be: this page will have exactly that for its first
 * eighty-nine days, and drawing "we did not exist yet" as either health or
 * failure would be inventing history.
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
    (history
      ? historyStrip(history.days, history.byComponent[component.id]) +
        '<div class="bars-scale" aria-hidden="true">' +
        `<span>${history.days.length} days ago</span><span>Today</span></div>`
      : "") +
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

/**
 * The banner, following the incident.io reference: what is wrong, how long it
 * has been wrong, and the latest thing anybody said about it.
 *
 * The updates read newest first. Somebody arriving mid-incident wants the
 * current position, not the archaeology, and a reverse-chronological list is
 * the only arrangement where the useful line is above the fold on a phone.
 */
function incidentBanner(incident, now) {
  // A FINISHED INCIDENT IS DRAWN NEUTRAL, not in the colour of the trouble it
  // used to be. The history section was shipping a red-bordered, red-tinted
  // card for something that had been fixed hours earlier -- alarming at a
  // glance, and wrong the moment anybody read the date. The state colour is
  // for things that are happening now; what a closed one needs to say is how
  // long it lasted.
  const done = Boolean(incident.resolved_at);
  const state = done ? "resolved" : (IMPACT_STATE[incident.impact] ?? "degraded");
  const running = done
    ? `Resolved after ${escapeHtml(humanDuration(incident.resolved_at - incident.started_at))}`
    : `Ongoing for ${escapeHtml(humanDuration(now - incident.started_at))}`;
  const updates = incident.updates
    .map(
      (update) =>
        '<li class="update">' +
        `<span class="update-status">${escapeHtml(update.status)}</span>` +
        `<time datetime="${new Date(update.at * 1000).toISOString()}">${utcStamp(update.at)}</time>` +
        `<p>${escapeHtml(update.body)}</p></li>`,
    )
    .join("");
  return (
    `<section class="card incident is-${state}">` +
    '<div class="card-head">' +
    `<h2>${escapeHtml(incident.title)}</h2>` +
    `<span class="pill">${done ? "Resolved" : (STATE_LABEL[state] ?? "Degraded")}</span></div>` +
    `<p class="incident-meta">${running}. Started ` +
    `<time datetime="${new Date(incident.started_at * 1000).toISOString()}">` +
    `${utcStamp(incident.started_at)}</time>.</p>` +
    `<ol class="updates">${updates}</ol>` +
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
  // ago knows no more about Discord than it does about us. Half a page of grey
  // rows beside four confident green ones would be worse than either, because
  // it reads as "our stuff is unknown, theirs is fine" -- a claim nobody made.
  const shownUpstreams = {};
  for (const upstream of UPSTREAMS) {
    shownUpstreams[upstream.id] = trusted ? upstreams[upstream.id] : { state: "unknown" };
  }

  const open = (incidents ?? []).filter((incident) => !incident.resolved_at);
  // THE COLOUR IS MEASURED, NEVER TYPED. An incident is a person's words, and
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
    // grey needs to be told that grey is a statement about this page rather
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

  ${open.map((incident) => incidentBanner(incident, now)).join("\n  ")}

  <section class="card">
    <div class="card-head">
      <h2>VRCVerify</h2>
      <span class="is-${overall.level} pill">${STATE_LABEL[overall.level]}</span>
    </div>
    ${COMPONENTS.map((c) => componentRow(c, shown[c.id], now, history)).join("\n    ")}
    ${staleWarning}
  </section>

  <section class="card">
    <div class="card-head">
      <h2>Services we depend on</h2>
    </div>
    ${UPSTREAMS.map((u) => upstreamRow(u, shownUpstreams[u.id])).join("\n    ")}
    <p class="card-note">Read from each company's own status feed. VRCVerify cannot
    fix these, and when one of them is down the rows above will usually follow.</p>
  </section>

  ${(incidents ?? []).some((incident) => incident.resolved_at)
    ? '<h2 class="section-heading">Recent incidents</h2>' +
      (incidents ?? [])
        .filter((incident) => incident.resolved_at)
        .map((incident) => incidentBanner(incident, now))
        .join("")
    : ""}

  <p class="caveat">Times are UTC. Everything is checked once a minute, and a
  problem has to show up twice in a row before it is published here, so a fault
  takes about two minutes to appear. Verification, the Discord bot and group
  invites report in on their own schedule rather than being reached directly,
  which can take about four. Recoveries are published as soon as they are seen.
  This page runs on Cloudflare, separately from everything it reports on, so
  that it stays up when they do not. Machine readable:
  <a href="/api/status.json">/api/status.json</a>.</p>

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
 * the theme, the type scale and the focus rings, and so that changing a colour
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

<main class="wrap">
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
  information. It never changes the status of any service, or the colour of anything:
  those come only from what was measured. While one is open the headline says how many
  there are, instead of claiming all is well above your own banner.</p>
</main>

</body>
</html>
`;
}
