import os
import json
import asyncio
import logging
import random
import string

import discord
from discord import app_commands
from discord.ext import commands

import pika
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from contextlib import contextmanager
from datetime import datetime, timezone
from dotenv import load_dotenv

# -------------------------------------------------------------------
# Load environment variables
# -------------------------------------------------------------------
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")
RABBITMQ_QUEUE_NAME = os.getenv("RABBITMQ_QUEUE_NAME")

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
# Models
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
intents.members = True

class VRCVerifyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = VRCVerifyBot()

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def generate_verification_code() -> str:
    return "VRC-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def send_verification_request_no_code(discord_id: str, vrc_user_id: str, guild_id: str):
    """Publishes a re-check (no code) request to RabbitMQ."""
    def _publish():
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)

        message = {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "verificationCode": None  # indicates a re-check, no code needed
        }
        channel.basic_publish(
            exchange="",
            routing_key=RABBITMQ_QUEUE_NAME,
            body=json.dumps(message)
        )
        connection.close()
        logger.info(f"📤 Sent re-check request for user {discord_id} in guild {guild_id}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _publish)

async def send_verification_request_with_code(discord_id: str, vrc_user_id: str, code: str, guild_id: str):
    """Publishes a normal verification request (with a new code) to RabbitMQ."""
    def _publish():
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)

        message = {
            "discordID": discord_id,
            "vrcUserID": vrc_user_id,
            "guildID": guild_id,
            "verificationCode": code
        }
        channel.basic_publish(
            exchange="",
            routing_key=RABBITMQ_QUEUE_NAME,
            body=json.dumps(message)
        )
        connection.close()
        logger.info(f"📤 Sent verification request w/ code for user {discord_id} in guild {guild_id}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _publish)

async def assign_role(discord_id: str, is_18_plus: bool, guild_id: str):
    """Assign or skip role in exactly one guild. Also DM the user about success/failure."""
    # 1) Reopen a session to fetch the Server data
    with session_scope() as session:
        server = session.query(Server).filter_by(server_id=guild_id).first()
        # Copy what we need to local variables
        if server:
            role_id = server.role_id
        else:
            role_id = None

    # 2) Now the DB session is closed, but we have local copies of what we need
    guild = bot.get_guild(int(guild_id))
    if not guild:
        print(f"⚠️ Could not find guild {guild_id}. Stopping role assignment.")
        return

    member = guild.get_member(int(discord_id))
    if not member:
        print(f"⚠️ User {discord_id} not found in guild {guild_id}.")
        return

    if not role_id:
        print(f"⚠️ No role_id configured for guild {guild_id}.")
        return

    role = discord.utils.get(guild.roles, id=int(role_id))
    if not role:
        print(f"⚠️ Role {role_id} not found in guild {guild_id}.")
        return

    if is_18_plus:
        try:
            await member.add_roles(role)
            print(f"✅ Assigned {role.name} to {member} in guild {guild.name}")
            # DM user on success
            try:
                await member.send(
                    f"✅ You have been verified as 18+ and assigned the role **{role.name}** in **{guild.name}**!"
                )
            except discord.Forbidden:
                print("⚠️ Could not DM user; they likely have DMs turned off.")
        except discord.Forbidden:
            print(f"⚠️ Bot lacks permission to assign {role.name} in guild {guild_id} to user {discord_id}")
        except Exception as e:
            print(f"⚠️ Unexpected error assigning {role.name}: {e}")
    else:
        # User is not verified => DM them about failure
        try:
            await member.send("❌ You are not verified as 18+ on VRChat, so we cannot assign the verified role.")
        except discord.Forbidden:
            print("⚠️ Could not DM user; they likely have DMs turned off.")

# -------------------------------------------------------------------
# Modal UI
# -------------------------------------------------------------------
class VRCUsernameModal(discord.ui.Modal, title="Enter Your VRChat Username"):
    vrc_username = discord.ui.TextInput(label="VRChat Username", placeholder="usr_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        vrc_username = self.vrc_username.value.strip()
        discord_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)

        # Generate a new verification code
        verification_code = generate_verification_code()
        with session_scope() as session:
            user = session.query(User).filter_by(discord_id=discord_id).first()
            if not user:
                user = User(
                    discord_id=discord_id,
                    vrc_user_id=vrc_username,
                    verification_status=False
                )
                session.add(user)
            else:
                user.vrc_user_id = vrc_username

        # Provide a Verify button
        view = VRCVerificationButton(vrc_username, verification_code, guild_id)
        await interaction.response.send_message(
            f"✅ **VRChat username saved!**\n"
            f"**1) Add the following code to your VRChat bio:**\n"
            f"```\n{verification_code}\n```\n"
            f"**2) Then press 'Verify' below.**",
            view=view,
            ephemeral=True
        )

class NewUserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # or specify a timeout in seconds

    @discord.ui.button(label="Continue Verification", style=discord.ButtonStyle.green)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1) Option A: Show the VRChat username modal
        await interaction.response.send_modal(VRCUsernameModal(interaction))

        # OR

        # 1) Option B: Directly do a “with code” approach:
        #
        #   code = generate_verification_code()
        #   # Possibly create DB user row?
        #   await send_verification_request_with_code(interaction.user.id, "someVRCID", code, str(interaction.guild.id))
        #   await interaction.response.send_message("Check your DMs soon!", ephemeral=True)


