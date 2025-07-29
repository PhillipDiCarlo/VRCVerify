import os
import json
import asyncio
import logging
import random
import string
import re

import discord
from discord import app_commands, Embed
from discord.ext import commands
from discord.ui import View, Button, Select

import pika
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import os
import discord
import json
import random
import string
import re
import pika
import asyncio
from locales import localizations, LANGUAGE_CODES

# --- Localization Helpers ---
def get_locale(interaction: discord.Interaction) -> str:
    """Return best matching locale code or fallback to English."""
    loc = getattr(interaction, 'locale', None)
    return loc if loc in LANGUAGE_CODES else 'en-US'

def get_message(key: str, interaction: discord.Interaction, **kwargs) -> str:
    """Fetch localized template and format with kwargs."""
    locale = get_locale(interaction)
    template = localizations.get(locale, localizations['en-US']).get(key)
    if template is None:
        template = localizations['en-US'].get(key, key)
    return template.format(**kwargs)
from locales import localizations, LANGUAGE_CODES

def get_locale(interaction: discord.Interaction) -> str:
    """Return the best matching locale for this interaction."""
    loc = getattr(interaction, 'locale', None)
    return loc if loc in LANGUAGE_CODES else 'en-US'

def get_message(key: str, interaction: discord.Interaction, **kwargs) -> str:
    """Fetch a localized message by key for the given interaction."""
    locale = get_locale(interaction)
    template = localizations.get(locale, localizations['en-US']).get(key)
    if template is None:
        template = localizations['en-US'].get(key, key)
    return template.format(**kwargs)

# -------------------------------------------------------------------
# Load environment variables
# -------------------------------------------------------------------
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')


RABBITMQ_REQUEST_QUEUE = os.getenv("RABBITMQ_QUEUE_NAME") # The queue to which we send verification requests (the "inbound" queue for vrc_online_checker).
RABBITMQ_RESULT_QUEUE = os.getenv("RABBITMQ_RESULT_QUEUE") # The queue from which we *receive* the verification results back from vrc_online_checker.
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")

# -------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# SQLAlchemy setup
# -------------------------------------------------------------------
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
Base = declarative_base()

@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()

# -------------------------------------------------------------------
# Database Models
# -------------------------------------------------------------------
class Server(Base):
    __tablename__ = 'servers'
    id = Column(Integer, primary_key=True)
    server_id = Column(String, unique=True, nullable=False)
    owner_id = Column(String, nullable=False)
    role_id = Column(String, nullable=True)
    subscription_status = Column(Boolean, default=False)
    subscription_start_date = Column(DateTime, nullable=True)
    email = Column(String, nullable=True)
    last_renewal_date = Column(DateTime, nullable=True)
    instructions_channel_id = Column(String, nullable=True)
    instructions_message_id = Column(String, nullable=True)
    auto_nickname_change = Column(Boolean, default=False)
    instructions_locale = Column(String, default='en-US', nullable=False)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(30), unique=True, nullable=False)
    verification_status = Column(Boolean, default=False)
    vrc_user_id = Column(String(50), nullable=True)
    last_verification_attempt = Column(DateTime(timezone=True))

class PendingVerification(Base):
    __tablename__ = "pending_verifications"
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(30), nullable=False)
    guild_id = Column(String(30), nullable=False)
    vrc_user_id = Column(String(50), nullable=False)
    verification_code = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)

# Create tables and ensure the new instructions_locale column exists
Base.metadata.create_all(engine)
try:
    with engine.connect() as conn:
        # add column if missing (PostgreSQL syntax)
        conn.execute(text(
            "ALTER TABLE servers ADD COLUMN IF NOT EXISTS instructions_locale VARCHAR NOT NULL DEFAULT 'en-US';"
        ))
except Exception as e:
    logger.warning(f"Could not auto-migrate instructions_locale column: {e}")

# -------------------------------------------------------------------
# RabbitMQ Setup
# -------------------------------------------------------------------
credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST,
    port=RABBITMQ_PORT,
    virtual_host=RABBITMQ_VHOST,
    credentials=credentials
)

# -------------------------------------------------------------------
# Discord Bot
# -------------------------------------------------------------------
intents = discord.Intents.default()

class VRCVerifyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync slash commands to the server
        await self.tree.sync()

bot = VRCVerifyBot()


