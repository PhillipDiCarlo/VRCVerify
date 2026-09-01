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

import { COMPONENTS, UPSTREAMS } from "./config.js";
import { verdict, humanDuration } from "./logic.js";

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

function pill(state) {
  return `<span class="pill">${STATE_LABEL[state] ?? STATE_LABEL.unknown}</span>`;
}

function componentRow(component, entry, now) {
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
    `<span class="row-state">${duration}${pill(state)}</span>` +
    `<p class="row-desc">${escapeHtml(component.description)}</p>` +
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

/**
 * @param components  {id: {state, since}} for the five capabilities. PRIVATE
 *                    detail fields are not read here and must not be passed.
 * @param upstreams   {id: {state}} for the four dependencies.
 * @param checkedAt   unix seconds of the last completed check, or null.
 * @param freshness   "fresh" | "stale" | "missing", from logic.dataFreshness.
 */
export function renderPage({ components, upstreams, checkedAt, now, freshness }) {
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

  const overall = trusted
    ? verdict(COMPONENTS.map((c) => shown[c.id]?.state ?? "unknown"))
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

  <section class="card">
    <div class="card-head">
      <h2>VRCVerify</h2>
      <span class="is-${overall.level} pill">${STATE_LABEL[overall.level]}</span>
    </div>
    ${COMPONENTS.map((c) => componentRow(c, shown[c.id], now)).join("\n    ")}
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

  <p class="caveat">Times are UTC. Every service is checked once a minute, so a
  problem can be up to a minute old before it appears here. This page runs on
  Cloudflare, separately from everything it reports on, so that it stays up when
  they do not. Machine readable: <a href="/api/status.json">/api/status.json</a>.</p>

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