class VRCVerificationButton(discord.ui.View):
    """View that has a 'Verify' button. Clicking it triggers sending the code to the checker."""
    def __init__(self, vrc_username: str, verification_code: str, guild_id: str):
        super().__init__()
        self.vrc_username = vrc_username
        self.verification_code = verification_code
        self.guild_id = guild_id

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        # Send the request w/ code to RabbitMQ
        await send_verification_request_with_code(
            discord_id, self.vrc_username, self.verification_code, self.guild_id
        )

        await interaction.followup.send("🔎 Verification request sent! Check your DMs soon.", ephemeral=True)

# -------------------------------------------------------------------
# Slash Command: /vrcverify
# -------------------------------------------------------------------
@bot.tree.command(name="vrcverify", description="Verify your VRChat 18+ status")
async def vrcverify(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)

    # Keep all logic in one session_scope
    with session_scope() as session:
        # 1) Check the server config
        server = session.query(Server).filter_by(server_id=guild_id).first()
        if not server or not server.role_id:
            await interaction.response.send_message(
                "⚠️ This server hasn't set up a verification role yet. "
                "Please contact an admin to configure the bot.",
                ephemeral=True
            )
            return

        # 2) Check if user is in DB
        user = session.query(User).filter_by(discord_id=user_id).first()

        # CASE A: user is in DB & verified => assign role
        if user and user.verification_status:
            # Defer once for our initial response
            await interaction.response.defer(ephemeral=True)
            # Now call assign_role(...) WITHOUT referencing 'server' or 'user' outside the session
            await assign_role(discord_id=user_id, is_18_plus=True, guild_id=guild_id)
            await interaction.followup.send(
                "✅ You’re already verified! Role assigned (or re-assigned).",
                ephemeral=True
            )
            return

        # CASE B: user in DB but not verified
        if user and user.vrc_user_id:
            await interaction.response.defer(ephemeral=True)
            # Do your re-check or code request
            await send_verification_request_no_code(user_id, user.vrc_user_id, guild_id)
            await interaction.followup.send(
                "🔎 We’re verifying your VRChat 18+ status. Check your DMs soon.",
                ephemeral=True
            )
            return

        # CASE C: user not in DB => show modal, or show a button, etc.
        # Must be the only "first response" if you're showing a modal
        await interaction.response.send_modal(VRCUsernameModal(interaction))
        return


# -------------------------------------------------------------------
# RabbitMQ Consumer (no while True)
# -------------------------------------------------------------------
async def consume_queue():
    """
    Sets up a background thread to call channel.start_consuming().
    We use on_message_callback to handle incoming verification results.
    """
    loop = asyncio.get_running_loop()

    def on_message(ch, method, properties, body):
        """Process each incoming RabbitMQ message in the event loop."""
        data = json.loads(body)
        future = asyncio.run_coroutine_threadsafe(handle_verification_result(data), loop)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def do_blocking_consume():
        """Runs in an executor thread so we don't block the main asyncio loop."""
        try:
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)
            channel.basic_consume(queue=RABBITMQ_QUEUE_NAME, on_message_callback=on_message, auto_ack=False)
            logger.info("✅ Listening for verification results from RabbitMQ...")
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as e:
            logger.error(f"❌ RabbitMQ connection failed: {e}")

    await loop.run_in_executor(None, do_blocking_consume)

async def handle_verification_result(data: dict):
    """
    Called from the background consumer whenever we get a message from 
    vrc_online_checker with is_18_plus, guild_id, etc.
    """
    logger.info(f"🔎 Received verification result: {data}")
    discord_id = data.get("discordID")
    guild_id = data.get("guildID")
    is_18_plus = data.get("is_18_plus", False)

    # Update the DB record
    with session_scope() as session:
        user = session.query(User).filter_by(discord_id=discord_id).first()
        if user:
            user.vrc_user_id = data.get("vrcUserID", user.vrc_user_id)
            # If they've become verified, set verification_status to True
            user.verification_status = bool(is_18_plus)

    # Now assign or skip role in that specific guild
    if discord_id and guild_id:
        await assign_role(discord_id, bool(is_18_plus), guild_id)

# -------------------------------------------------------------------
# Bot Events
# -------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f'Bot is ready. Logged in as {bot.user} (ID: {bot.user.id})')
    # Start consuming in the background
    bot.loop.create_task(consume_queue())

    # Optionally attempt to sync commands on startup
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == '__main__':
    bot.run(DISCORD_BOT_TOKEN)
