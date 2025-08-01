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
   - Checks the target VRChat user’s profile for age verification status and whether the provided code is present in their bio.
   - Sends back the verification result via a RabbitMQ result queue.

---

## Architecture Overview

- **Discord Integration:**  
  Uses the [discord.py](https://github.com/Rapptz/discord.py) library to create slash commands (e.g., `/vrcverify`, `/vrcverify_setup`, `/vrcverify_support`) that let users initiate the verification process and administrators configure server settings.

- **Database:**  
  Utilizes SQLAlchemy to manage three primary models:
  - **Server:** Holds configuration for each Discord server (guild), including role assignments and subscription details.
  - **User:** Stores individual user verification statuses and VRChat IDs.
  - **PendingVerification:** Temporarily holds verification requests until they are processed.

- **Messaging with RabbitMQ:**  
  Uses the pika library to handle two queues:
  - A request queue (for sending verification requests from the bot to the checker).
  - A result queue (for receiving verification outcomes back in the bot).

- **VRChat API Integration:**  
  Uses the `vrchatapi` library to interact with VRChat’s API. The online checker handles VRChat login, including two-factor authentication by checking the inbox of a Gmail account via IMAP.

---

## Tech Stack

- **Language:** Python 3.8+
- **Discord Bot Library:** [discord.py](https://github.com/Rapptz/discord.py)
- **Database ORM:** SQLAlchemy
- **Message Queue:** RabbitMQ (via pika)
- **VRChat API:** [vrchatapi](https://github.com/vrchatapi/vrchatapi)
- **Environment Variables:** python-dotenv
- **Additional Libraries:** asyncio, logging, imaplib (for Gmail integration), and more.

---

## Getting Started

### Prerequisites

- **Python:** Version 3.8 or higher.
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
   pip install -r requirements.txt
   ```

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

   # Gmail Login for 2FA Code Fetching
   GMAIL_USER=your_gmail_address
   GMAIL_APP_PASSWORD=your_gmail_app_password

   # Logging Level: DEBUG, INFO, WARNING, ERROR, CRITICAL
   LOG_LEVEL=INFO
   ```

4. **Database Setup:**

   The bot automatically creates the necessary tables using SQLAlchemy when it runs. Make sure your database is reachable via the `DATABASE_URL`.

---

## Running the Bot and Checker

- **Start the Discord Bot:**

  ```bash
  python bot.py
  ```

- **Start the VRChat Online Checker (in a separate terminal):**

  ```bash
  python vrc_online_checker.py
  ```

Each component connects to RabbitMQ to exchange verification requests and results.

---

## Usage

### For Users

- **Verify Your VRChat 18+ Status:**  
  Use the slash command `/vrcverify` to initiate verification. You will be prompted to enter your VRChat username, and the bot will generate a unique code that must be added to your VRChat bio. Once updated, press the "Verify" button to complete the process.

### For Administrators

- **Setup Server Configuration:**  
  Use `/vrcverify_setup` to set or update the role that will be assigned to verified users.
- **Additional Commands:**
  - `/vrcverify_subscription` – Get subscription or premium feature information.
  - `/vrcverify_support` – Receive help and support information.
  - `/vrcverify_instructions` – Post instructions in an embed for server members.

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
