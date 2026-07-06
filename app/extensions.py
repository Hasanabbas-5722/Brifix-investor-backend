from flask_pymongo import PyMongo
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
import os
from app.utils.logger import get_logger
from growwapi import GrowwAPI
import pyotp
from datetime import datetime, time as dtime

logger = get_logger(__name__)

# ── Store in env vars or config, never hardcode ──────────────
GROWW_TOTP_TOKEN  = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjcwNTI1OTUsImlhdCI6MTc3ODY1MjU5NSwibmJmIjoxNzc4NjUyNTk1LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCIyMTY3ZDNmNi1jZWZhLTQ2NDYtOGE3MS1mYjFhY2RhMmZmNWJcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMDdhZGU3YjYtMzg5ZS00MTMzLTlkODctMGJiNzBhZmNlZWJhXCIsXCJkZXZpY2VJZFwiOlwiN2E1NWNmNTMtMTBmOS01OTg1LTgyZjItODFmMTlmMWEwOTU1XCIsXCJzZXNzaW9uSWRcIjpcIjBlOTFlODg3LTAxNGItNDQzNC1hMGIwLTMzMmY0ZTZiY2M5NlwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYkVXL085NUlhQ3VGSXdRS1J1c01xa0ZSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIxMDMuMjM4LjE0LjI0NywxNzIuNjkuODYuMzIsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjcwNTI1OTU1NzAsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.KkaHbXnrk5FBNzeJU9Wi9CJXBmp10iN8T5n9mhzb12xwrIccnlcpDyHpl9oVeonoWzssE-5GJLdA7aWKxeHelQ"   # from portal — long static token
GROWW_TOTP_SECRET = "G67O7CC56XI4WCLP4HWMF5H3JKMOEBEO"  # base32 string e.g. JBSWY3DPEHPK3PXP

# ── Session cache ────────────────────────────────────────────
_groww_session = {
    "client": None,
    "access_token": None,
    "created_at": None,   # datetime of last login
}


# grow api key = eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjY5ODk5MDEsImlhdCI6MTc3ODU4OTkwMSwibmJmIjoxNzc4NTg5OTAxLCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJlZmUwNzBhOC04MDE0LTRjZGItOTdkMC0yM2E5ODVjZmY3OGNcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMDdhZGU3YjYtMzg5ZS00MTMzLTlkODctMGJiNzBhZmNlZWJhXCIsXCJkZXZpY2VJZFwiOlwiN2E1NWNmNTMtMTBmOS01OTg1LTgyZjItODFmMTlmMWEwOTU1XCIsXCJzZXNzaW9uSWRcIjpcIjIyNzQ0N2UwLTA3NGEtNDEyMi05OGYyLTU0ZTA1ZjQ1YjEwM1wiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYkVXL085NUlhQ3VGSXdRS1J1c01xa0ZSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIxMDMuMjM4LjE0LjI0NywxNjIuMTU4LjIzNS4xNzcsMzUuMjQxLjIzLjEyM1wiLFwidHdvRmFFeHBpcnlUc1wiOjI1NjY5ODk5MDE1MjAsXCJ2ZW5kb3JOYW1lXCI6XCJncm93d0FwaVwifSIsImlzcyI6ImFwZXgtYXV0aC1wcm9kLWFwcCJ9.y2sYdQv1LadI9Vd3m0aKAkO8kwfha7-qz5q0chzKWnLqCNx1o9bdbWro8NywTklp8DH1XdVwMbSyv-WcjsJY-Q
# grow secret= 5dnR6E1nv$7EVoBtXN5nM&LazTUJ5Ypa

mongo = PyMongo()
client = None

def connect_to_mongodb():
    """Establish a standalone MongoDB connection."""
    global client
    mongo_uri = os.environ.get("MONGO_URI", "mongodb+srv://brifixinvestor:donsaale5722@brifix-investor.g7snl.mongodb.net/")
    logger.info(f"client ==> {str(client)}")
    try:
        if client is not None:
            logger.info("✓ MongoDB connection already established")
            return client

        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        logger.info("✓ MongoDB connection established")

        client = client["brifix-investor"]

        return client
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.info(f"✗ Failed to connect to MongoDB: {e}")
        return None

def _is_session_expired() -> bool:
    """Session expires at 6 AM IST every day."""
    if _groww_session["created_at"] is None:
        return True

    now = datetime.now()
    created = _groww_session["created_at"]

    # If created before today's 6 AM and it's now past 6 AM → expired
    six_am_today = now.replace(hour=6, minute=0, second=0, microsecond=0)

    if created < six_am_today and now >= six_am_today:
        return True

    # If created on a previous day entirely → expired
    if created.date() < now.date() and now.time() >= dtime(6, 0):
        return True

    return False


def get_groww_client(force_new: bool = False) -> GrowwAPI:
    """
    Returns a valid GrowwAPI client.
    Auto re-logins if session expired (post 6 AM reset).
    """
    logger.info(f"Check grow seession  new ... {_groww_session}")
    logger.info(f"Check force new ... {force_new}")
    logger.info(f"Check session new ... {_is_session_expired()}")
    logger.info(f"Check client new ... {_groww_session['client']}")
    logger.info(f"Check connection new ... {not force_new and not _is_session_expired() and _groww_session['client']}")

    if not force_new and not _is_session_expired() and _groww_session["client"]:
        logger.info("Returning cached Groww session")
        return _groww_session["client"]

    logger.info("Creating new Groww session via TOTP...")
    try:
        # Fresh TOTP code — valid for 30 seconds, pyotp handles timing
        totp = pyotp.TOTP(GROWW_TOTP_SECRET).now()
        logger.info(f"TOTP generated: {totp}")

        access_token = GrowwAPI.get_access_token(
            api_key=GROWW_TOTP_TOKEN,
            totp=totp
        )
        logger.info(f"Access token obtained: {access_token[:30]}...")

        groww = GrowwAPI(access_token)

        # Cache it
        _groww_session["client"]       = groww
        _groww_session["access_token"] = access_token
        _groww_session["created_at"]   = datetime.now()

        logger.info(f"Groww session created and cached successfully:: {_groww_session}")
        return groww

    except Exception as e:
        logger.error(f"Groww connection failed: {e}")
        raise


# ── Usage anywhere in your project ──────────────────────────
# groww = get_groww_client()
# orders = groww.get_order_list()
    

def test_connection():
    """Test MongoDB connection from command line"""
    from app import create_app
    from app.utils import mongo_utils

    app = create_app()
    with app.app_context():
        try:
            db = mongo_utils.get_db()
            # Perform a simple operation to verify connection
            collections = db.list_collection_names()
            print(f"✓ MongoDB connection successful")
            print(f"  Database: {db.name}")
            print(f"  Collections: {collections}")
            return True
        except Exception as e:
            print(f"✗ MongoDB connection failed: {e}")
            return False

def get_connection_info():
    """Get MongoDB connection information"""
    from app import create_app
    from app.utils import mongo_utils

    app = create_app()
    with app.app_context():
        try:
            client = mongo_utils.get_mongo_client()
            server_info = client.server_info()
            db_info = {
                "version": server_info.get("version"),
                "database": mongo_utils.get_db().name,
                "connected": True
            }
            return db_info
        except Exception as e:
            return {"connected": False, "error": str(e)}

if __name__ == "__main__":
    test_connection()
