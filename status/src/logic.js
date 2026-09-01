/**
 * Every decision the status page makes, as functions with no I/O.
 *
 * The Worker fetches and writes; this file decides. That split is not
 * ceremony: Node can run these directly (`node --test status/test`), and a
 * status page whose rules are only exercised in production is a status page
 * whose first real test is an outage.
 *
 * THE RULE THAT OUTRANKS THE OTHERS: never say `up` from missing data.
 *
 * Absent, stale and unparseable all resolve to `unknown` or to `down`, never
 * to `up`. A page that shows green because a probe failed to run is worse than
 * no page, because it is believed. The incident.io reference draws "no data
 * available" as its own state for the same reason, rather than filling the gap
 * with the colour everybody wants to see.
 */

/**
 * Ordered worst first. Everything that compares two states uses this array and
 * not a pile of if statements, so there is one place to look when asking what
 * beats what.
 *
 * `unknown` sits below `degraded` on purpose. "We could not check" is a weaker
 * claim than "we checked and it was bad", and letting it outrank a real
 * observation would let one silent probe repaint a page full of good ones.
 */
export const SEVERITY = ["down", "degraded", "unknown", "up"];

export function worst(states) {
  for (const candidate of SEVERITY) {
    if (states.includes(candidate)) return candidate;
  }
  return "unknown";
}

/**
 * Our own endpoints: what an HTTP answer means.
 *
 * A timeout or a refused connection arrives here as `ok: false` with no status
 * and means `down`. 5xx means down as well: the origin answered, but with the
 * thing a reader would experience as broken. Anything else unexpected is
 * `degraded` rather than down, because a 403 from the edge is a story about
 * Cloudflare's bot protection and not proof the service behind it has stopped.
 * That exact confusion is SECURITY_AUDIT A-25.
 */
export function classifyHttp({ ok, status }) {
  if (!ok || typeof status !== "number") return "down";
  if (status >= 200 && status < 300) return "up";
  if (status >= 500) return "down";
  return "degraded";
}

/**
 * Statuspage's /api/v2/status.json, which Discord, VRChat and Cloudflare all
 * answer. The vocabulary is fixed by Statuspage: none, minor, major, critical.
 *
 * `minor` maps to degraded and not to down. Statuspage calls a single region
 * being slow "minor", and repainting our page red for that would train people
 * to ignore red.
 */
export function readStatuspage(body) {
  const indicator = body?.status?.indicator;
  if (indicator === "none") return "up";
  if (indicator === "minor") return "degraded";
  if (indicator === "major" || indicator === "critical") return "down";
  // Includes `maintenance`, which Statuspage documents but which none of the
  // three has ever sent us, and includes the shape changing under us.
  return "unknown";
}

/**
 * Stripe's /current, which is not Statuspage and carries no promise of
 * stability. Checked 2026-08-31: `{"statuses": {"api": "up", ...},
 * "largestatus": "up", "message": ..., "time": ...}`.
 *
 * Only the named services are read. If none of them are present the answer is
 * `unknown`, which is the honest reading of "the feed changed shape" and is
 * what stops a silent format change from being drawn as a healthy Stripe.
 */
export function readStripe(body, services) {
  const statuses = body?.statuses;
  if (!statuses || typeof statuses !== "object") return "unknown";
  const seen = services
    .filter((name) => typeof statuses[name] === "string")
    .map((name) => statuses[name].toLowerCase());
  if (seen.length === 0) return "unknown";
  if (seen.some((s) => s === "down" || s === "outage")) return "down";
  if (seen.every((s) => s === "up")) return "up";
  // "degraded", "partial", "maintenance", or a word we have not met. Something
  // is being said about a service we depend on, and it is not "up".
  return "degraded";
}

/**
 * Publish an observation, or hold it back.
 *
 * Down and degraded need to be seen twice in a row before the page says so.
 * Recovery is published on the first sight of it. The asymmetry is deliberate
 * and is written out in schema.sql: being slow to say "down" costs a minute,
 * being slow to say "back" costs a reader's trust in the page.
 *
 * `unknown` is held to the same two-in-a-row rule as down. One skipped probe
 * should not blank a row that was green a minute ago.
 *
 * Returns the row to store plus whether this call is the moment of change,
 * which is what the alerting keys off. Pure: the caller supplies `now`.
 */
