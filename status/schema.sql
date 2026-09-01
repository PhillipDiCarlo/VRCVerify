-- The status page's D1 schema (issue #170).
--
-- Apply once, from the repository root:
--
--   npx wrangler d1 execute vrcverify-status --remote --file status/schema.sql
--
-- Every table is created in full here, including the ones phases 4 and 5 use,
-- because the alternative is running a migration during the first outage that
-- needs the feature. A status page is the worst place in this project to be
-- doing schema work under pressure.
--
-- WHAT IS PUBLIC AND WHAT IS NOT
--
-- `component_state.detail`, `transitions.detail` and the whole of `heartbeat`
-- are PRIVATE. They carry infrastructure names -- which queue, which host,
-- which database -- and the public page and /api/status.json must never
-- render them. That is decision 3 on the issue, and tests/test_status_page.py
-- pins it. The detail exists because it is what makes an alert at 3am useful;
-- the alert goes to a private Discord channel, not to the page.

-- Current published state, one row per component. The page reads this and
-- nothing else for its verdict.
--
-- `pending` and `pending_n` hold an observation that has NOT been published
-- yet. A single failed probe is not an outage: it is a lost packet, a cold
-- start, or Cloudflare having a moment. Requiring the same observation twice
-- in a row before it becomes the published state is what keeps a flap off the
-- page and out of the alerts. Recovery is published immediately, which is
-- deliberately asymmetric -- being slow to say "down" costs nothing, being
-- slow to say "back" costs someone reading a page that lies about a service
-- they can see working.
CREATE TABLE IF NOT EXISTS component_state (
  component   TEXT PRIMARY KEY,
  state       TEXT NOT NULL CHECK (state IN ('up', 'degraded', 'down', 'unknown')),
  since       INTEGER NOT NULL,
  observed_at INTEGER NOT NULL,
  pending     TEXT,
  pending_n   INTEGER NOT NULL DEFAULT 0,
  detail      TEXT
);

-- One row per component per UTC day, counting observations by state.
--
-- Counters rather than raw samples: a sample per component per minute is about
-- 1.2 million rows over 90 days, to answer a question that is four integers
-- wide. The cost of the choice is that the exact minute an outage started is
-- not recoverable from here -- `transitions` is where that lives.
--
-- `unknown` is counted separately and is excluded from the uptime percentage
-- rather than counted against it. A minute we could not observe is not a
-- minute the service was down, and reporting it as one would make the page
-- lie in the direction that looks worst.
CREATE TABLE IF NOT EXISTS daily (
  component TEXT NOT NULL,
  day       TEXT NOT NULL,
  up        INTEGER NOT NULL DEFAULT 0,
  degraded  INTEGER NOT NULL DEFAULT 0,
  down      INTEGER NOT NULL DEFAULT 0,
  unknown   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (component, day)
);

-- Every published state change, which is both the audit trail and what the
-- alerting reads to decide it has something new to say.
CREATE TABLE IF NOT EXISTS transitions (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  component TEXT NOT NULL,
  state     TEXT NOT NULL,
  at        INTEGER NOT NULL,
  detail    TEXT
);
CREATE INDEX IF NOT EXISTS transitions_at ON transitions (at DESC);

-- Small key/value facts about the checker itself rather than about a service.
--
-- It exists because `component_state.observed_at` cannot answer "when did we
-- last poll Discord's status page": that column is written every minute for
-- every row, including the upstream rows whose stored answer is simply being
-- carried forward. Reading the poll time from it would say "we polled a second
-- ago" forever, and the five minute upstream interval would silently become
-- "once, at deploy".
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- PRIVATE. The homelab reporter's last word on each internal part (phase 2).
-- `part` is an infrastructure name and never leaves this table: the mapping
-- from parts to the five public capabilities happens in src/logic.js, where
-- it is testable, and only the capability is stored in component_state.
--
-- A row that stops being updated is the down signal. That is the point of the
-- design: the reporter cannot tell us the homelab is on fire, so silence has
-- to mean something, and here it means "down" rather than "fine".
CREATE TABLE IF NOT EXISTS heartbeat (
  part   TEXT PRIMARY KEY,
  at     INTEGER NOT NULL,
  up     INTEGER NOT NULL,
  detail TEXT
);

-- Hand written incidents (phase 5), posted through /admin behind Cloudflare
-- Access. `body` is PUBLIC prose, unlike every other detail column in here.
CREATE TABLE IF NOT EXISTS incidents (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  impact      TEXT NOT NULL CHECK (impact IN ('maintenance', 'degraded', 'down')),
  started_at  INTEGER NOT NULL,
  resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS incidents_started ON incidents (started_at DESC);

CREATE TABLE IF NOT EXISTS incident_updates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
  at          INTEGER NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('investigating', 'identified', 'monitoring', 'resolved')),
  body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incident_updates_incident ON incident_updates (incident_id, at DESC);