# -------------------------------------------------------------------
# Instruction Button
# -------------------------------------------------------------------
class VRCVerifyInstructionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Begin Verification", style=discord.ButtonStyle.primary)
    async def begin_verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Instead of calling vrcverify directly, call the helper
        await process_verification(interaction)

    @discord.ui.button(label="Update Nickname", style=discord.ButtonStyle.secondary)
    async def update_nickname(self, interaction, button):
        user_id = str(interaction.user.id)
        # fetch everything you need inside the session
        with session_scope() as session:
            user = session.query(User).filter_by(discord_id=user_id).first()
        if not user:
            return await interaction.response.send_message(
                get_message('not_verified', interaction),
                ephemeral=True
            )
            vrc_user_id = user.vrc_user_id  # grab this while session is open

        # now it’s just a local variable, no lazy load needed
        await publish_to_vrc_checker(
            discord_id=user_id,
            vrc_user_id=vrc_user_id,
            guild_id=str(interaction.guild.id),
            code=None,
            update_nickname=True
        )
        await interaction.response.send_message(
            get_message('nickname_update_requested', interaction),
            ephemeral=True
        )

# -------------------------------------------------------------------
# Show Settings View
# -------------------------------------------------------------------
class SettingsView(View):
    def __init__(self, auto_nick: bool, instr_locale: str):
        super().__init__(timeout=None)
        self.auto_nick = auto_nick
        self.selected_nick: str | None = None
        self.selected_locale: str | None = None

        # dropdown for Yes/No nickname setting
        nick_options = [
            discord.SelectOption(label="Yes", value="yes", default=auto_nick),
            discord.SelectOption(label="No",  value="no",  default=not auto_nick),
        ]
        self.nick_dropdown = Select(
            placeholder="Enable auto nickname change",
            min_values=1,
            max_values=1,
            options=nick_options
        )
        self.nick_dropdown.callback = self.on_nick_select
        self.add_item(self.nick_dropdown)

        # dropdown for instructions locale
        locale_options = [
            discord.SelectOption(label=code, value=code, default=(code==instr_locale))
            for code in LANGUAGE_CODES
        ]
        self.locale_dropdown = Select(
            placeholder="Instructions message language",
            min_values=1,
            max_values=1,
            options=locale_options
        )
        self.locale_dropdown.callback = self.on_locale_select
        self.add_item(self.locale_dropdown)

        # save button
        self.save_btn = Button(label="Save", style=discord.ButtonStyle.primary)
        self.save_btn.callback = self.on_save
        self.add_item(self.save_btn)

    async def on_nick_select(self, interaction: discord.Interaction):
        self.selected_nick = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
    async def on_locale_select(self, interaction: discord.Interaction):
        self.selected_locale = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)

    async def on_save(self, interaction: discord.Interaction):
        # determine new settings
        nick_choice = self.selected_nick or ("yes" if self.nick_dropdown.options[0].default else "no")
        new_nick = (nick_choice == "yes")
        new_locale = self.selected_locale or self.locale_dropdown.options[0].value

        # persist into your servers table
        with session_scope() as session:
            srv = session.query(Server).filter_by(server_id=str(interaction.guild.id)).first()
            if not srv:
                srv = Server(server_id=str(interaction.guild.id), owner_id=str(interaction.user.id))
                session.add(srv)
            srv.auto_nickname_change = new_nick
            srv.instructions_locale = new_locale

        # confirm & remove view
        # localized confirmation
        msg = get_message('settings_saved', interaction,
                          nickname='Yes' if new_nick else 'No',
                          locale=new_locale)
        await interaction.response.edit_message(
            content=msg,
            view=None
        )

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
async def process_verification(interaction: discord.Interaction):
    """
    Processes a verification request by doing one of the following:
      1. If the server's configuration is missing (or the verification role is not set), it notifies the user.
      2. If the user is already verified, it defers the response, assigns the role (or re-assigns), and sends a followup message.
      3. If the user exists but is not verified, it triggers a "no-code" re-check by publishing a verification request.
      4. If the user is not in the database, it presents a modal for the user to enter their VRChat username.
    """
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)

    # Use a session block to load data and extract only the necessary values.
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server or not server.role_id:
            await interaction.response.send_message(
                get_message('setup_missing', interaction),
                ephemeral=True
            )
            return

        # Extract the values you need from the user object while still in the session.
        user = session.query(User).filter_by(discord_id=user_id).first()
        if user:
            is_verified = user.verification_status
            stored_vrc_user_id = user.vrc_user_id or ""
        else:
            is_verified = None
            stored_vrc_user_id = None

    # CASE A: User exists and is verified.
    if user is not None and is_verified:
        await interaction.response.defer(ephemeral=True)
        await assign_role(user_id, True, guild_id)
        await interaction.followup.send(
            get_message('already_verified', interaction),
            ephemeral=True
        )
        return

    # CASE B: User exists but is not verified => trigger a "no-code" re-check.
    if user is not None and not is_verified:
        await interaction.response.defer(ephemeral=True)
        await publish_to_vrc_checker(
            discord_id=user_id,
            vrc_user_id=stored_vrc_user_id,
            guild_id=guild_id,
            code=None  # No-code re-check
        )
        await interaction.followup.send(
            get_message('recheck_started', interaction),
            ephemeral=True
        )
        return

    # CASE C: User is not in the database => show the VRChat username modal.
    await interaction.response.send_modal(VRCUsernameModal(interaction))