export function nextState(current, observed, now, { confirmations = 2 } = {}) {
  const previous = current?.state ?? "unknown";
  const since = current?.since ?? now;

  if (observed === previous) {
    return { state: previous, since, pending: null, pendingN: 0, changed: false };
  }

  // Getting better, or leaving `unknown` for anything at all: publish it now.
  // Leaving unknown is included because a row that says "we cannot tell" has
  // no credibility left to protect by waiting.
  const improving =
    SEVERITY.indexOf(observed) > SEVERITY.indexOf(previous) || previous === "unknown";
  if (improving) {
    return { state: observed, since: now, pending: null, pendingN: 0, changed: true };
  }

  const runLength = current?.pending === observed ? (current.pendingN ?? 0) + 1 : 1;
  if (runLength >= confirmations) {
    return { state: observed, since: now, pending: null, pendingN: 0, changed: true };
  }
  return { state: previous, since, pending: observed, pendingN: runLength, changed: false };
}

/**
 * The homelab's private parts, folded into the public capabilities (phase 2).
 *
 * Silence is the interesting case. A part whose heartbeat is older than the
 * stale threshold is `down`, not `unknown`: the reporter's whole job is to
 * speak every minute, so it not speaking is information. A part that has never
 * reported at all IS `unknown` -- that is a feature that has not been deployed
 * yet, not a feature that has failed.
 */
export function capabilitiesFromParts(heartbeats, mapping, now, staleSeconds) {
  const partState = {};
  for (const part of Object.keys(mapping)) {
    const beat = heartbeats[part];
    if (!beat) {
      // NOT `unknown`, and not counted at all. A part that has never once
      // reported is a part that has not been deployed yet, and letting it vote
      // would drag every capability it touches to unknown for as long as the
      // reporter is unbuilt -- including capabilities a public probe can
      // answer perfectly well on its own. Silence only means something once
      // there has been a voice: from the first heartbeat onward a row exists
      // forever, and staleness is read as `down` below.
      continue;
    }
    const age = now - beat.at;
    if (age > staleSeconds) {
      partState[part] = { state: "down", detail: `no report for ${age}s` };
      continue;
    }
    partState[part] = {
      state: beat.up ? "up" : "down",
      detail: beat.detail ?? null,
    };
  }

  const out = {};
  for (const [part, capabilities] of Object.entries(mapping)) {
    if (!partState[part]) continue;
    for (const entry of capabilities) {
      // A plain string means "this part's state IS this capability's state".
      // An object may cap how bad it is allowed to make the row -- see the
      // dashboard's entry in config.js for the case that needs it.
      const capability = typeof entry === "string" ? entry : entry.capability;
      const ceiling = typeof entry === "string" ? null : entry.as;
      let state = partState[part].state;
      if (ceiling && SEVERITY.indexOf(state) < SEVERITY.indexOf(ceiling)) {
        state = ceiling;
      }
      const bucket = (out[capability] ??= { states: [], details: [] });
      bucket.states.push(state);
      if (partState[part].state !== "up") {
        // The detail is PRIVATE and exists for the alert. It names the part on
        // purpose, which is exactly why it must not reach the page.
        bucket.details.push(
          `${part}: ${partState[part].detail ?? partState[part].state}`,
        );
      }
    }
  }

  const result = {};
  for (const [capability, bucket] of Object.entries(out)) {
    result[capability] = {
      state: worst(bucket.states),
      detail: bucket.details.length ? bucket.details.join("; ") : null,
    };
  }
  return result;
}

/**
 * The sentence at the top of the page.
 *
 * There is no wording for "everything is fine except the parts we could not
 * check", because that is not fine and the headline should not imply it is.
 */
