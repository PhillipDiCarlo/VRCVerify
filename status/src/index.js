/**
 * The status Worker: one cron that observes, and one page that reports.
 *
 * ORDER OF TRUST, which is the design in one line: what we measured ourselves,
 * then what the homelab told us, then nothing. There is no fourth option where
 * the page fills a gap with a guess.
 */

import {
  COMPONENTS,
  COMPONENT_IDS,
  UPSTREAMS,
  PART_CAPABILITIES,
  HEARTBEAT_STALE_SECONDS,
  PAGE_CACHE_SECONDS,
} from "./config.js";
import {
  capabilitiesFromParts,
  classifyHttp,
  dataFreshness,
  dayKey,
  nextState,
  readStatuspage,
  readStripe,
  worst,
} from "./logic.js";
import { renderPage } from "./render.js";

/** Every id the cron writes a row for: the five public ones and the four upstreams. */
const ALL_IDS = [...COMPONENT_IDS, ...UPSTREAMS.map((u) => u.id)];

/** One statement per state, so no state name is ever pasted into SQL. */
const DAILY_INCREMENTS = {
  up: "INSERT INTO daily (component, day, up) VALUES (?1, ?2, 1) ON CONFLICT (component, day) DO UPDATE SET up = up + 1",
  degraded:
    "INSERT INTO daily (component, day, degraded) VALUES (?1, ?2, 1) ON CONFLICT (component, day) DO UPDATE SET degraded = degraded + 1",
  down: "INSERT INTO daily (component, day, down) VALUES (?1, ?2, 1) ON CONFLICT (component, day) DO UPDATE SET down = down + 1",
  unknown:
    "INSERT INTO daily (component, day, unknown) VALUES (?1, ?2, 1) ON CONFLICT (component, day) DO UPDATE SET unknown = unknown + 1",
};

/**
 * A fetch that cannot hang the whole run.
 *
 * Eight seconds is well past any healthy answer and well inside the Worker's
 * own limits. A probe that times out is a probe that failed, which is the
 * correct reading: a dashboard taking eight seconds to say "ok" is not one a
 * person would call working either.
 */
async function probe(url) {
  try {
    const response = await fetch(url, {
      signal: AbortSignal.timeout(8000),
      headers: { "user-agent": "vrcverify-status/1.0 (+https://status.vrcverify.com)" },
    });
    return {
      response,
      state: classifyHttp({ ok: true, status: response.status }),
      reason: `HTTP ${response.status}`,
    };
  } catch (error) {
    // DNS failure, connection refused, TLS error, timeout. All of them are
    // "a reader would find this broken".
    //
    // THE REASON IS KEPT, and the first version of this function threw it
    // away. A bare `catch {}` here meant that when every probe failed at once
    // the page said "down" and nothing anywhere said why -- which is precisely
    // the 17h43m outage's shape, where every signal read healthy and none of
    // them explained anything. It is private, like every other detail, and
    // reaches the alert rather than the page.
    return { response: null, state: "down", reason: `${error?.name}: ${error?.message}` };
  }
}

async function probeJson(url) {
  const { response, state, reason } = await probe(url);
  if (state === "down" || !response) return { state, body: null, reason };
  try {
    return { state, body: await response.json(), reason };
  } catch {
    // Answered, but not with the JSON its API promises. Not down, and not
    // trustworthy either.
    return { state: "unknown", body: null, reason: "unparseable body" };
  }
}

async function readUpstreams(now, lastUpstreamCheck) {
  // Polled every five minutes rather than every minute. Nothing here changes
  // faster than that in a way we could act on, and four extra requests a
  // minute against someone else's status page, forever, is rude for no gain.
  const due = lastUpstreamCheck === null || now - lastUpstreamCheck >= 300;
  if (!due) return null;

  const entries = await Promise.all(
    UPSTREAMS.map(async (upstream) => {
      const { state, body, reason } = await probeJson(upstream.url);
      if (state === "down") {
        console.log(`upstream ${upstream.id} unreadable: ${reason}`);
        return [upstream.id, "unknown"];
      }
      // THE FEED BEING DOWN IS NOT THE SERVICE BEING DOWN. Discord's status
      // page failing to load tells us nothing about Discord, so it reads as
      // unknown. Only the feed's own contents may say `down`.
      if (!body) return [upstream.id, "unknown"];
      const read =
        upstream.kind === "stripe"
          ? readStripe(body, upstream.services)
          : readStatuspage(body);
      return [upstream.id, read];
    }),
  );
  return Object.fromEntries(entries);
}

