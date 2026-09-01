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
  HISTORY_DAYS,
  ALERT_COLOURS,
} from "./config.js";
import {
  capabilitiesFromParts,
  composeAlert,
  isAlertable,
  recentDays,
  parseSignatureHeader,
  readReport,
  signatureIsTimely,
  classifyHttp,
  dataFreshness,
  dayKey,
  nextState,
  readStatuspage,
  readStripe,
  worst,
} from "./logic.js";
import {
  readIncidentForm,
  readUpdateForm,
  verdictWithIncidents,
} from "./logic.js";
import { verifyAccessToken } from "./access.js";
import { renderAdmin, renderPage } from "./render.js";

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
  const [current, lastUpstreamCheck, prunedAt] = await Promise.all([
    loadState(env.DB),
    readMeta(env.DB, "upstreams_checked_at"),
    readMeta(env.DB, "pruned_at"),
  ]);

  const { observed, polledUpstreams } = await observe(
    env,
    current,
    now,
    lastUpstreamCheck === null ? null : Number(lastUpstreamCheck),
  );

  const day = dayKey(now);
  const statements = [];
  const changes = [];
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
      changes.push({
        component: id,
        from: current[id]?.state ?? null,
        state: decided.state,
        detail: observed[id].detail,
      });
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

  statements.push(...pruneStatements(env, now, prunedAt));

  await env.DB.batch(statements);

  // AFTER the write, never before. An alert about a state that failed to
  // persist would be an alert the page then contradicts, and the person
  // reading both is left trusting neither.
  const alertable = changes.filter((change) => isAlertable(change, COMPONENT_IDS));
  const alert = composeAlert(
    alertable,
    Object.fromEntries([
      ...COMPONENTS.map((c) => [c.id, c.name]),
      ...UPSTREAMS.map((u) => [u.id, u.name]),
    ]),
    now,
  );
  if (alert) await sendAlerts(env, alert);
}

/**
 * The daily counters for the window the page draws, as {component: {day: row}}.
 *
 * One query for every component rather than one per row: D1 charges per
 * statement and the whole table is at most nine components times ninety days.
 */
/**
 * Open incidents first, then recently resolved ones, each with its updates.
 *
 * Two queries rather than a join: D1 charges per statement, and stitching two
 * small result sets in JavaScript is clearer than reading a flattened join
 * back apart.
 */
async function loadIncidents(db, now) {
  const { results: incidents } = await db
    .prepare(
      `SELECT id, title, impact, started_at, resolved_at FROM incidents
       WHERE resolved_at IS NULL OR resolved_at > ?1
       ORDER BY started_at DESC LIMIT 20`,
    )
    .bind(now - HISTORY_DAYS * 86400)
    .all();
  if (!incidents?.length) return [];

  const { results: updates } = await db
    .prepare(
      `SELECT incident_id, at, status, body FROM incident_updates
       WHERE incident_id IN (${incidents.map((_, i) => `?${i + 1}`).join(", ")})
       ORDER BY at DESC`,
    )
    .bind(...incidents.map((incident) => incident.id))
    .all();

  const byIncident = {};
  for (const update of updates ?? []) {
    (byIncident[update.incident_id] ??= []).push(update);
  }
  return incidents.map((incident) => ({ ...incident, updates: byIncident[incident.id] ?? [] }));
}

async function loadHistory(db, now) {
  const days = recentDays(now, HISTORY_DAYS);
  const { results } = await db
    .prepare(
      "SELECT component, day, up, degraded, down, unknown FROM daily WHERE day >= ?1",
    )
    .bind(days[0])
    .all();
  const byComponent = {};
  for (const row of results ?? []) {
    (byComponent[row.component] ??= {})[row.day] = row;
  }
  return { days, byComponent };
}

/**
 * Drop anything past the window, once a day.
 *
 * Not every minute: it is a table scan to delete nothing, 1439 times out of
 * 1440. The marker is in `meta` for the same reason the upstream poll's is --
 * `observed_at` cannot answer a question about the checker rather than about a
 * service.
 */
function pruneStatements(env, now, prunedAt) {
  if (prunedAt !== null && now - Number(prunedAt) < 86400) return [];
  const oldest = recentDays(now, HISTORY_DAYS)[0];
  return [
    env.DB.prepare("DELETE FROM daily WHERE day < ?1").bind(oldest),
    // Transitions are the audit trail behind the bars, so they go when the
    // bars they explain go. Keeping them forever would grow without bound to
    // answer questions about days the page no longer draws.
    env.DB.prepare("DELETE FROM transitions WHERE at < ?1").bind(now - HISTORY_DAYS * 86400),
    env.DB.prepare(
      "INSERT INTO meta (key, value) VALUES ('pruned_at', ?1) " +
        "ON CONFLICT (key) DO UPDATE SET value = ?1",
    ).bind(String(now)),
  ];
}

