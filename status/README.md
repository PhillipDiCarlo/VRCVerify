# status.vrcverify.com

The public status page (issue #170). A Cloudflare Worker, separate from
everything it reports on, which is the whole point of it.

## Why it is not part of anything else

A status page shares a fate with whatever it is deployed next to, and a page
that goes down with the thing it reports on has answered the one question it
exists for by not loading. So:

| Failure | Does it take this page with it? |
| --- | --- |
| The homelab is off, or its containers are stopped | No |
| The VPS is off, its tunnel is stopped, `cloudflared` exits 0 at 02:42 | No |
| The apex site's Worker is broken or mid-deploy | No, it is a separate script |
| The database or the message queue is down | No, this page holds neither |
| Cloudflare itself is down | Yes. Accepted, and named on the page |

The last row is the honest limit. Everything this project runs on is behind
Cloudflare already, so a Cloudflare outage is not a failure mode this page
could have avoided by living somewhere else, and pretending otherwise would
mean a second vendor to keep in step for a case that already breaks the DNS.

## What it can see, and what it cannot

Only two things are reachable from the public internet: the dashboard's
`/healthz` and the apex site. Everything else runs behind the tailnet with
mutual TLS, deliberately (`SECURITY_AUDIT.md` section 2), so it cannot be
probed from out here and has to report outward instead. That is the homelab
reporter in phase 2: the services touch a heartbeat file, one small container
reads those files and the local infrastructure, and posts a single signed
summary. A report that stops arriving is itself the outage signal.

**The page names capabilities. It never names infrastructure.** No row for the
database, the queue, the internal API, the tunnel or either host, and none of
those words appear in the HTML or in `/api/status.json`. The detail exists --
it is what makes an alert useful at 3am -- and it goes to a private Discord
channel instead. `tests/test_status_page.py` and `test/render.test.js` both
fail if that leaks.

## Layout

    status/
      wrangler.toml     the Worker, its D1 binding, its cron, its hostname
      schema.sql        every table, applied once
      src/
        config.js       what is listed, and the private part -> capability map
        logic.js        every decision, with no I/O, so it can be tested
        render.js       the page, as a string
        index.js        the routes and the cron
      public/
        style.css       a copy of site/style.css plus the status colours
        theme.js        a copy of site/theme.js, byte for byte
        fonts/          a copy of the site's font, served from this origin
      test/             node --test, no dependencies

## Running it locally

There is no JavaScript toolchain on the development machine and there does not
need to be one. Both of these run in a container with the repository mounted:

    # The test suite
    docker run --rm -v "$PWD/status":/app -w /app node:22-bookworm-slim \
      node --test test/logic.test.js test/render.test.js

    # The real Worker, with a local D1, on http://localhost:8787
    docker run --rm -it -p 8787:8787 -v "$PWD/status":/app -w /app \
      node:22-bookworm-slim sh -c '
        npm i -g wrangler@4 &&
        wrangler d1 execute vrcverify-status --local --file schema.sql --yes &&
        wrangler dev --local --ip 0.0.0.0 --port 8787 --test-scheduled'

`--test-scheduled` exposes the cron at `/__scheduled?cron=*+*+*+*+*`; fetch it
once and the page has real data in it. Use `node:22-bookworm-slim` and not
`alpine`: `workerd` is built against glibc.

## First deploy

Nothing below has been run from this repository yet. Each step is separate on
purpose, because the failure messages when one is missing are unhelpful enough
to be worth meeting one at a time.

1. **Create the database.**

       npx wrangler d1 create vrcverify-status

   Put the id it prints into `status/wrangler.toml`, replacing `REPLACE_ME`.
   It is an identifier, not a credential, and belongs in the diff.

2. **Create the tables.**

       npx wrangler d1 execute vrcverify-status --remote --file status/schema.sql

3. **Deploy.**

       npx wrangler deploy --config status/wrangler.toml

   This also creates the DNS record for `status.vrcverify.com`, because the
   route is declared in the config with `custom_domain = true`. If the name
   already exists in the zone, wrangler will refuse rather than take it over;
   delete the old record first and read what it was before you do.

4. **Watch one cron fire.** The page says "No check has run yet" until the
   first one lands, which is within a minute.

       npx wrangler tail vrcverify-status

5. **Check the page tells the truth by making it lie.** Stop the cron (comment
   out `[triggers]` and redeploy, or simply wait five minutes with the cron
   disabled) and confirm the page turns grey and says the data is out of date,
   rather than continuing to show the last green it saw. A status page is
   worth exactly what its worst case is worth.

## Turning on the homelab's reporting (phase 2)

**Order matters, and getting it backwards is worse than not doing it.** A
reporter posting to a Worker that does not know the key yet is answered 401
every minute, and the page then shows the entire homelab as down while it is
running perfectly. Somebody will believe it.

1. **Generate a key.** `openssl rand -hex 32`. It is not a credential for
   anything else: the worst a stolen copy can do is lie to a status page.

2. **Give it to the Worker first.**

       npx wrangler secret put REPORT_SECRET --config status/wrangler.toml

3. **Then the homelab.** In the `.env` beside `docker-compose.deploy.yml`:

       HEARTBEAT_DIR=/heartbeats
       STATUS_REPORT_URL=https://status.vrcverify.com/report
       STATUS_REPORT_SECRET=<the same key>

4. **Deploy the homelab**, which needs a new image tag: the three services now
   write heartbeats, and `status-reporter` is a fourth container.

       ./tag_and_push_images.sh      # option 6, all of them
       VRCVERIFY_VERSION=x.y.z docker compose -f docker-compose.deploy.yml up -d

5. **Watch one report land.** `docker compose logs -f status-reporter` should
   say `Reported 6 parts` every minute. A 401 there means the keys differ; a
   connection error means this host cannot reach Cloudflare, which is worth
   knowing on its own.

6. **Prove the silence works.** `docker compose stop vrc-online-checker` and
   watch Verification go down within about three minutes: one report to notice
   the heartbeat is stale, and two runs of the page's cron to confirm it. Start
   it again and the row recovers on the next run, because recovery is published
   immediately and only failure needs confirming. This is the single most
   important behaviour in the whole system and it takes four minutes to check.

Phases 4 and 5 add the Discord webhook and a Cloudflare Access policy on
`/admin`. Each is documented here as it lands, rather than in advance.

## The rules this thing holds

1. **Never render green from missing data.** Absent, stale and unparseable are
   their own states, drawn grey and named. Stale data is drawn as unknown even
   when every stored row says `up`, because a checker that stopped an hour ago
   is not evidence, it is a photograph.
2. **No infrastructure name reaches the public surface.**
3. **The page renders when its own storage does not**, and says the fault is
   its own rather than implying the services are down.
4. **The observation interval is the resolution.** One minute, so nothing on
   the page is more precise than that, and no duration is written as though it
   were.
