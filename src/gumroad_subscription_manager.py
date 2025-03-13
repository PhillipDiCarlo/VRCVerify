import os
import threading
import logging
from flask import Flask, request, jsonify
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Set up the Gumroad database engine and session
DATABASE_URL_GUMROAD = os.getenv('DATABASE_URL')
engine_gumroad = create_engine(DATABASE_URL_GUMROAD)
GumroadBase = declarative_base()
SessionGumroad = sessionmaker(bind=engine_gumroad)

# Define the Gumroad subscription table
class GumroadSubscription(GumroadBase):
    __tablename__ = 'gumroad_subscriptions'
    id = Column(Integer, primary_key=True)
    subscription_id = Column(String(255), unique=True, nullable=False)
    discord_server_id = Column(String(30), nullable=False)
    email = Column(String(255), nullable=True)
    subscription_status = Column(Boolean, default=False)
    subscription_start_date = Column(DateTime(timezone=True), nullable=True)
    last_renewal_date = Column(DateTime(timezone=True), nullable=True)

# Create the table (or run migrations in production)
GumroadBase.metadata.create_all(engine_gumroad)

# Context manager for database sessions
@contextmanager
def session_scope(service_type):
    if service_type == 'gumroad':
        Session = SessionGumroad
    else:
        raise ValueError(f"Unknown service_type: {service_type}")
    session = Session()
    try:
        yield session
        session.commit()
        logging.info("Transaction committed.")
    except Exception as e:
        session.rollback()
        logging.error(f"Error during session scope: {e}")
        raise
    finally:
        session.close()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Endpoint to receive Gumroad webhooks
@app.route('/gumroad-webhook', methods=['POST'])
def gumroad_webhook():
    payload = request.get_json()
    logging.info(f"Received Gumroad webhook: {payload}")
    # Process the event asynchronously
    thread = threading.Thread(target=process_gumroad_event, args=(payload,))
    thread.start()
    return jsonify({'status': 'success'}), 200

def process_gumroad_event(payload):
    try:
        alert_name = payload.get('alert_name')
        if alert_name == "subscription_created":
            handle_gumroad_subscription_created(payload)
        elif alert_name == "subscription_renewed":
            handle_gumroad_subscription_renewed(payload)
        elif alert_name == "subscription_cancelled":
            handle_gumroad_subscription_cancelled(payload)
        else:
            logging.info(f"Unhandled Gumroad alert: {alert_name}")
    except Exception as e:
        logging.error(f"Error processing Gumroad event: {e}")

def handle_gumroad_subscription_created(payload):
    """
    Handles the event when a new Gumroad subscription is created.
    Expects the payload to include:
      - subscription_id: Unique ID from Gumroad
      - email: Buyer’s email address
      - purchase_date: Unix timestamp (as a string) of the purchase
      - custom_fields: A dict containing the key 'discord_server_id'
    """
    logging.info("Handling Gumroad subscription created event")
    subscription_id = payload.get('subscription_id')
    email = payload.get('email')
    purchase_date_str = payload.get('purchase_date')
    try:
        purchase_timestamp = int(purchase_date_str) if purchase_date_str else None
        purchase_date = datetime.fromtimestamp(purchase_timestamp, tz=timezone.utc) if purchase_timestamp else datetime.now(timezone.utc)
    except Exception:
        purchase_date = datetime.now(timezone.utc)

    # Extract discord server id from custom fields (assumes a key 'discord_server_id')
    custom_fields = payload.get('custom_fields', {})
    discord_server_id = custom_fields.get('discord_server_id')

    if not subscription_id or not discord_server_id:
        logging.error("Missing subscription_id or discord_server_id in payload")
        return

    try:
        with session_scope('gumroad') as db_session:
            sub = db_session.query(GumroadSubscription).filter_by(subscription_id=subscription_id).first()
            if sub:
                # Update an existing subscription record
                sub.subscription_status = True
                sub.last_renewal_date = purchase_date
                logging.info(f"Updated existing Gumroad subscription {subscription_id}")
            else:
                # Create a new subscription record
                new_sub = GumroadSubscription(
                    subscription_id=subscription_id,
                    discord_server_id=discord_server_id,
                    email=email,
                    subscription_status=True,
                    subscription_start_date=purchase_date,
                    last_renewal_date=purchase_date
                )
                db_session.add(new_sub)
                logging.info(f"Created new Gumroad subscription {subscription_id}")
    except Exception as e:
        logging.error(f"Error handling subscription created: {e}")

def handle_gumroad_subscription_renewed(payload):
    """
    Handles subscription renewals.
    Expects payload with:
      - subscription_id
      - purchase_date (as Unix timestamp string)
    """
    logging.info("Handling Gumroad subscription renewed event")
    subscription_id = payload.get('subscription_id')
    purchase_date_str = payload.get('purchase_date')
    try:
        purchase_timestamp = int(purchase_date_str) if purchase_date_str else None
        renewal_date = datetime.fromtimestamp(purchase_timestamp, tz=timezone.utc) if purchase_timestamp else datetime.now(timezone.utc)
    except Exception:
        renewal_date = datetime.now(timezone.utc)

    if not subscription_id:
        logging.error("Missing subscription_id in renewal payload")
        return

    try:
        with session_scope('gumroad') as db_session:
            sub = db_session.query(GumroadSubscription).filter_by(subscription_id=subscription_id).first()
            if sub:
                sub.subscription_status = True
                sub.last_renewal_date = renewal_date
                logging.info(f"Renewed Gumroad subscription {subscription_id}")
            else:
                logging.error(f"Gumroad subscription {subscription_id} not found for renewal")
    except Exception as e:
        logging.error(f"Error handling subscription renewed: {e}")

def handle_gumroad_subscription_cancelled(payload):
    """
    Handles subscription cancellation events.
    Expects payload with:
      - subscription_id
    """
    logging.info("Handling Gumroad subscription cancelled event")
    subscription_id = payload.get('subscription_id')
    if not subscription_id:
        logging.error("Missing subscription_id in cancellation payload")
        return

    try:
        with session_scope('gumroad') as db_session:
            sub = db_session.query(GumroadSubscription).filter_by(subscription_id=subscription_id).first()
            if sub:
                sub.subscription_status = False
                logging.info(f"Cancelled Gumroad subscription {subscription_id}")
            else:
                logging.error(f"Gumroad subscription {subscription_id} not found for cancellation")
    except Exception as e:
        logging.error(f"Error handling subscription cancellation: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5435)