def generate_verification_code() -> str:
    return "VRC-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def publish_to_vrc_checker(
    discord_id: str,
    vrc_user_id: str,
    guild_id: str,
    code: str | None,
    update_nickname: bool = False
):
    def _publish():
        message = {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID":   guild_id,
            "verificationCode": code
        }
        if update_nickname:
            message["updateNickname"] = True

        conn    = pika.BlockingConnection(parameters)
        channel = conn.channel()
        channel.queue_declare(queue=RABBITMQ_REQUEST_QUEUE, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=RABBITMQ_REQUEST_QUEUE,
            body=json.dumps(message)
        )
        conn.close()
        logger.info("📤 Sent to vrc_online_checker: %s", message)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _publish)

async def assign_role(
    discord_id: str,
    is_18_plus: bool,
    guild_id: str,
    verification_code: str | None = None,   # no longer required for nickname logic
    display_name:   str | None = None
):
    """
    Assigns or skips the 18+ role in one guild.
    Automatically updates the nickname if the server setting is on.
    """
    # Load server settings
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        role_id = server.role_id if server else None
        auto_nick = server.auto_nickname_change if server else False

    guild = bot.get_guild(int(guild_id))
    if not guild:
        logger.warning(f"⚠️ Guild {guild_id} not found.")
        return

    try:
        member = await guild.fetch_member(int(discord_id))
    except discord.NotFound:
        logger.warning(f"⚠️ Member {discord_id} not in guild.")
        return

    if not role_id:
        logger.warning(f"⚠️ No verification role configured for guild {guild_id}.")
        return

    role = discord.utils.get(guild.roles, id=int(role_id))
    if not role:
        logger.warning(f"⚠️ Role ID {role_id} missing in guild {guild_id}.")
        return

    # Assign or notify
    if is_18_plus:
        try:
            await member.add_roles(role)
            logger.info(f"✅ Assigned role {role.name} to {member}.")
            try:
                await member.send(f"✅ You’ve been verified and given **{role.name}** in **{guild.name}**!")
            except discord.Forbidden:
                logger.warning("⚠️ Cannot DM user after role assign.")
        except discord.Forbidden:
            logger.warning(f"⚠️ Missing permission to add {role.name} in {guild_id}.")

        # Auto-nickname change if enabled
        if auto_nick and display_name:
            try:
                await member.edit(nick=display_name)
                logger.info(f"🔄 Updated nickname to {display_name} for {member}.")
                try:
                    await member.send(f"🔔 Your nickname was updated to **{display_name}**.")
                except discord.Forbidden:
                    logger.warning("⚠️ Cannot DM after nickname update.")
            except discord.Forbidden:
                # let user know we couldn’t rename them
                try:
                    await member.send("⚠️ We could not update your username.")
                except discord.Forbidden:
                    logger.warning("⚠️ Cannot DM user after nickname failure.")
    else:
        # Not 18+
        try:
            await member.send("❌ You are not 18+ according to VRChat. Contact an admin if this is an error.")
        except discord.Forbidden:
            logger.warning("⚠️ Cannot DM user about 18+ status.")