async function readMeta(db, key) {
  const row = await db.prepare("SELECT value FROM meta WHERE key = ?1").bind(key).first();
  return row?.value ?? null;
}

/**
 * The one page a person may write from, and the gate in front of it.
 *
 * Everything here is deliberately small. This exists to be usable on a phone,
 * one-handed, by somebody who has just been woken up: three fields and a
 * button, no JavaScript, no client-side anything. The elaborate version of
 * this feature is the version that does not work at 3am.
 */
async function handleAdmin(request, env, now) {
  // NO ACCESS POLICY, NO ROUTE. Not a 403: a 404, because a form that posts
  // announcements to a page people trust should not advertise its own
  // existence to somebody who cannot open it.
  if (!env.ACCESS_AUD || !env.ACCESS_TEAM_DOMAIN) {
    return new Response("Not found", { status: 404 });
  }

  const who = await verifyAccessToken(request.headers.get("cf-access-jwt-assertion"), {
    teamDomain: env.ACCESS_TEAM_DOMAIN,
    audience: env.ACCESS_AUD,
    now,
  });
  if (!who) {
    console.log("admin rejected: no valid Access assertion");
    return new Response("Not found", { status: 404 });
  }

  if (request.method === "GET") {
    const incidents = await loadIncidents(env.DB, now);
    return new Response(renderAdmin({ incidents, who, now }), {
      headers: {
        "content-type": "text/html; charset=utf-8",
        // Never cached, anywhere. It is per-person and it is a control panel.
        "cache-control": "no-store, private",
        ...SECURITY_HEADERS,
        // The public page posts nowhere; this page posts to itself.
        "content-security-policy": SECURITY_HEADERS["content-security-policy"].replace(
          "form-action \'none\'",
          "form-action \'self\'",
        ),
      },
    });
  }

  const form = Object.fromEntries(await request.formData());
  const action = form.action;

  if (action === "open") {
    const incident = readIncidentForm(form);
    if (!incident) return new Response("Bad request", { status: 400 });
    const inserted = await env.DB.prepare(
      "INSERT INTO incidents (title, impact, started_at) VALUES (?1, ?2, ?3) RETURNING id",
    )
      .bind(incident.title, incident.impact, now)
      .first();
    await env.DB.prepare(
      "INSERT INTO incident_updates (incident_id, at, status, body) VALUES (?1, ?2, 'investigating', ?3)",
    )
      .bind(inserted.id, now, incident.body)
      .run();
    console.log(`incident ${inserted.id} opened by ${who}: ${incident.title}`);
  } else if (action === "update") {
    const update = readUpdateForm(form);
    if (!update) return new Response("Bad request", { status: 400 });
    const statements = [
      env.DB.prepare(
        "INSERT INTO incident_updates (incident_id, at, status, body) VALUES (?1, ?2, ?3, ?4)",
      ).bind(update.incidentId, now, update.status, update.body),
    ];
    // "resolved" is the status AND the act. Two controls for one thing is how
    // an incident ends up resolved with no closing word on it, or closed in
    // the database while the banner insists it is ongoing.
    if (update.status === "resolved") {
      statements.push(
        env.DB.prepare(
          "UPDATE incidents SET resolved_at = ?2 WHERE id = ?1 AND resolved_at IS NULL",
        ).bind(update.incidentId, now),
      );
    }
    await env.DB.batch(statements);
    console.log(`incident ${update.incidentId} updated by ${who}: ${update.status}`);
  } else {
    return new Response("Bad request", { status: 400 });
  }

  // POST then redirect, so a refresh cannot post the same update twice. On a
  // phone, on a bad connection, a double-tap is the normal case.
  return new Response(null, { status: 303, headers: { location: "/admin" } });
}

/**
 * Tell somebody. Two channels, and the second one is the point.
 *
 * A Discord webhook is the obvious place for this project's alerts and it is
 * also the one that goes silent in a Discord outage -- which is the case where
 * an alert matters most, because that is when everything else is on fire too.
 * So there is a second path that does not touch Discord at all: Cloudflare
 * Email Routing, sent from this Worker.
 *
 * NEITHER CHANNEL MAY BREAK THE CRON. An alert that throws would take down the
 * checking, which would leave the page stale, which the page would then
 * correctly report as its own failure -- an impressive way to turn "Discord is
 * slow" into "the status page is broken". Both are wrapped, both log, and the
 * run continues either way.
 */
