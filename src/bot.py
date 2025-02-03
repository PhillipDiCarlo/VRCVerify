import os
import logging
import discord
import pika
import json
import random
import string
import asyncio
from discord import app_commands
from discord.ext import commands
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
from dotenv import load_dotenv
from datetime import datetime, timezone

# Load environment variables
load_dotenv()

# Set up logging
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Retrieve environment variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

# RabbitMQ Configuration
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT"))
RABBITMQ_USERNAME = os.getenv("RABBITMQ_USERNAME")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST")
RABBITMQ_QUEUE_NAME = os.getenv("RABBITMQ_QUEUE_NAME")

# RabbitMQ setup
credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST,
    port=RABBITMQ_PORT,
    virtual_host=RABBITMQ_VHOST,
    credentials=credentials
)

# Database setup
engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

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

# Define database models
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
    discord_id = Column(String(30), nullable=False)
    verification_status = Column(Boolean, default=False)
    vrc_user_id = Column(String(50), unique=True, nullable=True)
    last_verification_attempt = Column(DateTime(timezone=True))

Base.metadata.create_all(engine)

# Initialize the Discord bot
intents = discord.Intents.default()
intents.members = True

class VRCVerifyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = VRCVerifyBot()

def generate_verification_code():
    """Generates a unique verification code."""
    return "VRC-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def send_verification_request(discord_id, vrc_user_id, verification_code):
    """Sends a verification request to RabbitMQ."""
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=RABBITMQ_QUEUE_NAME, durable=True)

    message = {
        "discordID": discord_id,
        "vrcUserID": vrc_user_id,
        "verificationCode": verification_code
    }

    channel.basic_publish(
        exchange="",
        routing_key=RABBITMQ_QUEUE_NAME,
        body=json.dumps(message)
    )

    connection.close()
    print(f"📤 Sent verification request for {discord_id}")

async def assign_role(discord_id, is_18_plus):
    """Handles role assignment asynchronously and looks up role in the database every time."""
    with session_scope() as session:
        for guild in bot.guilds:
            member = guild.get_member(int(discord_id))
            server = session.query(Server).filter_by(server_id=str(guild.id)).first()
            
            if server:
                role = discord.utils.get(guild.roles, id=int(server.role_id))  # Look up role each time
            
                if is_18_plus and member is not None and role is not None:
                    try:
                        await member.add_roles(role)
                        print(f"✅ Assigned role to {discord_id}")
                        await member.send(f"✅ You have been verified and assigned the role **{role.name}**!", delete_after=10)
                    except discord.Forbidden:
                        print(f"⚠️ Bot does not have permission to assign role {role.name} to {discord_id}")
                    except Exception as e:
                        print(f"⚠️ Unexpected error assigning role: {e}")

                elif not is_18_plus and member is not None:
                    try:
                        await member.send("❌ You are not verified as 18+ on VRChat.", delete_after=10)
                    except Exception:
                        print(f"⚠️ Could not send message to {discord_id}")

async def listen_for_verification_results():
    """Listens for verification results from RabbitMQ asynchronously."""
    try:
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue="vrc_verification_results", durable=True)

        message_queue = asyncio.Queue()

        def callback(ch, method, properties, body):
            """Handles incoming RabbitMQ messages."""
            asyncio.create_task(message_queue.put(json.loads(body)))

        channel.basic_consume(queue="vrc_verification_results", on_message_callback=callback, auto_ack=True)

        print("✅ Listening for verification results from RabbitMQ...")

        while True:
            result = await message_queue.get()
            discord_id = result["discordID"]
            vrc_user_id = result["vrcUserID"]
            is_18_plus = result["is_18_plus"]

            print(f"🔎 Received verification result for {discord_id}: {is_18_plus}")

            with session_scope() as session:
                user = session.query(User).filter_by(discord_id=discord_id).first()
                if user:
                    user.vrc_user_id = vrc_user_id
                    user.verification_status = is_18_plus

            bot.loop.create_task(assign_role(discord_id, is_18_plus))

    except pika.exceptions.AMQPConnectionError:
        print("❌ RabbitMQ connection failed! Retrying in 5 seconds...")
        await asyncio.sleep(5)
        bot.loop.create_task(listen_for_verification_results())

class VRCUsernameModal(discord.ui.Modal, title="Enter Your VRChat Username"):
    vrc_username = discord.ui.TextInput(label="VRChat Username", placeholder="Enter your VRChat username")

    def __init__(self, interaction: discord.Interaction):
        super().__init__()
        self.interaction = interaction

    async def on_submit(self, interaction: discord.Interaction):
        vrc_username = self.vrc_username.value.strip()
        discord_id = str(interaction.user.id)

        verification_code = generate_verification_code()

        with session_scope() as session:
            user = session.query(User).filter_by(discord_id=discord_id).first()
            if not user:
                user = User(discord_id=discord_id, vrc_user_id=vrc_username, verification_status=False)
                session.add(user)
            else:
                user.vrc_user_id = vrc_username

        view = VRCVerificationButton(vrc_username, verification_code)
        await interaction.response.send_message(
            f"✅ **VRChat account linked!**\n"
            f"**Add the following code to your VRChat bio:**\n"
            f"```\n{verification_code}\n```\n"
            f"Once added, press the 'Verify' button below.",
            view=view,
            ephemeral=True
        )

class VRCVerificationButton(discord.ui.View):
    def __init__(self, vrc_username, verification_code):
        super().__init__()
        self.vrc_username = vrc_username
        self.verification_code = verification_code

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        send_verification_request(discord_id, self.vrc_username, self.verification_code)
        await interaction.response.send_message("🔎 Verification request sent! Please wait...", ephemeral=True)

@bot.tree.command(name="vrcverify", description="Verify your VRChat 18+ status")
async def vrcverify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)

    with session_scope() as session:
        user = session.query(User).filter_by(discord_id=user_id).first()

        if not user or not user.vrc_user_id:
            await interaction.response.send_modal(VRCUsernameModal(interaction))
            return

        verification_code = generate_verification_code()
        send_verification_request(user_id, user.vrc_user_id, verification_code)

        await interaction.followup.send("🔎 Verification request sent. Please wait...", ephemeral=True)

@bot.event
async def on_ready():
    await bot.wait_until_ready()
    logger.info(f'Bot is ready. Logged in as {bot.user}')
    
    try:
        synced = await bot.tree.sync()
        logger.debug(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

    bot.loop.create_task(listen_for_verification_results())

bot.run(DISCORD_BOT_TOKEN)
