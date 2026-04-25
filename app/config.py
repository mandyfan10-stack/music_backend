import os

MONGO_URL = os.getenv("MONGO_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
ENV = os.getenv("ENV", "development")

ADMIN_USERNAMES = set()

def validate_settings():
    global ADMIN_USERNAMES, DEV_MODE
    if not MONGO_URL:
        raise RuntimeError("MONGO_URL not set")

    if ENV == "production":
        if DEV_MODE:
            raise RuntimeError("DEV_MODE cannot be true when ENV is production")
        if not TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required in production")

    admin_users_str = os.getenv("ADMIN_USERNAMES")
    if not admin_users_str:
        if ENV == "production":
            raise RuntimeError("ADMIN_USERNAMES must be set in production")
        else:
            admin_users_str = ""

    ADMIN_USERNAMES = set(
        u.strip().lower()
        for u in admin_users_str.split(",")
        if u.strip()
    )

validate_settings()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL_PRIMARY = os.getenv("GROQ_MODEL_PRIMARY", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACKS = [
    m.strip() for m in os.getenv("GROQ_MODEL_FALLBACKS", "llama-3.1-8b-instant").split(",") if m.strip()
]
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "2"))