async function sendAlerts(env, alert) {
  const summary = `${alert.title}\n${alert.lines.join("\n")}`;

  if (env.DISCORD_WEBHOOK_URL) {
    try {
      await fetch(env.DISCORD_WEBHOOK_URL, {
        method: "POST",
        signal: AbortSignal.timeout(8000),
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          username: "VRCVerify Status",
          embeds: [
            {
              title: alert.title,
              description: alert.lines.join("\n"),
              color: ALERT_COLOURS[alert.severity] ?? ALERT_COLOURS.unknown,
              url: "https://status.vrcverify.com/",
              timestamp: new Date(alert.at * 1000).toISOString(),
            },
          ],
        }),
      });
    } catch (error) {
      console.log(`discord alert failed: ${error?.name}: ${error?.message}`);
    }
  }

  if (env.ALERT_EMAIL && env.ALERT_FROM && env.ALERT_TO) {
    try {
      // Imported here rather than at the top of the file: the module only
      // exists when the binding does, and a Worker deployed without email
      // configured must still boot.
      const { EmailMessage } = await import("cloudflare:email");
      const message = [
        `From: VRCVerify Status <${env.ALERT_FROM}>`,
        `To: <${env.ALERT_TO}>`,
        `Subject: [VRCVerify] ${alert.title}`,
        "Content-Type: text/plain; charset=utf-8",
        "MIME-Version: 1.0",
        `Message-ID: <${crypto.randomUUID()}@status.vrcverify.com>`,
        `Date: ${new Date(alert.at * 1000).toUTCString()}`,
        "",
        summary,
        "",
        "https://status.vrcverify.com/",
      ].join("\r\n");
      await env.ALERT_EMAIL.send(new EmailMessage(env.ALERT_FROM, env.ALERT_TO, message));
    } catch (error) {
      console.log(`email alert failed: ${error?.name}: ${error?.message}`);
    }
  }

  // Always logged, whatever the channels did. `wrangler tail` is the third
  // channel, and the only one with no moving parts.
  console.log(`ALERT ${alert.severity}: ${summary}`);
}

/**
 * The homelab's report, and the only route on this Worker that writes.
 *
 * Authenticated by an HMAC over the timestamp AND the body, verified with
 * crypto.subtle.verify rather than by comparing strings -- a `===` on a
 * signature leaks its prefix through timing, and this is the one place an
 * attacker gets unlimited attempts.
 *
 * Every failure answers with the same shape and says as little as possible.
 * "Bad signature" and "stale timestamp" are the same 401 to a caller: telling
 * someone which half of the check they failed is telling them how to pass it.
 * The log says which, because the operator is not the attacker.
 */
async function handleReport(request, env, now) {
  const secret = env.REPORT_SECRET;
  if (!secret) {
    console.log("report rejected: REPORT_SECRET is not configured on this Worker");
    return new Response("Not configured", { status: 503 });
  }

  const raw = await request.text();
  if (raw.length > 8192) return new Response("Too large", { status: 413 });

  const parsed = parseSignatureHeader(request.headers.get("x-vrcverify-signature"));
  if (!parsed) {
    console.log("report rejected: unparseable signature header");
    return new Response("Unauthorized", { status: 401 });
  }
  if (!signatureIsTimely(parsed.timestamp, now)) {
    console.log(`report rejected: timestamp ${parsed.timestamp} is outside the window at ${now}`);
    return new Response("Unauthorized", { status: 401 });
  }

  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const signature = Uint8Array.from(
    parsed.signature.match(/../g).map((byte) => parseInt(byte, 16)),
  );
  const valid = await crypto.subtle.verify(
    "HMAC",
    key,
    signature,
    encoder.encode(`${parsed.timestamp}.${raw}`),
  );
  if (!valid) {
    console.log("report rejected: signature does not match");
    return new Response("Unauthorized", { status: 401 });
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return new Response("Bad request", { status: 400 });
  }

  const rows = readReport(body, Object.keys(PART_CAPABILITIES));
  if (!rows) return new Response("Bad request", { status: 400 });

  await env.DB.batch(
    rows.map((row) =>
      env.DB.prepare(
        `INSERT INTO heartbeat (part, at, up, detail) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT (part) DO UPDATE SET at = ?2, up = ?3, detail = ?4`,
      ).bind(row.part, now, row.up ? 1 : 0, row.detail),
    ),
  );

  // 204: there is nothing useful to say back, and a body here would only be
  // something for a reporter to start depending on.
  return new Response(null, { status: 204 });
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
    const [rows, history, incidents] = await Promise.all([
      loadState(env.DB),
      loadHistory(env.DB, now),
      loadIncidents(env.DB, now),
    ]);
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
    return {
      components,
      upstreams,
      history,
      incidents,
      checkedAt,
      freshness: dataFreshness(checkedAt, now),
    };
  } catch {
    // D1 unreachable. The page still renders and says so. Returning a 500 here
    // would be the status page taking itself down, which is a poor answer to
    // "is anything working".
    return {
      components: {},
      upstreams: {},
      history: { days: [], byComponent: {} },
      incidents: [],
      checkedAt: null,
      freshness: "unavailable",
    };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = Math.floor(Date.now() / 1000);

    if (request.method === "POST" && url.pathname === "/report") {
      return handleReport(request, env, now);
    }

    if (url.pathname === "/admin" && (request.method === "GET" || request.method === "POST")) {
      return handleAdmin(request, env, now);
    }

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
