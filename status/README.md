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
once and the page has real data in it.

Two things that will waste an hour otherwise:

  * Use `node:22-bookworm-slim`, not `alpine`. `workerd` is built against
    glibc and will not run on musl.
  * `apt-get install -y ca-certificates` in that container before starting.
    The slim image ships no CA bundle, so every HTTPS probe fails with
    `TLS peer's certificate is not trusted` -- and the page then shows
    everything as down, which looks exactly like a bug in this code. It is
    not; it is the container.
  * `wrangler dev` ignores a scheduled time supplied on the query string and
    always uses the clock, so the duplicate-delivery guard cannot be exercised
    through it. That one is unit tested instead.

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

## Alerting (phase 4)

The page closes the "nobody is watching" gap only if somebody is told. Two
channels, and the second exists because the first shares a failure with the
thing it reports on.

    npx wrangler secret put DISCORD_WEBHOOK_URL --config status/wrangler.toml

A webhook for a PRIVATE channel. The alert body carries the infrastructure
detail the public page refuses to print -- which part, and why -- because that
is the half that saves a login at 3am. Anyone who can read that channel can
read the shape of the estate.

For the second channel, see the commented `[[send_email]]` block in
`wrangler.toml`. Email Routing has to be enabled on the zone and the
destination address verified; Cloudflare will not deliver to an unverified
address and this Worker cannot tell that it did not.

What it will and will not send:

  * Our own five rows: every change, in both directions. An alert that only
    fires on the way down leaves you refreshing a page to find out when it is
    over.
  * Somebody else's status page: only into and out of `down`. Cloudflare sits
    at "minor" for hours over things that never touch us, and an alert that
    fires for those is an alert that gets muted, which costs the ones that
    matter.
  * `unknown` never alerts. It means the checker could not look.
  * One message per cron run, not one per row. A database outage moves four
    rows, and four alerts for one event is how somebody learns to ignore the
    fourth.

Both channels are wrapped: an alert that throws would stop the checking, which
would leave the page stale, which the page would then honestly report as its
own failure. That is an impressive way to turn "Discord is slow" into "the
status page is broken". `wrangler tail` is the third channel and the only one
with no moving parts.

## Posting an incident by hand (phase 5)

`/admin` is three fields and a button, no JavaScript, sized for a phone held
one-handed by somebody who has just been woken up. Posting is immediately
public, and the page says so above the form.

**With no Access policy configured, the route does not exist.** Not a 403: a
404, because a form that publishes announcements should not advertise itself
to somebody who cannot open it. That means the correct order is to set Access
up first and switch the route on second.

1. **Create the Access application.** In Zero Trust, a self-hosted application
   covering `status.vrcverify.com/admin` and nothing else. The public page must
   stay public: an Access policy over `/` would put a login in front of the one
   page that has to load during an outage.

2. **Add a policy.** One-time PIN to your email is the one to use here. No
   password to type on a phone at 3am, and no credential to lose.

3. **Tell the Worker what to expect.** From the application's overview, take
   the Application Audience (AUD) tag and your team domain:

       npx wrangler secret put ACCESS_AUD --config status/wrangler.toml
       npx wrangler secret put ACCESS_TEAM_DOMAIN --config status/wrangler.toml
       # e.g. yourteam.cloudflareaccess.com

**The Worker verifies the Access token itself**, against the team's published
keys, rather than trusting that the edge did. Access is the control; this is
the second one, and it exists because the first is a setting in a dashboard.
It can be scoped to the wrong path, switched off by somebody tidying up, or
silently fail to cover a route added later, and none of that is visible in a
diff. The failure would not be an error page. It would be a world-writable
form that posts announcements to a page people trust, and somebody would find
it.

To check it: open `/admin` in a browser signed out of Access and expect a 404,
then sign in and expect the form.

An open incident is shown in its own banner, as information.

**It never moves a colour.** Not the hero, not one pill, not one row. Not
better than what was measured, and not worse either: an operator opening a
"down" incident must not paint five working capabilities red for everyone
reading the page. State is measured; prose is written; the two are shown side
by side rather than merged.

The one thing an open incident does change is the *sentence* at the top,
which becomes "1 open incident" rather than "All systems operational" while
one is open. A green all-clear set in the largest text on the page, directly
above a red banner somebody wrote to say otherwise, is a page arguing with
itself -- and the all-clear wins, because it is bigger. A real measured
outage keeps its own headline, which already agrees with the banner and says
more than a count does.

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