export function verdict(states) {
  const overall = worst(states);
  if (overall === "up") {
    return { level: "up", headline: "All systems operational" };
  }
  if (overall === "down") {
    const allDown = states.every((s) => s === "down");
    return {
      level: "down",
      headline: allDown ? "Everything is down" : "Some services are down",
    };
  }
  if (overall === "degraded") {
    return { level: "degraded", headline: "Some services are degraded" };
  }
  return { level: "unknown", headline: "Service status is unknown" };
}

/**
 * A day's worth of counters, as a percentage and a colour.
 *
 * Minutes we could not observe are excluded from the denominator rather than
 * counted as downtime. A day with nothing but `unknown` has no percentage at
 * all -- `null`, drawn as a gap -- because averaging over no observations
 * produces a number that looks like measurement and is not.
 */
export function dayUptime(row) {
  const observed = (row?.up ?? 0) + (row?.degraded ?? 0) + (row?.down ?? 0);
  if (observed === 0) return { percent: null, state: "unknown" };
  const state = row.down > 0 ? "down" : row.degraded > 0 ? "degraded" : "up";
  return { percent: (row.up / observed) * 100, state };
}

/** The same, across a run of days. Days with no observations are skipped. */
export function uptimeOverDays(rows) {
  let up = 0;
  let observed = 0;
  for (const row of rows) {
    up += row.up ?? 0;
    observed += (row.up ?? 0) + (row.degraded ?? 0) + (row.down ?? 0);
  }
  if (observed === 0) return null;
  return (up / observed) * 100;
}

/** UTC day key, `YYYY-MM-DD`. The whole page is UTC; see render.js. */
export function dayKey(unixSeconds) {
  return new Date(unixSeconds * 1000).toISOString().slice(0, 10);
}

/** The last `count` day keys ending at `unixSeconds`, oldest first. */
export function recentDays(unixSeconds, count) {
  const days = [];
  for (let i = count - 1; i >= 0; i -= 1) {
    days.push(dayKey(unixSeconds - i * 86400));
  }
  return days;
}

/**
 * "4 minutes", "3 hours", "2 days". Coarse on purpose: a status page saying
 * "1 hour 22 minutes 9 seconds" is showing off a precision that the one minute
 * observation interval does not support.
 */
export function humanDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 60) return "less than a minute";
  const units = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [name, size] of units) {
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      return `${n} ${name}${n === 1 ? "" : "s"}`;
    }
  }
  return "less than a minute";
}

/**
 * Whether the stored data may be believed.
 *
 * The cron runs every minute. Five minutes of silence means the checker itself
 * has stopped, and at that point the last thing it wrote is not the truth, it
 * is a photograph of the truth taken at an unknown time. The page draws
 * `stale` as unknown rather than as whatever the photograph shows, which is
 * the same rule as never rendering green from missing data, applied to the one
 * failure that would otherwise route around it: the checker being the thing
 * that broke.
 */
export function dataFreshness(checkedAt, now, { staleAfter = 300 } = {}) {
  if (checkedAt === null || checkedAt === undefined) return "missing";
  return now - checkedAt > staleAfter ? "stale" : "fresh";
}

/**
 * `t=1788233609,v1=<hex>`, which is Stripe's scheme and therefore the one this
 * project already reasons about (see src/dashboard/stripe_events.py).
 *
 * Returns null for anything it does not fully understand. A signature header
 * that is nearly right is not nearly authentic.
 */
export function parseSignatureHeader(header) {
  if (typeof header !== "string") return null;
  const fields = {};
  for (const piece of header.split(",")) {
    const [key, value] = piece.split("=", 2);
    if (key && value !== undefined) fields[key.trim()] = value.trim();
  }
  const timestamp = Number(fields.t);
  const signature = fields.v1;
  if (!Number.isInteger(timestamp) || !/^[0-9a-f]{64}$/.test(signature ?? "")) return null;
  return { timestamp, signature };
}

