import os
from dotenv import load_dotenv

load_dotenv()


def get_bot_token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")
    return t


def get_user_id() -> int:
    uid = os.environ.get("TELEGRAM_USER_ID", "").strip()
    if not uid:
        raise ValueError("TELEGRAM_USER_ID is not set in .env")
    return int(uid)
