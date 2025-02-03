import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
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

# Database setup
engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

# Link to vrc_online_service.py
VRC_MICROSERVICE_URL = "http://localhost:5532/check_vrc_18plus"
VRCHAT_LOGIN_URL = "https://vrchat.com/home/login"

@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session  # Provide session to caller
        session.commit()  # Commit changes if successful
    except:
        session.rollback()  # Rollback on error
        raise
    finally:
        session.close()  # Always close session

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
    verification_status = Column(Boolean, default=False)  # Represents 18+ status
    vrc_user_id = Column(String(50), unique=True, nullable=False)
    last_verification_attempt = Column(DateTime(timezone=True))

Base.metadata.create_all(engine)

# Initialize the Discord bot
intents = discord.Intents.default()
intents.members = True  # Required for role assignment

class VRCVerifyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = VRCVerifyBot()

# Function to check VRChat age verification
def check_vrc_age_verification(vrc_user_id):
    url = f"https://vrchat.com/home/user/{vrc_user_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        return None  # Failed to fetch profile page
    
    soup = BeautifulSoup(response.text, "html.parser")
    badge = soup.find("div", class_="tw-bg-user-age-18+ tw-text-white tw-rounded-lg")
    
    return badge is not None

# Slash command to set up the verification role (Admin Only)
@bot.tree.command(name="setup_vrc", description="Set the role for verified users")
@app_commands.describe(role="The role to assign to verified users")
async def setup_vrc(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    
    with session_scope() as session:
        server_config = session.query(Server).filter_by(server_id=guild_id).first()

        if not server_config:
            new_server = Server(
                server_id=guild_id,
                owner_id=str(interaction.guild.owner_id),
                role_id=str(role.id),
                subscription_status=False  # Default to inactive
            )
            session.add(new_server)
        else:
            server_config.role_id = str(role.id)

    await interaction.response.send_message(f"✅ Verification role set to: **{role.name}**", ephemeral=True)

# Slash command to verify a user
@bot.tree.command(name="vrcverify", description="Verify your VRChat 18+ status")
async def vrcverify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    guild_id = str(interaction.guild.id)

    with session_scope() as session:
        user = session.query(User).filter_by(discord_id=user_id).first()

        if not user:
            await interaction.followup.send("❌ You are not in the database. Please sign in via VRChat first.", ephemeral=True)
            return

        # Send the VRChat User ID to the microservice
        response = requests.post(VRC_MICROSERVICE_URL, json={"vrcUserID": user.vrc_user_id})

        if response.status_code != 200:
            await interaction.followup.send("❌ Failed to check VRChat verification status.", ephemeral=True)
            return

        result = response.json()
        is_18_plus = result.get("is_18_plus", False)

        # If verified, assign the role
        if is_18_plus:
            server = session.query(Server).filter_by(server_id=guild_id).first()
            role = discord.utils.get(interaction.guild.roles, id=int(server.role_id)) if server else None
            if role:
                await interaction.user.add_roles(role)
                await interaction.followup.send("✅ You are verified! Role assigned.", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ No verification role set.", ephemeral=True)
        else:
            await interaction.followup.send("❌ You are not verified on VRChat.", ephemeral=True)

@bot.event
async def on_ready():
    logger.info(f'Bot is ready. Logged in as {bot.user}')
    try:
        synced = await bot.tree.sync()
        logger.debug(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

bot.run(DISCORD_BOT_TOKEN)