/**
 * A signature is only good for five minutes either way.
 *
 * Without this a captured report is replayable forever, and for this endpoint
 * that means pinning the page at "everything is fine" while the homelab is
 * dark -- the exact lie the whole design exists to prevent. The future half of
 * the window exists because the homelab's clock is not ours to trust; five
 * minutes of skew is generous and still far short of useful to an attacker.
 */
export function signatureIsTimely(timestamp, now, { tolerance = 300 } = {}) {
  return Math.abs(now - timestamp) <= tolerance;
}

/**
 * Validate a report body against the parts we are willing to hear about.
 *
 * An unrecognised part name is DROPPED rather than rejected or stored. Stored,
 * it would let whatever holds the signing key invent rows on a public page.
 * Rejecting the whole report would mean a newer reporter, deployed before this
 * Worker knows about its new part, silently stops reporting the parts this
 * Worker does understand -- which is an outage in the reporting caused by an
 * upgrade, and those are the worst kind to diagnose.
 */
export function readReport(body, allowedParts, { maxDetail = 200 } = {}) {
  const parts = body?.parts;
  if (!parts || typeof parts !== "object") return null;
  const clean = [];
  for (const [name, value] of Object.entries(parts)) {
    if (!allowedParts.includes(name)) continue;
    if (!value || typeof value !== "object" || typeof value.up !== "boolean") continue;
    const detail = typeof value.detail === "string" ? value.detail.slice(0, maxDetail) : null;
    clean.push({ part: name, up: value.up, detail });
  }
  return clean.length ? clean : null;
}

/**
 * Which state changes are worth waking somebody for.
 *
 * OUR OWN SERVICES: every change, in both directions. Recovery matters as much
 * as failure -- an alert that only ever fires on the way down leaves you
 * refreshing a page to find out whether it is over.
 *
 * SOMEBODY ELSE'S: only `down`, and only on the way in and out of it.
 * Cloudflare's status page sits at `minor` for hours at a time over things
 * that never touch us, and an alert that fires for those is an alert that gets
 * muted, which costs the ones that matter.
 *
 * `unknown` never alerts on its own. It means the checker could not look, and
 * a page that pages you because it briefly could not see is a page you turn
 * off. The exception is the whole-checker case, which the freshness rule
 * covers on the page itself.
 */
export function isAlertable(change, ownIds) {
  if (change.state === "unknown") return false;
  if (ownIds.includes(change.component)) return true;
  return change.state === "down" || change.from === "down";
}

/**
 * One message for a whole cron run, not one per row.
 *
 * The database going down takes four capabilities with it, and four separate
 * alerts for one event is how a person learns to ignore the fourth. The title
 * names the worst thing that happened; the body lists everything.
 */
export function composeAlert(changes, names, now) {
  if (changes.length === 0) return null;
  const severity = worst(changes.map((c) => c.state));
  const recovered = changes.filter((c) => c.state === "up");
  const broken = changes.filter((c) => c.state !== "up");

  const label = (change) => names[change.component] ?? change.component;

  // THE TITLE NAMES ONLY WHAT MATCHES THE WORD IT USES. The first version read
  // "Down: Verification, Discord bot, Group invites, Dashboard and sign-in"
  // when the dashboard was merely degraded -- the title asserted an outage for
  // a service that was still serving pages. Anything at a lesser severity is
  // counted rather than named, and the body still lists all of it.
  const atWorst = broken.filter((change) => change.state === severity);
  const lesser = broken.length - atWorst.length;

  const title =
    broken.length === 0
      ? `Recovered: ${recovered.map(label).join(", ")}`
      : `${severity === "down" ? "Down" : "Degraded"}: ${atWorst.map(label).join(", ")}` +
        (lesser ? ` (+${lesser} degraded)` : "");

  const lines = changes.map((change) => {
    const name = names[change.component] ?? change.component;
    // The detail names infrastructure, which is the entire reason this goes to
    // a private channel and the page does not. It is also the only part of
    // this message that saves anybody a login.
    const detail = change.detail ? ` -- ${change.detail}` : "";
    return `${name}: ${change.from ?? "unknown"} -> ${change.state}${detail}`;
  });

  return { title, lines, severity, at: now };
}