async function loadState(db) {
  const { results } = await db
    .prepare("SELECT component, state, since, observed_at, pending, pending_n, detail FROM component_state")
    .all();
  const byId = {};
  for (const row of results ?? []) {
    byId[row.component] = {
      state: row.state,
      since: row.since,
      observedAt: row.observed_at,
      pending: row.pending,
      pendingN: row.pending_n,
      detail: row.detail,
    };
  }
  return byId;
}

async function loadHeartbeats(db) {
  const { results } = await db
    .prepare("SELECT part, at, up, detail FROM heartbeat")
    .all();
  const byPart = {};
  for (const row of results ?? []) {
    byPart[row.part] = { at: row.at, up: row.up === 1, detail: row.detail };
  }
  return byPart;
}

/**
 * One observation per id, from every source that has something to say.
 *
 * A capability with no evidence at all is `unknown`, and that is a real state
 * this page ships in: until the homelab reporter exists (phase 2), three of
 * the five rows have nothing behind them and say so, rather than borrowing
 * confidence from the two that do.
 */
async function observe(env, previousUpstreams, now, lastUpstreamCheck) {
  const evidence = {};
  const details = {};
  const add = (id, state, detail) => {
    (evidence[id] ??= []).push(state);
    if (detail && state !== "up") (details[id] ??= []).push(detail);
  };

  const [dashboard, website, upstreams, heartbeats] = await Promise.all([
    probe(env.DASHBOARD_HEALTH_URL),
    probe(env.WEBSITE_URL),
    readUpstreams(now, lastUpstreamCheck),
    loadHeartbeats(env.DB),
  ]);

  add("dashboard", dashboard.state, `dashboard probe: ${dashboard.reason}`);
  add("website", website.state, `website probe: ${website.reason}`);

  const fromParts = capabilitiesFromParts(
    heartbeats,
    PART_CAPABILITIES,
    now,
    HEARTBEAT_STALE_SECONDS,
  );
  for (const [capability, entry] of Object.entries(fromParts)) {
    add(capability, entry.state, entry.detail);
  }

  // An upstream not polled this minute keeps the answer it gave last time.
  // That is a five minute old reading of somebody else's page, not a guess.
  for (const upstream of UPSTREAMS) {
    const state = upstreams
      ? upstreams[upstream.id]
      : (previousUpstreams[upstream.id]?.state ?? "unknown");
    add(upstream.id, state);
  }

  const observed = {};
  for (const id of ALL_IDS) {
    observed[id] = {
      state: evidence[id]?.length ? worst(evidence[id]) : "unknown",
      detail: details[id]?.join("; ") ?? null,
    };
  }
  return { observed, polledUpstreams: upstreams !== null };
}

/**
 * The cron body. Writes are batched so that a run either lands or does not,
 * rather than leaving half the components describing one minute and half
 * describing another.
 */
async function runCheck(env, now) {
  const [current, lastUpstreamCheck] = await Promise.all([
    loadState(env.DB),
    readMeta(env.DB, "upstreams_checked_at"),
  ]);

  const { observed, polledUpstreams } = await observe(
    env,
    current,
    now,
    lastUpstreamCheck === null ? null : Number(lastUpstreamCheck),
  );

  const day = dayKey(now);
  const statements = [];
  for (const id of ALL_IDS) {
    const decided = nextState(current[id], observed[id].state, now);
    statements.push(
      env.DB.prepare(
        `INSERT INTO component_state (component, state, since, observed_at, pending, pending_n, detail)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
         ON CONFLICT (component) DO UPDATE SET
           state = ?2, since = ?3, observed_at = ?4, pending = ?5, pending_n = ?6, detail = ?7`,
      ).bind(
        id,
        decided.state,
        decided.since,
        now,
        decided.pending,
        decided.pendingN,
        observed[id].detail,
      ),
    );

    // The DAY COUNTER FOLLOWS THE PUBLISHED STATE, not the raw observation.
    // The uptime figure should describe what this page told people, so that a
    // reader who watched it all day and the percentage next to it are describing
    // the same events. A held-back flap never reached a reader and does not
    // belong in their number.
    //
    // The state names a COLUMN, which is the one thing D1's placeholders
    // cannot stand in for, so the four statements are written out and chosen
    // from rather than the name being pasted into SQL. `decided.state` comes
    // from our own logic and not from a request, so this is not fixing a live
    // injection -- it is refusing to leave one string away from being one.
    const counter = DAILY_INCREMENTS[decided.state];
    if (!counter) throw new Error(`unknown state ${decided.state}`);
    statements.push(env.DB.prepare(counter).bind(id, day));

    if (decided.changed) {
      statements.push(
        env.DB.prepare(
          "INSERT INTO transitions (component, state, at, detail) VALUES (?1, ?2, ?3, ?4)",
        ).bind(id, decided.state, now, observed[id].detail),
      );
    }
  }

  if (polledUpstreams) {
    statements.push(
      env.DB.prepare(
        "INSERT INTO meta (key, value) VALUES ('upstreams_checked_at', ?1) " +
          "ON CONFLICT (key) DO UPDATE SET value = ?1",
      ).bind(String(now)),
    );
  }

  await env.DB.batch(statements);
}