# -------------------------------------------------------------------
# Modal: Collect VRChat Username
# -------------------------------------------------------------------
class VRCUsernameModal(discord.ui.Modal, title="Enter Your VRChat Profile URL or UserID"):
    vrc_username = discord.ui.TextInput(
        label="VRChat Profile URL or UserID",
        placeholder="https://vrchat.com/home/user/usr_1234d567-b12e-123d-a1c2-fd12345a67ea"
    )

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        raw_input = self.vrc_username.value.strip()

        # 1) If they pasted the full URL, extract the userID
        url_pattern = r'https?://vrchat\.com/home/user/([A-Za-z0-9\-_]+)'
        m = re.match(url_pattern, raw_input)
        if m:
            vrc_user_id = m.group(1)
        # 2) If they already only entered the usr_… ID, accept it
        elif raw_input.startswith('usr_'):
            vrc_user_id = raw_input
        # 3) Otherwise, they probably typed a display name ➔ warn & cancel
        else:
            await interaction.response.send_message(
                "❌ It looks like you entered your display name instead of your VRChat userID.\n"
                "Please enter either the full profile URL or your userID (which always starts with `usr_`).\n https://imgur.com/a/EEl6ekH",
                ephemeral=True
            )
            return

        # …and now proceed exactly as before, but using `vrc_user_id`
        discord_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        verification_code = generate_verification_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        with session_scope() as session:
            # Remove any old pending entry for this user/guild
            session.query(PendingVerification).filter_by(discord_id=discord_id, guild_id=guild_id).delete()

            pending = PendingVerification(
                discord_id=discord_id,
                guild_id=guild_id,
                vrc_user_id=vrc_user_id,
                verification_code=verification_code,
                expires_at=expires_at
            )
            session.add(pending)

        view = VRCVerificationButton(vrc_user_id, verification_code, guild_id)
        await interaction.response.send_message(
            f"✅ **VRChat userID saved!**\n\n"
            f"**1) Add this code to your VRChat bio:**\n"
            f"```\n{verification_code}\n```\n"
            f"**2) Once you've updated your bio, click “Verify” below (within 5 minutes).**",
            view=view,
            ephemeral=True
        )

