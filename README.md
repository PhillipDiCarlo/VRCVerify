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

---

## Recent updates

- Localization support with per-server locale setting (supports multiple language codes; see Localization section).
- Admin settings UI (/vrcverify_settings) with paged controls for:
  - Auto nickname change to VRChat display name on successful verification.
  - Instructions language (per-server locale) used for embeds and button flows.
  - Auto-verify new members when they join (opt-in).
  - Optional removal of an "unverified" role once a user becomes verified.
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
  - **Server:** Holds configuration for each Discord server (guild), including role assignments. (Its `subscription_status` / `email` / `last_renewal_date` columns are dormant leftovers from a removed Stripe integration and are not used by anything — premium keys off Discord entitlements instead.)
  - **User:** Stores individual user verification statuses and VRChat IDs.
  - **PendingVerification:** Temporarily holds verification requests until they are processed.
  - **PremiumCutoverNotice:** Which guilds have already had the one-time premium announcement DM. (Whether a server is *grandfathered* is not stored — it's `servers.id <= PREMIUM_GRANDFATHER_MAX_ID`.)
  - **VerificationLogChannel:** Where a guild posts its verification activity log.

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
- `/vrcverify_settings` – Admin-only. Opens a paged settings UI with:
  - Auto nickname change (on successful verification, set Discord nickname to VRChat display name).
  - Instructions language (server-wide locale).
  - Auto-verify new members (attempt verification or initiate the flow when a member joins).
- `/vrcverify_setrequestmessage` – Admin-only. Opens a modal to set or clear the custom DM shown after a successful verification role assignment.
- `/vrcverify_logchannel` – Admin-only, premium. Chooses where verification activity is logged; run with no channel to turn it off.
- `/vrcverify_support` – Anyone. Sends help/support information.
- `/vrcverify_subscription` – Admin-only. Shows this server's premium status and, if it isn't subscribed, Discord's purchase button.

---

## Premium tier

18+ verification itself is free and always will be. A per-server subscription,
billed natively through Discord App Subscriptions (guild-scoped SKU, monthly),
unlocks the automation around it:

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

Auto-verify-on-join is free for everyone and is deliberately not gated at all —
`on_member_join` never so much as reads an entitlement. Users read "the bot
recognises me and gives me the role" as simply how a verification bot works, so
charging for it reads as the bot being broken rather than as an upsell. It's
also the only gated feature a *member* could perceive, and members move between
servers. `tests/test_premium.py::TestAutoVerifyOnJoinIsFree` pins this so it
can't drift back behind the paywall.

\* Servers configured before the tier launched — defined as
`servers.id <= PREMIUM_GRANDFATHER_MAX_ID` (default 820, where the
autoincrementing key stood at the start of July 2026). Using the existing
primary key means there is nothing to backfill and no way for the line to
drift; a restored database draws it in exactly the same place.

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

### Verification activity log

`/vrcverify_logchannel #channel` posts each verification outcome to a channel —
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

  The bot supports multiple locales for user-facing content. A server admin can choose the preferred language via `/vrcverify_settings` (Instructions language). If a user’s Discord locale is supported, it will be used; otherwise, English (en-US) is the fallback.

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
  VRCVERIFY_VERSION=1.4.0 docker compose -f config/other_configs/docker-compose.deploy.yml up -d
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
  - `/vrcverify_settings` – Configure auto nickname change, instructions language, and auto-verify-on-join. The auto-nickname page is shown locked with a purchase button (rather than hidden) on servers without premium.
  - `/vrcverify_setrequestmessage` – Configure the optional custom success DM sent after successful verification.

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