async function readMeta(db, key) {
  const row = await db.prepare("SELECT value FROM meta WHERE key = ?1").bind(key).first();
  return row?.value ?? null;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": `public, max-age=${PAGE_CACHE_SECONDS}`,
      // Anyone may read this. It is the same information as the page, and a
      // dashboard somewhere else showing our status is a good outcome.
      "access-control-allow-origin": "*",
    },
  });
}

const SECURITY_HEADERS = {
  // Same posture as the dashboard's: nothing loads from anywhere but here.
  // The page has no inline script and no third party anything, so this is the
  // strictest policy it can hold rather than the strictest it can tolerate.
  "content-security-policy":
    "default-src 'none'; style-src 'self'; script-src 'self'; font-src 'self'; " +
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "strict-transport-security": "max-age=31536000; includeSubDomains",
};

/** What both surfaces read, with the freshness rule applied once, here. */
async function present(env, now) {
  try {
    const rows = await loadState(env.DB);
    const observedAts = Object.values(rows)
      .map((r) => r.observedAt)
      .filter((v) => typeof v === "number");
    const checkedAt = observedAts.length ? Math.max(...observedAts) : null;
    const components = {};
    for (const id of COMPONENT_IDS) {
      components[id] = rows[id] ? { state: rows[id].state, since: rows[id].since } : undefined;
    }
    const upstreams = {};
    for (const upstream of UPSTREAMS) {
      upstreams[upstream.id] = rows[upstream.id]
        ? { state: rows[upstream.id].state }
        : undefined;
    }
    return { components, upstreams, checkedAt, freshness: dataFreshness(checkedAt, now) };
  } catch {
    // D1 unreachable. The page still renders and says so. Returning a 500 here
    // would be the status page taking itself down, which is a poor answer to
    // "is anything working".
    return { components: {}, upstreams: {}, checkedAt: null, freshness: "unavailable" };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = Math.floor(Date.now() / 1000);

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }

    if (url.pathname === "/api/status.json") {
      const state = await present(env, now);
      const trusted = state.freshness === "fresh";
      // The JSON carries exactly what the page carries. No detail column, no
      // part names: an infrastructure name that is too sensitive to render is
      // not less sensitive for being served as application/json.
      return jsonResponse({
        updated_at: state.checkedAt,
        freshness: state.freshness,
        services: COMPONENTS.map((c) => ({
          id: c.id,
          name: c.name,
          state: trusted ? (state.components[c.id]?.state ?? "unknown") : "unknown",
          since: state.components[c.id]?.since ?? null,
        })),
        dependencies: UPSTREAMS.map((u) => ({
          id: u.id,
          name: u.name,
          state: trusted ? (state.upstreams[u.id]?.state ?? "unknown") : "unknown",
        })),
      });
    }

    if (url.pathname === "/") {
      const state = await present(env, now);
      return new Response(renderPage({ ...state, now }), {
        headers: {
          "content-type": "text/html; charset=utf-8",
          // Half a minute. Long enough that a burst of readers during an
          // outage is answered from cache instead of from D1, short enough
          // that nobody is looking at a materially old page.
          "cache-control": `public, max-age=${PAGE_CACHE_SECONDS}`,
          ...SECURITY_HEADERS,
        },
      });
    }

    return new Response("Not found", {
      status: 404,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCheck(env, Math.floor(event.scheduledTime / 1000)));
  },
};