# -------------------------------------------------------------------
# Button: triggers code-based check
# -------------------------------------------------------------------
class VRCVerificationButton(discord.ui.View):
    def __init__(self, vrc_username: str, verification_code: str, guild_id: str):
        super().__init__(timeout=None)
        self.vrc_username = vrc_username
        self.verification_code = verification_code
        self.guild_id = guild_id

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        When pressed, we publish a request with verificationCode != None, 
        meaning code-based flow. 
        """
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        await publish_to_vrc_checker(
            discord_id,
            self.vrc_username,
            self.guild_id,
            self.verification_code
        )

        await interaction.followup.send(
            get_message('verification_requested', interaction),
            ephemeral=True
        )

# -------------------------------------------------------------------
# Slash Command: /vrcverify
# -------------------------------------------------------------------
@bot.tree.command(name="vrcverify", description="Verify your VRChat 18+ status")
async def vrcverify(interaction: discord.Interaction):
    await process_verification(interaction)

# -------------------------------------------------------------------
# Slash Command: /vrcverify_setup
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="vrcverify_setup", description="Admin command: Set or update the verified role for this server.")
@app_commands.describe(role="Select the Discord role to be assigned to verified users.")
async def vrcverify_setup(interaction: discord.Interaction, role: discord.Role):
    """
    Inserts or updates a row in the 'servers' table with this server_id, 
    storing the admin's user ID as 'owner_id' and the chosen role ID as 'role_id'.
    """
    guild_id = str(interaction.guild.id)
    owner_id = str(interaction.user.id)
    role_id_str = str(role.id)

    with session_scope() as session:
        # See if we already have a row for this server_id
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server:
            # If no entry, create one
            server = Server(
                server_id=guild_id,
                owner_id=owner_id,
                role_id=role_id_str
            )
            session.add(server)
            action = "created"
        else:
            # Update the existing row
            server.owner_id = owner_id  # optional, if you want to update the owner each time
            server.role_id = role_id_str
            action = "updated"

    # Let the admin know it worked
    await interaction.response.send_message(
        f"✅ **Successfully {action} server config** for this guild.\n"
        f"**Verified Role** set to: `{role.name}` (ID={role.id})",
        ephemeral=True
    )

# -------------------------------------------------------------------
# Slash Command: /vrcverify_subscription
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="vrcverify_subscription", description="Admin command: Get subscription info to unlock premium features.")
async def vrcverify_subscription(interaction: discord.Interaction):
    """
    Sends an ephemeral link or info to the admin about how to subscribe or purchase 
    premium features for your VRChat verification bot.
    """
    subscription_link = "https://esattotech.com/vrcverify-vrchat-age-verifier-for-discord/"
    kofi_link = "https://ko-fi.com/italiandogs"

    # localized subscription info
    await interaction.response.send_message(
        get_message('subscription_info', interaction, kofi_link=kofi_link),
        ephemeral=True
    )

# -------------------------------------------------------------------
# Slash Command: /vrcverify_support
# -------------------------------------------------------------------
@bot.tree.command(name="vrcverify_support", description="Get help with the VRChat 18+ verification process.")
async def vrcverify_support(interaction: discord.Interaction):
    """
    Sends an ephemeral message to the user with instructions on how to get support,
    whether that’s contacting an admin or visiting an external support link.
    """
    # Customize the text below however you like
    # localized support info
    await interaction.response.send_message(
        get_message('support_info', interaction),
        ephemeral=True
    )

# -------------------------------------------------------------------
# Slash Command: /vrcverify_instructions
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="vrcverify_instructions", description="Admin only: Post instructions for using the verification bot.")
async def vrcverify_instructions(interaction: discord.Interaction):
    # determine instructions locale (server setting overrides user locale)
    with session_scope() as session:
        srv = session.query(Server).filter_by(server_id=str(interaction.guild.id)).first()
        instr_locale = str(srv.instructions_locale) if srv and srv.instructions_locale else get_locale(interaction)
    # fetch localized messages for instructions
    strings = localizations.get(instr_locale, localizations['en-US'])
    embed = Embed(
        title=strings['instructions_title'],
        description=strings['instructions_desc'],
        color=discord.Color.blue()
    )

    usage_example = (
        "**Example Usage**:\n"
        "```bash\n"
        "/vrcverify\n"
        "```"
    )
    embed.add_field(name="Example Command", value=usage_example, inline=False)

    view = VRCVerifyInstructionView()
    # Send the initial response and then fetch the message
    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()

    # Save the channel and message IDs to your database for reinitialization.
    guild_id = str(interaction.guild.id)
    channel_id = str(interaction.channel.id)
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if server:
            server.instructions_channel_id = channel_id
            server.instructions_message_id = str(message.id)


# -------------------------------------------------------------------
# Slash Command: /vrcverify_settings
# -------------------------------------------------------------------
@bot.tree.command(
    name="vrcverify_settings",
    description="Admin: Configure VRChat-Verify settings"
)
@app_commands.checks.has_permissions(administrator=True)
async def vrcverify_settings(interaction: discord.Interaction):
    """Show the dropdown + Save button for your server’s settings."""
    # fetch current settings
    with session_scope() as session:
        srv = session.query(Server).filter_by(server_id=str(interaction.guild.id)).first()
        if srv:
            # ensure native Python types
            current = bool(srv.auto_nickname_change)
            raw_locale = getattr(srv, 'instructions_locale', None)
            current_locale = raw_locale or 'en-US'
        else:
            current = False
            current_locale = 'en-US'

    # include both auto-nick and instruction locale
    view = SettingsView(current, current_locale)
    await interaction.response.send_message(
        content=(
            "⚙️ **VRChat Verify Settings**\n\n"
            "1.) **Enable auto nickname change**\n"
            "   Automatically update users’ Discord nicknames to match their VRChat display names.\n"
            f"   Current: **{'Yes' if current else 'No'}**"
        ),
        view=view,
        ephemeral=True
    )

# -------------------------------------------------------------------
# RabbitMQ Consumer - handle verification results
# -------------------------------------------------------------------
async def consume_results_queue():
    loop = asyncio.get_running_loop()

    def on_message(ch, method, properties, body):
        data = json.loads(body)
        future = asyncio.run_coroutine_threadsafe(handle_verification_result(data), loop)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def do_blocking_consume():
        try:
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.queue_declare(queue=RABBITMQ_RESULT_QUEUE, durable=True)
            logger.info(f"✅ Listening for verification results on '{RABBITMQ_RESULT_QUEUE}'...")
            channel.basic_consume(
                queue=RABBITMQ_RESULT_QUEUE,
                on_message_callback=on_message,
                auto_ack=False
            )
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as e:
            logger.error(f"❌ RabbitMQ connection failed: {e}")

    await loop.run_in_executor(None, do_blocking_consume)

async def handle_verification_result(data: dict):
    """
    Called when vrc_online_checker returns a result.
    Distinguishes code-based flow vs. no-code re-check.
    Also handles on-demand nickname updates.
    """
    try:
        logger.info(f"🔎 Received verification result: {data}")
        discord_id = data.get("discordID")
        guild_id = data.get("guildID")
        is_18_plus = data.get("is_18_plus", False)
        verification_code = data.get("verificationCode")   # None => re-check
        update_nick       = data.get("updateNickname", False)
        display_name      = data.get("display_name")

        # — On-demand nickname update flow —
        if update_nick:
            guild  = bot.get_guild(int(guild_id))
            member = guild.get_member(int(discord_id)) if guild else None
            if member and display_name:
                # try to change nickname
                try:
                    await member.edit(nick=display_name)
                except discord.Forbidden:
                    # Notify user on failure
                    try:
                        await member.send("⚠️ We could not update your username.")
                    except discord.Forbidden:
                        logger.warning("⚠️ Cannot DM user after nickname failure.")
                    return
                # Confirm on success
                try:
                    await member.send(f"✅ Your nickname has been updated to **{display_name}**.")
                except discord.Forbidden:
                    logger.warning("⚠️ Cannot DM user after nickname success.")
            return

        # — No-code re-check flow —
        if verification_code is None:
            with session_scope() as session:
                user = session.query(User).filter_by(discord_id=discord_id).first()
                if not user:
                    logger.warning(f"⚠️ No user row for {discord_id} in re-check.")
                    return
                user.verification_status = is_18_plus
                # preserve vrc_user_id if provided
                if data.get("vrcUserID"):
                    user.vrc_user_id = data["vrcUserID"]

            # Now assign role + maybe nickname
            await assign_role(discord_id, is_18_plus, guild_id, display_name=display_name)
            return

        # — Code-based flow —
        now_utc = datetime.now(timezone.utc)
        with session_scope() as session:
            pending = (
                session.query(PendingVerification)
                .filter_by(discord_id=discord_id, guild_id=guild_id, verification_code=verification_code)
                .first()
            )
            if not pending:
                logger.warning(f"⚠️ No pending verification for {discord_id}/{verification_code}.")
                return
            if now_utc > pending.expires_at:
                session.delete(pending)
                logger.warning(f"⚠️ Verification code expired for {discord_id}.")
                return
            if not data.get("code_found", False):
                session.delete(pending)
                guild  = bot.get_guild(int(guild_id))
                member = guild.get_member(int(discord_id)) if guild else None
                if member:
                    try:
                        await member.send(
                            "❌ We couldn’t find your code in your VRChat bio. Please try again."
                        )
                    except discord.Forbidden:
                        logger.warning("⚠️ Cannot DM user about missing code.")
                return

            # Everything checks out — create/update user row
            user = session.query(User).filter_by(discord_id=discord_id).first()
            if not user:
                user = User(discord_id=discord_id)
                session.add(user)
            user.vrc_user_id = data["vrcUserID"]
            user.verification_status = is_18_plus
            session.delete(pending)

        # Assign role + maybe nickname
        await assign_role(discord_id, is_18_plus, guild_id, display_name=display_name)

    except Exception:
        logger.error("❌ Exception in handle_verification_result", exc_info=True)

# -------------------------------------------------------------------
# Bot Events
# -------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Bot is ready. Logged in as {bot.user} (ID: {bot.user.id})")
    bot.loop.create_task(consume_results_queue())
    
    # Reinitialize instruction messages across servers
    with session_scope() as session:
        servers = session.query(Server).filter(Server.instructions_message_id != None).all()
        servers_data = [
            (server.server_id, server.instructions_channel_id, server.instructions_message_id)
            for server in servers
        ]
    for server_id, channel_id, message_id in servers_data:
        guild = bot.get_guild(int(server_id))
        if not guild:
            continue
        try:
            channel = guild.get_channel(int(channel_id))
            if channel:
                message = await channel.fetch_message(int(message_id))
                await message.edit(view=VRCVerifyInstructionView())
                logger.info(f"Reinitialized instructions message for guild {server_id}")
        except Exception as e:
            logger.error(f"Error reinitializing instructions message for guild {server_id}: {e}")
        
    logger.info("Bot is reinitialized and ready to go!")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == '__main__':
    bot.run(DISCORD_BOT_TOKEN)