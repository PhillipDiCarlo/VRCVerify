# VRChat Verify Bot
[Get the Full Bot HERE](https://discord.com/discovery/applications/1335738139825799188)

VRChat Verify Bot is a Discord bot that automates the verification of VRChat users’ age (specifically confirming their "18+" status) by cross-checking their VRChat profiles. The project is split into two main components:

1. **Discord Bot (bot.py):**  
   - Implements slash commands for users and administrators.
   - Collects VRChat usernames via modals.
   - Generates and verifies a unique code which users must add to their VRChat bio.
   - Communicates with a RabbitMQ messaging system to send verification requests and receive results.
   - Uses SQLAlchemy to store server, user, and pending verification data in a database.
   - Assigns roles based on the verification outcome.

2. **VRChat Online Checker (vrc_online_checker.py):**  
   - Listens for verification requests on a RabbitMQ queue.
   - Logs into VRChat using the [vrchatapi](https://github.com/vrchatapi/vrchatapi) library. Handles two-factor authentication automatically by fetching a 2FA code from a Gmail account.
  - Retries VRChat login in the background on a fixed interval when the session is unavailable, instead of relogging on every verification request.
  - Applies explicit VRChat API connect/read timeouts so stalled upstream requests fail visibly instead of hanging forever.
   - Checks the target VRChat user’s profile for age verification status and whether the provided code is present in their bio.
  - Sends back the verification result via a RabbitMQ result queue, including structured outage/auth metadata when VRChat is unavailable.
  - Its VRChat login, 2FA handling, cookie persistence and outage classification live in `src/vrc_session.py`, shared so a second VRChat account can hold its own independent session.

3. **VRChat Group Inviter (vrc_group_inviter.py):**
   - Runs a **second, separate VRChat account** from the checker, so moderation action against one cannot take the other down.
   - Consumes its own RabbitMQ queue (`RABBITMQ_GROUP_INVITE_QUEUE`), never the verification queue: two consumers on one queue split messages round-robin, so the checker would swallow its jobs.
   - Verifies a server's group setup on request: joins the group it was told about, then reports whether the account is a member and holds `group-invites-manage` (its own permission, which being a group admin does **not** include) and the optional `group-members-viewall`.
   - **Never joins a group it was not explicitly told to join.** There is no loop that polls for or accepts pending invites; anyone can invite the account to a group, and that alone must never put it in one.
   - Sends one member's invite on request, after checking where they already stand: an existing member, a waiting invite, or a pending join request all mean "nothing to send", each with its own answer. `confirm_override_block` is passed as **False** explicitly, because `vrchatapi` defaults it to `True` — omitting it would opt in to pushing invites past people who have blocked the group.
   - Spaces consecutive invites with jitter (`INVITE_MIN_SPACING_SECONDS`). One account serves every guild, so throughput is a shared budget and a server's verification drive must not be able to spend it in a burst.
   - Exits immediately when `INVITE_VRCHAT_USERNAME`/`INVITE_VRCHAT_PASSWORD` are unset, so the feature is simply not provisioned rather than broken.

   **How a member gets invited.** Nothing reaches VRChat until they ask. A
   verified member in a server with a ready group gets a DM offering a button;
   pressing it is what creates the request. A member who ignores the DM costs
   no API call at all, and one who has been invited, is already in the group,
   or has group invites switched off is never offered again — their answer is
   recorded and honoured. That is a compliance position as much as a privacy
   one: VRChat's Creator Guidelines treat unsolicited automation as abuse, and
   an invite nobody asked for is exactly that.

---

## Recent updates

- Localization support with per-server locale setting (supports multiple language codes; see Localization section).
- Configuring moved to the web dashboard. The admin commands that used to edit
  settings now show them read-only and link there; `/vrcverify_setup` and
  `/vrcverify_status` are unchanged, because one is how a server is configured
  before anyone has heard of the website and the other is what you reach for
  when something is broken — which is exactly when the website may be the
  broken thing.
  - Optional removal of an "unverified" role once a user becomes verified.
  - Instructions panel colour and server-icon thumbnail (premium).
- Instructions posting command (/vrcverify_instructions) that publishes a localized, interactive instruction embed with buttons.
- Robust request/result flow via RabbitMQ including a dedicated result consumer in the bot.
- Improved RabbitMQ reliability: both services auto-reconnect after broker restarts/idle disconnects; publishes retry and use persistent delivery.
- Ephemeral "pending verification" records with background cleanup of expired requests.
- REST and VRChat API TTL caches to reduce rate-limit pressure and speed up repeated checks.
- Checker-side outage-aware responses and background relogin scheduling for expired or failed VRChat sessions.
- VRChat API connect/read timeout controls to prevent the checker from hanging during login or lookups.

See the sections below for details and configuration.

---

## Architecture Overview

- **Discord Integration:**  
  Uses the [discord.py](https://github.com/Rapptz/discord.py) library to create slash commands (e.g., `/vrcverify`, `/vrcverify_setup`, `/vrcverify_support`) that let users initiate the verification process and administrators configure server settings.

- **Database:**  
  Utilizes SQLAlchemy to manage these models:
  - **Server:** Holds configuration for each Discord server (guild), including role assignments. (Its `subscription_status` / `email` / `last_renewal_date` columns are dormant leftovers from a Stripe integration removed years ago, and are still not used by anything. The current Stripe support deliberately did not resurrect them — see **StripeSubscription** below.)
  - **User:** Stores individual user verification statuses and VRChat IDs.
  - **PendingVerification:** Temporarily holds verification requests until they are processed.
  - **PremiumCutoverNotice:** Which guilds have already had the one-time premium announcement DM.
  - **PremiumGrandfatherLine:** Single row holding `MAX(servers.id)` as of the moment the premium tier was switched on. Servers at or below it keep the grandfathered features free, permanently. Captured once, never moved.
  - **InstructionPanelBranding:** A premium server's chosen embed colour and whether to show its server icon on the instructions panel. Both default to off, so the row existing does not by itself restyle anything.
  - **VerificationLogChannel:** Where a guild posts its verification activity log.
  - **StripeSubscription:** A guild's card subscription, mirrored from Stripe so the premium gate stays a database read rather than an API call. Stripe remains the source of truth; the bot holds no Stripe credential and never talks to Stripe — the dashboard verifies each webhook signature and forwards a normalised summary over the existing mTLS channel. Premium is granted if **either** this or a Discord entitlement is live.
  - **StripeEvent:** Every webhook event id already acted on. Stripe retries a delivery for up to three days, so duplicates are expected traffic; this is what makes applying one idempotent.
  - **PremiumEntitlementSeen:** Which guilds have ever held a Discord entitlement for the premium SKU, including ones that have since ended. Discord's gateway only reports entitlements that change while the bot is connected, so this is filled by a sweep on every boot that walks ended entitlements too. It exists for one question — whether a server has ever paid — and the free trial is the only thing that asks it.

- **Messaging with RabbitMQ:**  
  Uses the pika library to handle two queues:
  - A request queue (for sending verification requests from the bot to the checker).
  - A result queue (for receiving verification outcomes back in the bot).
  
  Reliability note: both components run a long-lived consumer and will automatically reconnect if the RabbitMQ container restarts or the connection becomes stale. Publishing is retried and messages are marked persistent.

- **VRChat API Integration:**  
  Uses the `vrchatapi` library to interact with VRChat’s API. The online checker handles VRChat login, including two-factor authentication by checking the inbox of a Gmail account via IMAP. When login fails or a session expires, the checker continues serving requests with structured temporary-unavailable or outage metadata and retries login on a background interval.

- **Caching and Rate Control:**  
  Both the bot and the checker use small TTL caches to avoid duplicate external requests and configurable concurrency limits on REST calls.

- **Operational Notes:**
  The checker performs one login attempt at startup, then retries in the background according to `VRCHAT_RELOGIN_INTERVAL_SECONDS` when logged out. VRChat API requests use explicit connect/read timeouts configured through environment variables.

---

## Slash commands

- `/vrcverify` – User entry point. Guides through entering a VRChat user ID/profile URL and verifying by adding a one-time code to the VRChat bio. Re-check flow is supported without a new code when applicable.
- `/vrcverify_setup` – Admin-only. Sets the role assigned to verified users and an optional role to remove once verification succeeds.
- `/vrcverify_instructions` – Admin-only. Posts a localized instruction embed with interactive buttons to begin verification and (optionally) update nickname.
- `/vrcverify_settings`, `/vrcverify_setrequestmessage`, `/vrcverify_logchannel`
  – Admin-only. These **no longer edit anything**. Each shows the server's
  current settings read-only and links to the dashboard, which is where
  configuring happens now. They kept their names rather than being deleted: a
  slash command that vanishes leaves an admin typing something Discord no
  longer offers and getting nothing back, with no clue where it went.
- `/vrcverify_support` – Anyone. Sends help/support information.
- `/vrcverify_subscription` – Admin-only. Shows this server's premium status,
  and answers for all four of them: paid by Discord, paid by card, paid by
  **both** (which warns, since nothing cancels the other automatically), or not
  subscribed — in which case it offers Discord's purchase button alongside a
  link to the dashboard's Subscriptions page, where the 6- and 12-month plans
  live. A card subscriber gets a Manage subscription link and is never told to
  cancel in Discord's User Settings, where their subscription does not exist.

---

## Premium tier

18+ verification itself is free and always will be. A per-server subscription
unlocks the automation around it, and there are two ways to buy the same thing
— Discord App Subscriptions (guild-scoped SKU, monthly) or a card on the web
dashboard. See [Two ways to pay](#two-ways-to-pay).

| Feature | Free | Grandfathered\* | Premium |
| --- | :---: | :---: | :---: |
| 18+ verification | ✅ | ✅ | ✅ |
| **Auto-verify-on-join** | ✅ | ✅ | ✅ |
| Manual **Update Nickname** button | ✅ | ✅ | ✅ |
| Instructions language | ✅ | ✅ | ✅ |
| Unverified-role auto-removal | — | ✅ | ✅ |
| Auto-nickname sync | — | ✅ | ✅ |
| Custom post-verification DM | — | ✅ | ✅ |
| Reduced verification cooldown | — | — | ✅ |
| Verification activity log channel | — | — | ✅ |
| Priority placement in the verification queue | — | — | ✅ |
| Branded instructions panel (colour + icon) | — | — | ✅ |

Auto-verify-on-join is free for everyone and is deliberately not gated at all —
`on_member_join` never so much as reads an entitlement. Users read "the bot
recognises me and gives me the role" as simply how a verification bot works, so
charging for it reads as the bot being broken rather than as an upsell. It's
also the only gated feature a *member* could perceive, and members move between
servers. `tests/test_premium.py::TestAutoVerifyOnJoinIsFree` pins this so it
can't drift back behind the paywall.

\* Servers installed before the tier went live. The line is **captured, not
configured**: on the first startup that finds `PREMIUM_SKU_ID` set, the bot
records `MAX(servers.id)` into `premium_grandfather_line` and never moves it.

That ordering is what makes switching the tier on safe — every server that
already exists is grandfathered by construction, so no server can lose
automation it was already using. Only servers added *after* launch are ever
asked to pay for those three features. It also means the cutover DM is purely
informational and can go out whenever, since it isn't warning anyone about a
loss.

The line lives in the database rather than an env var so a restore carries it
alongside the servers it describes. Recomputing it after a restore would
re-draw it wherever the restore landed, retroactively grandfathering everyone
who signed up since. `PREMIUM_GRANDFATHER_MAX_ID` overrides the captured value
and exists only as an escape hatch.

Grandfathering costs less revenue than it looks like: the activity log, queue
priority and everything built after them are premium for *every* server
regardless, so the line only governs three features.

**The tier is off until `PREMIUM_SKU_ID` is set.** With it unset every gate
answers "allowed", so this code runs identically to the free bot — the SKU can
be created and the tier switched on without a redeploy.

Entitlements are read from `interaction.entitlements` where an interaction is
available (free, authoritative) and from the REST entitlements endpoint
otherwise, cached with a TTL and invalidated by the `ENTITLEMENT_*` gateway
events. A lookup failure **fails open** to the last known value, so a Discord
outage can't silently disable a paying server's automation.

The one-time cutover announcement to existing servers is manually triggered:
`touch` the file at `PREMIUM_CUTOVER_TRIGGER_PATH` and it trickles out under a
per-sweep cap, then stops on its own.

#### Launching

1. Set `PREMIUM_SKU_ID` and restart. On that startup the grandfather line is
   captured at `MAX(servers.id)`, which the bot logs. Every server that exists
   at this moment keeps the three grandfathered features permanently.
2. `touch` the trigger whenever you like afterwards. The campaign DMs those
   servers to say the tier launched and nothing changed for them, trickling at
   roughly 240 servers/hour (20 per sweep, 300s apart), then stops on its own.

There is deliberately no ordering hazard here. An earlier design had the line
hand-set in the environment, which meant forgetting to update it before
flipping the switch silently stripped automation from servers that had it
([#59](https://github.com/PhillipDiCarlo/VRCVerify/issues/59)). Capturing the
line at switch-on removes that failure entirely rather than detecting it.

The bot logs a one-time reminder if the tier is live and the announcement is
still outstanding. That is a courtesy nudge, not a safety check — those servers
keep their features either way.

### Two ways to pay

The same subscription can be bought through **Discord** or with a **card on the
dashboard**, and the premium gate is an OR over the two: a guild is premium if
a Discord entitlement is live **or** a mirrored Stripe subscription is in a
paying status. Nothing else in the bot knows which one paid.

Card plans exist because Discord only offers monthly. The dashboard's
Subscriptions page reads its plans from one Stripe **product** at render time,
so creating a price publishes a plan and archiving one retires it, with no
deploy either way. Each price's own metadata carries its label, order, saving
and trial length.

The bot holds **no Stripe credential and never talks to Stripe.** The dashboard
verifies each webhook signature and forwards a normalised summary over the
existing mTLS channel; the bot writes `StripeSubscription` and answers from the
database. That keeps the payment integration on the public box and the money
questions answerable without a network call.

**The two halves fail in opposite directions, deliberately.** A Discord
entitlement lookup fails **open** to the last known value, because an API
outage must not switch off a paying customer. Trial eligibility fails
**closed**, because the cost of being wrong there is a free month handed out
repeatedly for as long as the database is unhappy — and a server that really
qualifies can be offered one a minute later.

#### The free trial

14 days, **card only**, **monthly only**, and only for a server that has
**never held premium by either route**. Length and which plans offer it are set
in Stripe (`trial_days` on a price's metadata); *who may be offered one* is the
bot's answer, travelling in the settings payload beside the SKU id, and the
checkout route re-checks it server-side before passing a trial to Stripe — a
card rendered without one is not a gate, because a POST is not a click.

Stripe cannot enforce this. Each checkout mints a fresh customer, so it holds
no memory linking a returning guild to the trial it already used.
`PremiumEntitlementSeen` plus the surviving `StripeSubscription` rows are the
whole of that memory.

Grandfathered servers **are** eligible: they have never paid, and that is the
only question asked. `is_grandfathered` compares a row id against a captured
line and knows nothing about money; reading it into a payment decision would
couple the two in the direction the grandfathering rule exists to prevent.

#### Kill switches

`STRIPE_ENABLED` exists separately on the bot and on the dashboard. **Turn the
bot's on first.** With the dashboard's on and the bot's off, a customer
completes checkout, is charged, and the forwarded write is answered 404 until
Stripe gives up three days later.

### Queue priority

Premium servers' verification requests are published at a higher RabbitMQ priority than
free ones. Every verification goes through a single shared VRChat account and a checker
consuming with `prefetch_count=1`, so this decides who is served first **when a backlog
exists** — RabbitMQ only reorders messages already waiting, so nothing changes while the
queue is empty.

`QUEUE_MAX_PRIORITY` is a hardcoded constant in **both** `src/bot.py` and
`src/vrc_online_checker.py`, deliberately not an environment variable. Both services
declare the same queue and the arguments must match exactly, or every declare fails with
406 `PRECONDITION_FAILED` and takes down publishing and consuming at once. Changing the
value is a migration, not a config change. `tests/test_priority_queue.py` pins that the
two services agree and that each actually passes the arguments.

> #### ⚠️ Upgrading to a build with queue priority
>
> The pre-existing request queue has no `x-max-priority` argument, and RabbitMQ will not
> let it be re-declared with one. **It must be deleted and recreated.**
>
> The queue is the one named by **`RABBITMQ_QUEUE_NAME`** in your `.env` — check it
> rather than assuming, since the deployed name is not the placeholder in
> `.env.example`. Only the *request* queue changes; `RABBITMQ_RESULT_QUEUE` is untouched.
>
> This sequence loses nothing:
>
> 1. Stop **the bot only**. No new requests are published.
> 2. In the RabbitMQ UI, watch that queue until **Ready = 0 and Unacked = 0** — the
>    checker finishes the backlog normally.
> 3. Stop the checker.
> 4. Delete the (now empty) queue.
> 5. Deploy both new images and start them.
>
> Deleting the queue while it still holds messages discards them, and each one is
> somebody who pressed Verify and will never hear back — hence draining first.
>
> Do not delete it while the old containers are still running: the bot re-declares the
> queue on every publish and the checker on every reconnect, so it would reappear within
> seconds, still without priority. The delete has to land in the gap between stopping the
> old containers and starting the new ones.
>
> If step 4 is skipped, both services log an explicit error naming the queue and the fix.
> The bot stops publishing (rather than retrying something that can never succeed) and
> the checker backs off; verification is stopped until the queue is deleted.

### Branded instructions panel

The dashboard's Instructions panel group lets a premium server set its own embed colour
and show its server icon as the panel thumbnail. Colour is entered as a hex
value (`#5865F2`, or `#58F` shorthand); Discord has no colour-picker component
of any kind, so a text field is the only way to express an exact brand colour.
`#000000` is nudged to `#010101`, because Discord reads a colour of 0 as "no
colour" and would render the default grey.

**The instruction copy itself is not customisable, deliberately.** It is the
part that actually gets people through verification correctly, and letting
servers rewrite it means support requests about instructions nobody here wrote.

Both settings default to off, so subscribing does not restyle a panel the admin
never asked to restyle. The thumbnail is the guild's own icon rather than an
arbitrary URL — nothing to validate, and no way for a server to put unrelated
imagery inside an age-verification panel.

Styling is resolved at edit time, never stored with the panel, which is what
makes a lapse revert: `resolve_panel_style` returns the defaults once the
entitlement is gone. Since the panel is a persisted Discord message, something
still has to re-edit it, so the bot does that when an admin saves and whenever a
guild's entitlements change. Guilds with no branding row skip the entitlement
read entirely, so the fleet refresh doesn't become one lookup per panel.

A server that changes its Discord icon keeps the old thumbnail until its panel
is next refreshed — the URL is baked into the message.

### Verification activity log

A log channel set on the dashboard posts each verification outcome to a channel —
granted, refused, and (most usefully) *granted but the role could not be
assigned*, which is otherwise a silent failure only the member ever hears about.
Run it with no channel to turn logging off.

Entries contain **the Discord member, the outcome, and the time. Nothing else.**
No VRChat display name, no `usr_` ID. The bot knows the link between someone's
Discord and VRChat identities because it has to; writing that into a server
channel publishes it to everyone who can read there, in a place whose
permissions we neither control nor can audit, and the member never agreed to it.
`tests/test_activity_log.py` asserts this across every locale so it can't drift.

Entries are buffered and posted in batches rather than one message each, and
every send suppresses mentions — otherwise each entry would ping the member it
reports on.

Announcement channels are rejected. Other servers can follow them, which would
republish your members' 18+ status outside your server.

Two known limits, both deliberate:

- The buffer is **in memory**, so up to `VERIFICATION_LOG_FLUSH_INTERVAL`
  seconds of entries are lost when the bot restarts or redeploys. Persisting
  them would mean a write per verification to guarantee a convenience feature.
- When a batch can't be posted (permissions revoked, Discord erroring) those
  entries are gone — but the count is carried forward, and the next batch that
  does land says how many never made it. A gap in the log always has a line
  accounting for it.

---

## Internal API (dashboard groundwork)

Groundwork for the web dashboard in issue #65. **Off by default and off in
production** — with `BOT_API_ENABLED` unset, `src/bot_api.py` never opens a
socket and the bot behaves exactly as it did before this existed.

When it *is* enabled, it runs inside the bot process (sharing the gateway
cache, so it can answer questions about roles, channels and permissions without
a second copy of anything), answers the dashboard's questions about a guild's
settings, and accepts the two changes it is allowed to make to them.
Three independent checks guard every request:

1. **mTLS** against a private CA — see `scripts/gen_bot_api_certs.sh`. The
   Tailscale tunnel it runs inside is a segmentation control, not an
   authentication one, so the transport is authenticated in its own right.
2. **A scoped token**, valid for one Discord user, one guild, one operation and
   about thirty seconds, and single-use. A captured request cannot be replayed
   against a different guild or a different endpoint.
3. **The bot's own Administrator check**, re-run per request against a
   short-lived cache (`BOT_API_ADMIN_TTL`, 15s) rather than held against a
   session — so pulling someone's admin role cuts off their dashboard access
   within seconds, which is what you need when the role is being pulled
   *because* an account is compromised. Manage Server is not enough, matching
   every slash command.

The server picker only answers for guilds the caller actually administers. It
will not confirm whether the bot is in a guild you have no standing in, so a
stolen session cannot be used to enumerate which servers run this bot.

Deployment rules, all enforced or explained in `.env.example`:

- `BOT_API_BIND` must be the tailnet address. The bot **refuses to start** on
  `0.0.0.0`, `::` or a blank value rather than listening on every interface.
- Never publish the port in Docker. Enabling the API means host networking —
  the compose file explains why the alternative is worse.
- A misconfiguration stops the API, never the bot. Verification keeps working
  through an expired dashboard certificate; the log says so at `ERROR`.

The API's authority is **a set of named capabilities, not a database handle**:
`BotAPIDeps` holds twelve callables, ten of which only read or check, and
exactly two that change anything — `write_settings` and `post_panel`. There is
no generic column setter, so widening what the website
can do to a server means adding a named capability here — a reviewable diff,
not a query string nobody noticed. `tests/test_bot_api.py` pins the exact
membership of that set and that no third mutating route exists.

(Through step 4 this was read-only by construction, with no writers at all.
Step 5 added the two above deliberately; the control was never "no writers
ever", it was "a writer cannot appear by accident".)

### The dashboard itself

`src/dashboard/` is a small Flask app that signs an admin in with Discord OAuth,
lists the servers they administer, and opens one server in a sidebar with three
sections. It holds no database credential and no bot token: everything it knows
it asks the bot for over mTLS, and the bot decides what it is allowed to know.

| Section | URL | What it is |
| --- | --- | --- |
| Overview | `/guild/<id>` | Where picking a server lands. Member count, verifications today / 7d / 30d, and the one thing worth fixing next. |
| Settings | `/guild/<id>/settings` | Every setting the slash commands can change. |
| Subscriptions | `/guild/<id>/subscription` | This server's premium status, and where a card subscription is bought. Plans come from Stripe at render time; checkout and the billing portal both redirect to Stripe's own domain. |

All three authorise identically — a session to prove who is asking, then the
bot to decide what they may see — and, just as importantly, **all three fail
identically**, through one `_guild_page_unavailable`. A 403 and a 404 from the
bot render as the same page with the same status, because rendered differently
they would let any signed-in user walk guild ids and enumerate the servers
running 18+ gating. An oracle only has to exist on one of the three routes to be
worth using — which is why Subscriptions called the bot before rendering even
back when it was a placeholder with nothing on it.

Subscriptions has one failure rule the other two don't need: **a read that
fails must never render as "not subscribed".** It apologises instead. "Not
subscribed" next to a Buy button is how you sell somebody a second
subscription, so the page carries a distinct "we could not read this" state
rather than collapsing it into the empty one.

The sidebar collapses to an icon rail from the hamburger in the top left, and
the choice is remembered in a cookie by `POST /prefs/nav` — the one write route
here that never reaches the bot. The toggle is a form rather than a script,
which is why the collapsed state and the cookie can never disagree.

### How it looks, and what that costs

The dashboard borrows **Discord's own surface layering** — a dark shell, a
lighter sidebar on it, lighter cards again on top — so an admin arriving from a
slash command feels like they changed rooms rather than applications. What is
deliberately *not* borrowed is blurple as decoration: here the brand colour
means exactly two things, "this is the page you are on" and "this button does
the thing", so it never appears on a border or a heading. Everything else is
carried by the surface ramp and type weight.

Four constraints shape the implementation, and three of them are CSP:

- **`style-src 'self'`, no `'unsafe-inline'`** — no `style=""` attributes
  anywhere. Any value that varies per element has to be a class or a
  presentation attribute, which is why the colour swatches are SVG `fill`. If a
  genuinely dynamic value is ever needed, the answer is a per-response nonce on
  one `<style>` block that sets custom properties — not `'unsafe-inline'`.
- **`font-src 'self'`** — Inter is vendored into `static/fonts` (48KB latin
  subset, variable weight, OFL, licence alongside it). No font CDN: a third
  party would otherwise see who opens the dashboard and could break it by going
  down. The subset has no U+2713 or U+2190, which is why the ticks and the back
  arrow are inline SVG — a glyph the font lacks falls back to another family at
  a different weight, and that reads as a rendering fault.
- **`img-src 'self' https://cdn.discordapp.com`** — icons are inline SVG, so
  they need no origin at all and inherit `currentColor` for free.
- **No `connect-src`** — so `fetch` and `XHR` are blocked by `default-src
  'none'`. Deliberate: adding one is a decision to take on purpose.

**Static assets are the one place `no-store` is relaxed.** Pages carry guild
names, plan state and a CSRF token, so they must never sit in a shared cache.
Static files are the same bytes for a signed-out stranger, and `asset()` stamps
a content digest into each URL — so they are cached for a year, and a deploy
changes the URL rather than leaving anyone on a stale stylesheet.

**There is exactly one script**, `static/app.js`, and it is a warning rather
than a mechanism: the settings page has five independent forms, and editing one
group then saving another silently discards the first group's edits. It is
external (never inline), touches no network, writes no markup, and holds no
authority — with JavaScript off, every page renders, navigates and saves
exactly as before. `tests/test_dashboard.py` pins all of that.

**Motion is narrow by intent**: 120ms colour fades on hover and focus, and
nothing else. A cross-document view transition between pages was tried and
removed — it cross-faded the whole page on every navigation, which put a delay
between clicking a section and being able to read it, and moving between
Overview and Settings is something an admin does repeatedly. Nothing loops,
spins or slides, and the fades are off under `prefers-reduced-motion`.

**Mobile and old hardware** are first-class: 44px touch targets on coarse
pointers, 16px form controls so iOS does not zoom on focus, guild icons
requested at `?size=64`, and no `backdrop-filter` or large blurred shadows —
the expensive things to paint. Modern CSS (`:has()`, container queries,
`color-mix()`) is used only as progressive enhancement, because the practical
floor is Safari 15.0–15.3 on a phone that stopped getting updates.

**The Overview's three ways of not showing a number are three different
statements**, and `src/dashboard/overview_view.py` exists to keep them apart:
`0` means the window is covered and nothing happened in it — a panel is up and
nobody is using it, which is usually the thing the admin came to find out; a
blank means the window reaches back before `verification_daily` started
collecting, so no figure would be true; "Couldn't check" means the bot did not
answer. Showing a blank as `0` invents a quiet week, and showing a `0` as blank
hides a broken server.

Counts only. The Overview reports no per-member information because none is
stored — see the `verification_daily` note under Database Setup. It also does
not report how many members hold the verified role: the bot runs with
`MemberCacheFlags.none()` and no startup chunking, so answering that honestly
would mean chunking the guild, and the cost scales with exactly the servers
where the number would be most interesting. A tile that is sometimes wrong is
worse than no tile.

It can now save **every setting the slash commands can**, one form per group.
That boundary is still enforced in the bot by `DASHBOARD_WRITABLE_FIELDS`,
not by which controls the website chose to draw, and the payload tells the
dashboard which fields are open so the two cannot drift. The website renders a
control only where the bot has said it would accept the value — and, for a
picker, only when it actually has something to pick from, because an empty
`<select>` invites a save that would clear the verified role.

Each value is checked the way its own slash command checks it, and the
differences between them are deliberate:

- **Roles** must name a real role in *that* guild — the guarantee Discord's
  picker gives `/vrcverify_setup` for free, which a form submitting a raw id
  has to provide for itself. A role the bot cannot *assign* is still accepted,
  because `/vrcverify_setup` accepts it too; refusing would block an admin who
  means to set the role first and fix the hierarchy after. The page warns.
- **The log channel** must not be an announcement channel, because
  the bot refuses those outright — other servers can follow
  one, which would republish an age disclosure about a named member. Those
  channels are left out of the picker entirely: here, omitting is matching the
  bot rather than being stricter than it.
- **The custom DM** goes through `sanitize_custom_message()` — the same
  function the slash command uses, not a second implementation. It strips
  zero-width characters, defuses `@everyone`, and allows links only to
  discord.com and vrchat.com. The *sanitised* text is what gets stored.

A picker is only rendered when it has something to pick from. When the roles or
channels read fails, the field falls back to read-only — an empty `<select>`
invites a save that would clear the verified role and stop verification for the
whole server.

It can also **post the instructions panel** into a channel — the one thing the
dashboard makes the bot *do* in a server rather than store. That makes doing it
twice by accident the main hazard, so the bot decides what a request means:

- the panel is already in the chosen channel → **refresh** it, the same
  one-call edit the fleet sweep uses. A double-click costs an edit, not a
  second panel with live buttons that nothing tracks.
- the panel is recorded elsewhere → **move**: post the new one, re-point the
  ids, and tell the admin where the old one still is. Deleting it would be this
  code destroying a message nobody pointed at.
- the panel is there but Discord **won't let the bot edit it** → **replace**:
  post a new one, then delete the old. A panel posted as a slash-command reply
  belongs to a webhook, and Discord answers 200 to an embed edit on one of
  those and keeps the old embed — so its text can never be corrected, and
  refreshing it again is a silent no-op. This is the one case that deletes, and
  it is the opposite of the move above for a reason: a move leaves the old
  panel alone in a different channel, where it is still the only panel there,
  while this one would sit directly above its replacement with live buttons and
  text nothing can fix. The delete is bounded to the message id the bot itself
  recorded, and only runs once the replacement is up and saved.
- the recorded location **cannot be read** → post nothing. That is the case
  where "no panel exists" and "the database blinked" look identical.

The write path adds two routes on the bot, and both are pinned by tests —
`TestWriteSurfaceIsExactlyTwoThings` asserts there are no others:

- `PATCH /api/v1/guilds/{id}/settings` and `POST /api/v1/guilds/{id}/panel`,
  the only non-GET routes. Because the signed operation includes the
  **method and path**, a token minted to read a guild's settings cannot be
  replayed to write them, or to post its panel.
- One `POST` per group on the dashboard, behind a CSRF token and a
  `SameSite=Lax` cookie — though the check that matters is the bot's, which
  re-runs Administrator, re-runs the plan gate, and validates every value
  against its own allowlist. Nothing the website sends is trusted by the thing
  that writes the row.

Every change that actually alters a value is recorded in `dashboard_audit`
with the actor, the field, and both values. The actor comes from the verified
token claims, never from the request body. Nothing in this codebase updates or
deletes a row in that table: an audit trail exists to disagree with someone's
account of events, which it cannot do if the code that made the change can
rewrite the record of it.

The settings page shows that history back — ids resolved to names, an actor
who has since left the server shown by id rather than dropped. It covers
changes made **from the website only**; slash commands are not recorded, and
the page says so rather than implying the list is everything that ever
happened. An unreadable trail renders as "couldn't load", never as "no changes
have been made" — those are different facts and only one of them is reassuring.

**Sessions can be revoked, not just expired.** Signing out ends the session in
front of you — which is the one an attacker is *not* using. "Sign out
everywhere" ends every session signed in as you, on every device, and it sits
in the header bar on every page rather than on an account screen, because the
moment you want it is the moment you should not have to go looking for it. It
is scoped per user and never per guild: a session spans every server someone
administers, so "revoke access to this server" would either cut off people who
did nothing or cut off nothing at all.

The session file is created owner-only, and the table prunes itself whenever a
login starts — the only unauthenticated way to add a row to it is therefore
also the thing that clears the abandoned ones, with no scheduler involved.

Two rules the settings page exists to honour:

- **It mirrors the bot field for field, including the inconsistencies.** Some
  settings a lapsed plan refuses to *save* (nickname sync, panel branding); some
  it saves for anyone and simply doesn't *act* on (the unverified role, the
  custom DM). Those get different badges, because rendering the second as the
  first would leave the website refusing to show something an admin can plainly
  set with a slash command. `SETTINGS_FIELDS` in `bot.py` is the single source
  of that distinction, and a test fails if a field it defines goes unrendered.
- **A 403 and a 404 from the bot are one indistinguishable answer.** The
  difference is meaningful behind mTLS and dangerous on the open web: shown
  separately, a signed-in user could walk guild ids and enumerate which servers
  run 18+ gating.

It also surfaces things the bot could previously only discover mid-verification
— a verified role the bot cannot grant, a log channel it cannot post in, an
announcement channel other servers could follow — while an admin is looking at
the setting rather than when a member is waiting.

**Buying Premium stays inside Discord.** A server that isn't subscribed gets an
upgrade block, and it points at `/vrcverify_subscription` rather than carrying a
buy button of its own. Discord's Store deep link
(`/application-directory/{app}/store/{sku}`) takes an application and a SKU and
nothing else — there is no guild parameter — so a link from a page about *this*
server cannot actually name it, and for a guild-scoped SKU choosing the wrong
one at checkout means billing the wrong server. The slash command's button is
already bound to the guild it was run in, so it leads; the store link is offered
alongside as somewhere to read the price and perks, and says plainly that
checkout is where the server gets picked. The SKU id travels in the settings
payload rather than being configured on the dashboard, for the same reason the
language list does: a second copy is a second thing to get wrong, and with
`PREMIUM_SKU_ID` unset the bot sends none and the block never renders — there is
nothing to sell when every gate already answers "allowed".

Grandfathered servers see the block too, with different wording. They keep their
three features permanently either way, but the premium-only set is still closed
to them, so treating them as already-sold would leave a badge with no route past
it.

---

## Tech Stack

- **Language:** Python 3.12 in the current containerized deployment
- **Discord Bot Library:** [discord.py](https://github.com/Rapptz/discord.py)
- **Database ORM:** SQLAlchemy
- **Message Queue:** RabbitMQ (via pika)
- **VRChat API:** [vrchatapi](https://github.com/vrchatapi/vrchatapi)
- **Environment Variables:** python-dotenv
- **Additional Libraries:** asyncio, logging, imaplib (for Gmail integration), and more.

---

## Getting Started

### Prerequisites

- **Python:** Version 3.12 is used in the current containerized environment. Python 3.8+ may work, but current behavior and dependencies are validated against Python 3.12.
- **Discord Bot:** Create a Discord application and bot account. Obtain the bot token.
- **Database:** A PostgreSQL (or compatible) database. Set the connection URL in your `.env`.
- **RabbitMQ:** A RabbitMQ server instance with the appropriate queues set up.
- **VRChat Credentials:** Valid VRChat username and password.
- **Gmail IMAP Credentials:** A Gmail account with an app password configured for IMAP access.

### Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/PhillipDiCarlo/vrchat-verify-bot.git
   cd vrchat-verify-bot
   ```

2. **Install Dependencies:**

   It is recommended to use a virtual environment:

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
  pip install -r config/other_configs/requirements.txt
   ```

  Docker builds use split requirement files:

  - `config/other_configs/requirements-bot.txt` for `src/bot.py`
  - `config/other_configs/requirements-checker.txt` for `src/vrc_online_checker.py`

3. **Create and Configure the `.env` File:**

   Create a `.env` file in the root directory with the following variables:

   ```dotenv
   # Discord Bot Token and Database URL
   DISCORD_BOT_TOKEN=your_discord_bot_token_here
   DATABASE_URL=your_database_connection_url

   # VRChat Credentials (for vrc_online_checker.py)
   VRCHAT_USERNAME=your_vrchat_username
   VRCHAT_PASSWORD=your_vrchat_password

   # Group-invite bot: a SEPARATE VRChat account and 2FA mailbox (optional).
   # Its mailbox must differ from GMAIL_USER and its session file must differ
   # from VRCHAT_SESSION_FILE -- see .env.example for why both matter.
   # INVITE_VRCHAT_USERNAME=
   # INVITE_VRCHAT_PASSWORD=
   # INVITE_GMAIL_USER=
   # INVITE_GMAIL_APP_PASSWORD=
   # INVITE_VRCHAT_USER_ID=
   # INVITE_VRCHAT_SESSION_FILE=/data/vrchat_invite_session.txt
   # RABBITMQ_GROUP_INVITE_QUEUE=vrcverify_group_invites

   # RabbitMQ Configuration
   RABBITMQ_HOST=your_rabbitmq_host
   RABBITMQ_PORT=your_rabbitmq_port
   RABBITMQ_USERNAME=your_rabbitmq_username
   RABBITMQ_PASSWORD=your_rabbitmq_password
   RABBITMQ_VHOST=
   RABBITMQ_QUEUE_NAME=your_request_queue_name
   RABBITMQ_RESULT_QUEUE=your_result_queue_name

  # Optional: RabbitMQ connection health + retry tuning (recommended for long uptime)
  # These are used by BOTH the bot and checker.
  RABBITMQ_HEARTBEAT=60
  RABBITMQ_BLOCKED_TIMEOUT=60
  RABBITMQ_CONN_ATTEMPTS=3
  RABBITMQ_RETRY_DELAY=2
  RABBITMQ_SOCKET_TIMEOUT=10
  RABBITMQ_PUBLISH_TRIES=3

   # Gmail Login for 2FA Code Fetching
   GMAIL_USER=your_gmail_address
   GMAIL_APP_PASSWORD=your_gmail_app_password

   # Logging Level: DEBUG, INFO, WARNING, ERROR, CRITICAL
   LOG_LEVEL=INFO

  # Optional: Bot-side REST caching and concurrency controls
  REST_TTL_SECONDS=180
  REST_CACHE_MAX=10000
  REST_CONCURRENCY=8

  # Optional: per-user cooldown (seconds) between verification/nickname requests
  VERIFICATION_COOLDOWN_SECONDS=10

  # Optional: VRChat checker login/lookup tuning
  VRCHAT_RELOGIN_INTERVAL_SECONDS=600
  VRCHAT_API_CONNECT_TIMEOUT_SECONDS=10
  VRCHAT_API_READ_TIMEOUT_SECONDS=20
  VRCHAT_STATUS_SUMMARY_URL=https://status.vrchat.com/api/v2/summary.json
  VRCHAT_STATUS_CACHE_SECONDS=120
  VRCHAT_LOOKUP_RETRIES=3
  VRCHAT_LOOKUP_BACKOFF_BASE=1.5

  # Optional: persist the VRChat auth cookie so a restarted checker resumes
  # its session instead of re-authenticating. Every fresh login consumes a
  # 2FA email and VRChat rate-limits that endpoint (429), so several quick
  # redeploys can lock the account out of logging in at all. The file is an
  # auth credential: it is written 0600 and belongs on a private volume.
  # Leave unset to disable. Docker Compose defaults it to /data (a named
  # volume); a stale/rejected session is discarded and login retried clean.
  VRCHAT_SESSION_FILE=/data/vrchat_session.txt

  # Optional: read bios from VRChat's newer GET /profile/{id} endpoint.
  # Defaults to true. GET /users/{id} can return a bio that is hours stale,
  # which silently fails verification for users whose code really is in their
  # bio; /profile/{id} reflects edits immediately. Falls back to /users/{id}
  # automatically if /profile/ errors. Set to false to force the old path.
  VRCHAT_USE_PROFILE_ENDPOINT=true

  # Optional: instruction message refresh trigger watched by bot.py
  INSTRUCTIONS_TRIGGER_PATH=/tmp/update_instructions.trigger
  INSTRUCTIONS_TRIGGER_POLL=5

  # Optional: startup instruction panel refresh tuning
  # (edits in flight, and edits started per second; keep RATE under
  # Discord's ~50 req/s global ceiling so verification traffic isn't throttled)
  INSTRUCTIONS_REFRESH_CONCURRENCY=10
  INSTRUCTIONS_REFRESH_RATE=25
   ```

   **Instruction panel buttons on restart.** Panel buttons use fixed
   `custom_id`s and are registered once at startup via a persistent view, so
   already-posted panels keep working across restarts without being re-edited.
   The `instruction_panel_views` table records which button version each guild's
   panel carries; it is created automatically, no manual migration needed. On
   boot the bot only re-edits panels that predate the current version, plus any
   that previously failed (revoked permissions, archived threads), which are
   retried each boot so they recover once an admin fixes the channel.

4. **Database Setup:**

   The bot automatically creates the necessary tables using SQLAlchemy when it runs. Make sure your database is reachable via the `DATABASE_URL`.

   **Verification history (`verification_daily`).** The dashboard's Overview
   page reports how many verifications a server completed today, in the last 7
   days and in the last 30. None of that can be derived from
   `servers.verification_count`, which is a running total with no history behind
   it, so the counts come from their own table: one row per guild per UTC day,
   holding a guild id, a date and a count. It is created automatically, no
   manual migration needed.

   The table deliberately stores **no member identifiers and no per-person
   timestamps**. A count cannot be turned back into a person, which is the
   property that makes it safe to keep for a product whose job is to answer "is
   this person over 18" and then forget. Adding a column here would change that,
   and `tests/test_overview.py` asserts the column list so it cannot happen
   quietly.

   History starts when the table does. Windows that reach back before the first
   row are shown blank on the Overview rather than as zero — see
   `src/dashboard/overview_view.py` for why those two must not look the same.
   Nothing prunes the table today; at one small row per active guild per day it
   is not a growth concern, but a deployment keeping years of it may want a
   periodic delete of rows older than ~400 days.

---

## Running the Bot and Checker

- **Start the Discord Bot:**

  ```bash
  python src/bot.py
  ```

- **Start the VRChat Online Checker (in a separate terminal):**

  ```bash
  python src/vrc_online_checker.py
  ```

Each component connects to RabbitMQ to exchange verification requests and results.

---

  ## Localization

  The bot supports multiple locales for user-facing content. A server admin can choose the preferred language on the dashboard (Instructions language). If a user’s Discord locale is supported, it will be used; otherwise, English (en-US) is the fallback.

  Add or adjust localized strings in `src/locales.py`. The list of supported language codes is defined in `LANGUAGE_CODES`.

---

  ## Background jobs and reliability

  - A dedicated RabbitMQ consumer in the bot listens for verification results from the checker and assigns/removes roles accordingly.
  - Both the bot and checker use resilient RabbitMQ connections (heartbeats + timeouts) and will automatically reconnect if the broker restarts.
  - RabbitMQ publishes are retried and sent as persistent messages (delivery_mode=2).
  - A periodic cleanup task removes expired entries from the `PendingVerification` table to keep the database tidy.
  - TTL caches and concurrency gates help reduce rate-limit pressure on Discord and VRChat APIs.
  - The checker attempts VRChat login once at startup, then retries on a background interval instead of retrying on every verification request.
  - When VRChat is unavailable or auth is temporarily broken, the checker still returns structured failure metadata so the bot can DM users a localized temporary-unavailable or outage message.
  - VRChat login and lookup requests use explicit connect/read timeouts controlled by environment variables.

---

  ## Docker and container images

  This repository includes Dockerfiles for the bot and the online checker:

  - `docker/Dockerfile-bot`
  - `docker/Dockerfile-online-checker`

  There are two example Compose files under `config/other_configs/`:

  - `docker-compose.yml` — builds images locally from this repo (development).
  - `docker-compose.deploy.yml` — pulls published images from Docker Hub by **explicit version tag**. `VRCVERIFY_VERSION` is required and there is deliberately no `latest` fallback: deploying a mutable `latest` tag would mean a registry-account compromise turns into code execution on the deploy host at the next pull.

  Make sure your environment variables are provided via an `.env` file or your preferred secret management solution.

  Examples (adjust paths and variables to your environment):

  ```bash
  # Local build (development)
  docker compose -f config/other_configs/docker-compose.yml up -d

  # Deployment: pin to the version you just pushed
  VRCVERIFY_VERSION=2.6.0 docker compose -f config/other_configs/docker-compose.deploy.yml up -d
  ```

  To tag and push images, optional helper scripts are provided:

  - `tag_and_push_images.sh` (bash)
  - `tag_and_push_images.ps1` (PowerShell)
  
---

## Usage

### For Users

- **Verify Your VRChat 18+ Status:**  
  Use the slash command `/vrcverify` to initiate verification. You will be prompted to enter your VRChat username, and the bot will generate a unique code that must be added to your VRChat bio. Once updated, press the "Verify" button to complete the process.

### For Administrators

- **Setup Server Configuration:**  
  Use `/vrcverify_setup` to set or update the role that will be assigned to verified users.
- **Additional Commands:**
  - `/vrcverify_subscription` – See this server's premium status, and subscribe if it isn't.
  - `/vrcverify_support` – Receive help and support information.
  - `/vrcverify_instructions` – Post instructions in an embed for server members.
  - `/vrcverify_settings` – Show this server's settings and link to the
    dashboard. Read-only; editing lives on the website.
  - `/vrcverify_setrequestmessage`, `/vrcverify_logchannel` – The same summary.
    Kept so the old names still answer.

---

## Testing

Unit tests live under `tests/` and cover locale consistency, bot helper logic (input parsing, custom-message sanitizing, outage message mapping), and checker logic (bio code matching, API error classification, status-page parsing) with all network access mocked.

```bash
pip install -r config/other_configs/requirements-dev.txt
pytest
```

Note: `src/test_vrc.py` is a manual VRChat login script, not part of the automated suite (pytest only collects from `tests/`, per `pytest.ini`).

---

## Contributing

Contributions to VRChat Verify Bot are welcome. If you have suggestions or improvements, please fork the repository and open a pull request. Be sure to update tests as appropriate.

---

## Acknowledgments

- [discord.py](https://github.com/Rapptz/discord.py) for the Discord integration.
- [SQLAlchemy](https://www.sqlalchemy.org/) for ORM support.
- [pika](https://pika.readthedocs.io/) for RabbitMQ messaging.
- [vrchatapi](https://github.com/vrchatapi/vrchatapi) for the VRChat API integration.
- The developers and community members who contribute to these projects.
