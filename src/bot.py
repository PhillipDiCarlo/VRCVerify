import os
import json
import asyncio
import logging
import random
import string

import discord
from discord import app_commands, Embed
from discord.ext import commands

import pika
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

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

Base.metadata.create_all(engine)

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
intents.members = True  # needed so that guild.get_member() works properly

class VRCVerifyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync slash commands to the server
        await self.tree.sync()

bot = VRCVerifyBot()

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def generate_verification_code() -> str:
    return "VRC-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def publish_to_vrc_checker(discord_id: str, vrc_user_id: str, guild_id: str, code: str | None):
    """Send a verification request to vrc_online_checker via RabbitMQ."""
    def _publish():
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=RABBITMQ_REQUEST_QUEUE, durable=True)

        message = {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "verificationCode": code  # None => re-check
        }
        channel.basic_publish(
            exchange="",
            routing_key=RABBITMQ_REQUEST_QUEUE,
            body=json.dumps(message)
        )
        connection.close()
        logger.info(f"📤 Sent verification request to vrc_online_checker: {message}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _publish)

async def assign_role(discord_id: str, is_18_plus: bool, guild_id: str, verification_code: str | None = None):
    """
    Assign or skip role in exactly one guild. 
    If user is not 18+, show a different DM depending on whether it was a code-based or no-code verification.
    """
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        role_id = server.role_id if server else None

    guild = bot.get_guild(int(guild_id))
    if not guild:
        logger.warning(f"⚠️ Could not find guild {guild_id}.")
        return

    member = guild.get_member(int(discord_id))
    if not member:
        logger.warning(f"⚠️ User {discord_id} not found in guild {guild_id}.")
        return

    if not role_id:
        logger.warning(f"⚠️ No role_id configured for guild {guild_id}.")
        return

    role = discord.utils.get(guild.roles, id=int(role_id))
    if not role:
        logger.warning(f"⚠️ Role {role_id} not found in guild {guild_id}.")
        return

    if is_18_plus:
        # Success => assign role
        try:
            await member.add_roles(role)
            logger.info(f"✅ Assigned {role.name} to {member} in {guild.name}.")
            try:
                await member.send(
                    f"✅ You have been verified and assigned **{role.name}** in **{guild.name}**!"
                )
            except discord.Forbidden:
                logger.warning("⚠️ Could not DM user; DMs off.")
        except discord.Forbidden:
            logger.warning(f"⚠️ Bot lacks permission to assign {role.name} in {guild_id}.")
    else:
        # Not 18+ => decide the DM based on whether we had a code or not
        if verification_code is None:
            # Re-check scenario
            try:
                await member.send(
                    "❌ Your VRChat profile is not set to '18+' (hidden or unverified). No role assigned."
                )
            except discord.Forbidden:
                logger.warning("⚠️ Could not DM user; DMs off.")
        else:
            # Code-based scenario
            try:
                await member.send(
                    "❌ The verification code was found in your bio, "
                    "but your VRChat profile is not set to '18+' (hidden or not verified). No role assigned."
                )
            except discord.Forbidden:
                logger.warning("⚠️ Could not DM user; DMs off.")

# -------------------------------------------------------------------
# Modal: Collect VRChat Username
# -------------------------------------------------------------------
class VRCUsernameModal(discord.ui.Modal, title="Enter Your VRChat Username"):
    vrc_username = discord.ui.TextInput(label="VRChat Username", placeholder="usr_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        """User has submitted their VRChat username => store a pending row, but do NOT publish to checker yet."""
        vrc_username = self.vrc_username.value.strip()
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
                vrc_user_id=vrc_username,
                verification_code=verification_code,
                expires_at=expires_at
            )
            session.add(pending)

        # Provide a "Verify" button that actually triggers the code-based check
        view = VRCVerificationButton(vrc_username, verification_code, guild_id)
        await interaction.response.send_message(
            f"✅ **VRChat username saved!**\n"
            f"**1) Add the following code to your VRChat bio now:**\n"
            f"```\n{verification_code}\n```\n"
            f"**2) Once you've updated your bio, press 'Verify' below (within 5 minutes).**",
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
            "🔎 Verification request sent! We'll DM you once we finish checking your VRChat profile.",
            ephemeral=True
        )

# -------------------------------------------------------------------
# Slash Command: /vrcverify
# -------------------------------------------------------------------
@bot.tree.command(name="vrcverify", description="Verify your VRChat 18+ status")
async def vrcverify(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)

    with session_scope() as session:
        # Check server config
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server or not server.role_id:
            await interaction.response.send_message(
                "⚠️ This server hasn't set up a verification role yet. Please contact an admin.",
                ephemeral=True
            )
            return

        # Check if user is in the DB
        user = session.query(User).filter_by(discord_id=user_id).first()

        # CASE A: user is in DB and verified
        if user and user.verification_status:
            await interaction.response.defer(ephemeral=True)
            await assign_role(user_id, True, guild_id)
            await interaction.followup.send("✅ You’re already verified! Role assigned (or re-assigned).", ephemeral=True)
            return

        # CASE B: user is in DB but NOT verified => do a "no-code" re-check
        if user and not user.verification_status:
            await interaction.response.defer(ephemeral=True)
            vrc_user_id = user.vrc_user_id or ""
            # Publish a "no-code" request => checks if they've gained 18+ status
            await publish_to_vrc_checker(
                discord_id=user_id,
                vrc_user_id=vrc_user_id,
                guild_id=guild_id,
                code=None
            )
            await interaction.followup.send(
                "🔎 We’re re-checking your VRChat 18+ status. If you’ve updated your VRChat age verification, "
                "you’ll get a DM soon!",
                ephemeral=True
            )
            return

        # CASE C: user is not in DB => show the VRChat Username modal
        await interaction.response.send_modal(VRCUsernameModal(interaction))

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
    subscription_link = "https://www.example.com/subscribe"

    # Example ephemeral message
    await interaction.response.send_message(
        f"Here’s the link to manage subscriptions or purchase premium:\n{subscription_link}",
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
    await interaction.response.send_message(
        "Need help with verification?\n"
        "- Contact a server admin for assistance\n"
        "- Or visit our support page at https://www.example.com\n\n"
        "If this is an error, please let us know!",
        ephemeral=True
    )

# -------------------------------------------------------------------
# Slash Command: /vrcverify_instructions
# -------------------------------------------------------------------
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="vrcverify_instructions", description="Admin only: Post instructions for using the verification bot.")
async def vrcverify_instructions(interaction: discord.Interaction):
    """
    Admin command that posts an embed telling users how to use the bot.
    The embed includes a code block with an example command usage.
    """
    # Example embed text
    embed = Embed(
        title="How to Use the VRChat Verification Bot",
        description=(
            "**Follow these steps** to verify your 18+ status:\n\n"
            "1. Type `/vrcverify` or click the **Begin Verification** button (if shown)\n"
            "2. If you're new, you'll be asked for your VRChat username\n"
            "3. The bot will give you a unique code – put this in your VRChat bio\n"
            "4. Press **Verify** in Discord once your bio is updated\n\n"
            "If you need additional help, contact an admin or type `/vrcverify_support`."
        ),
        color=discord.Color.blue()
    )

    # Optionally show a code block with example usage
    # (Discord code blocks use triple backticks)
    usage_example = (
        "**Example Usage**:\n"
        "```bash\n"
        "/vrcverify\n"
        "```"
    )
    embed.add_field(name="Example Command", value=usage_example, inline=False)

    await interaction.response.send_message(embed=embed)

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
    try:
        """
        Called when vrc_online_checker returns a result.
        Distinguishes code-based flow vs. no-code re-check.
        If code-based => we also check data["code_found"].
        """
        logger.info(f"🔎 Received verification result: {data}")
        discord_id = data.get("discordID")
        guild_id = data.get("guildID")
        is_18_plus = data.get("is_18_plus", False)
        vrc_user_id = data.get("vrcUserID")
        verification_code = data.get("verificationCode")  # None => re-check
        code_found = data.get("code_found", False)        # Only relevant if verification_code != None

        # 1) If verification_code is None => "re-check" flow
        if verification_code is None:
            with session_scope() as session:
                user = session.query(User).filter_by(discord_id=discord_id).first()
                if not user:
                    logger.warning(f"⚠️ No user row found for discord_id={discord_id} in re-check flow.")
                    return
                # Update user verification status
                user.verification_status = bool(is_18_plus)
                if vrc_user_id:
                    user.vrc_user_id = vrc_user_id

            # Now call assign_role with no code
            await assign_role(discord_id, is_18_plus, guild_id, verification_code=None)
            return

        # 2) Otherwise => code-based flow
        now_utc = datetime.now(timezone.utc)
        with session_scope() as session:
            query = session.query(PendingVerification).filter_by(discord_id=discord_id, guild_id=guild_id)
            query = query.filter_by(verification_code=verification_code)
            pending = query.first()

            if not pending:
                logger.warning(
                    f"⚠️ No matching pending verification found for user={discord_id}, "
                    f"guild={guild_id}, code={verification_code}."
                )
                return

            # Check if expired
            if now_utc > pending.expires_at:
                logger.warning(f"⚠️ Code expired for user={discord_id} in guild={guild_id}. Removing pending row.")
                session.delete(pending)
                return

            # If code was NOT found in the bio, do NOT create the user
            if not code_found:
                logger.info(f"❌ The code was NOT found in VRChat bio for user={discord_id}. Removing pending row.")
                session.delete(pending)
                # Optionally DM them about missing code:
                guild = bot.get_guild(int(guild_id))
                if guild:
                    member = guild.get_member(int(discord_id))
                    if member:
                        try:
                            await member.send(
                                "❌ We could not find your verification code in your VRChat bio. Please try again."
                            )
                        except discord.Forbidden:
                            logger.warning("⚠️ Could not DM user (missing code scenario).")
                return

            # -- NEW SECTION: Check if this VRChat ID is already used by another Discord user --
            existing_with_same_vrc = session.query(User).filter_by(vrc_user_id=vrc_user_id).first()
            if existing_with_same_vrc and (existing_with_same_vrc.discord_id != discord_id):
                # It's already claimed by a different discord user => remove pending + DM user
                logger.warning(
                    f"VRChat ID {vrc_user_id} is already associated with discord_id={existing_with_same_vrc.discord_id}."
                )
                session.delete(pending)

                guild = bot.get_guild(int(guild_id))
                if guild:
                    member = guild.get_member(int(discord_id))
                    if member:
                        try:
                            await member.send(
                                "❌ That VRChat account is already associated with another Discord user. "
                                "If this is a mistake, please contact support or an admin."
                            )
                        except discord.Forbidden:
                            logger.warning("⚠️ Could not DM user about the duplicate VRChat ID.")

                return

            # If code_found=True => code is in the bio, and VRChat ID is not claimed by someone else
            user = session.query(User).filter_by(discord_id=discord_id).first()
            if not user:
                user = User(discord_id=discord_id)
                session.add(user)

            # Attempt to set VRChat ID => might fail if there's a unique constraint (but we just checked, so safe)
            user.vrc_user_id = vrc_user_id
            user.verification_status = bool(is_18_plus)

            # Remove pending
            session.delete(pending)

        # Finally, assign role (or skip) based on is_18_plus
        await assign_role(discord_id, is_18_plus, guild_id, verification_code)

    except Exception as e:
        logger.error("❌ Exception in handle_verification_result", exc_info=True)

# -------------------------------------------------------------------
# Bot Events
# -------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Bot is ready. Logged in as {bot.user} (ID: {bot.user.id})")
    bot.loop.create_task(consume_results_queue())

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == '__main__':
    bot.run(DISCORD_BOT_TOKEN)
 