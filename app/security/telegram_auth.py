from fastapi import HTTPException, Request, Depends
from typing import Optional
from urllib.parse import parse_qs
import json
import time
import hmac
import hashlib
from app.config import TELEGRAM_BOT_TOKEN, INIT_DATA_MAX_AGE, DEV_MODE, ADMIN_USERNAMES
from app.db import blocked_col
from app.models import TelegramUser

def validate_telegram_init_data(init_data: str) -> dict:
    parsed = parse_qs(init_data, keep_blank_values=True)
    raw_user = parsed.get("user", [None])[0]
    if not raw_user:
        raise HTTPException(401, "No user in initData")

    try:
        user_data = json.loads(raw_user)
    except Exception:
        raise HTTPException(401, "Invalid user payload")

    if not TELEGRAM_BOT_TOKEN:
        if not DEV_MODE:
            raise HTTPException(500, "Server configuration error: TELEGRAM_BOT_TOKEN is not set")
        print("⚠️  DEV MODE: Telegram signature NOT verified (set TELEGRAM_BOT_TOKEN for production)")
        return user_data

    received_hash = parsed.get("hash", [None])[0]
    if not received_hash:
        raise HTTPException(401, "Missing hash in initData")

    check_pairs = []
    for key in sorted(parsed.keys()):
        if key == "hash":
            continue
        check_pairs.append(f"{key}={parsed[key][0]}")
    data_check_string = "\n".join(check_pairs)

    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(401, "Invalid Telegram signature")

    auth_date = int(parsed.get("auth_date", ["0"])[0])
    if INIT_DATA_MAX_AGE > 0 and auth_date > 0:
        age = int(time.time()) - auth_date
        if age > INIT_DATA_MAX_AGE:
            raise HTTPException(401, "initData expired")

    return user_data

async def get_current_user(request: Request) -> TelegramUser:
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()

    if init_data:
        tg_user = validate_telegram_init_data(init_data)
        user_id = tg_user.get("id", 0)
        username = (tg_user.get("username") or "").strip().lower()
        first_name = (tg_user.get("first_name") or "").strip()
    elif not TELEGRAM_BOT_TOKEN and DEV_MODE:
        dev_name = request.headers.get("X-Dev-Username", "").strip()
        if not dev_name:
            dev_name = request.query_params.get("username", "guest")
        clean = dev_name.replace("@", "").strip().lower()
        username = clean or "guest"
        first_name = dev_name
        user_id = int(hashlib.sha256(username.encode("utf-8")).hexdigest()[:15], 16) % 10**9
        print(f"⚠️  DEV MODE user: {username}")
    else:
        raise HTTPException(401, "Authorization required: open from Telegram")

    is_admin = username in ADMIN_USERNAMES

    return TelegramUser(
        user_id=user_id,
        username=username,
        first_name=first_name,
        is_admin=is_admin,
    )

async def get_optional_user(request: Request) -> Optional[TelegramUser]:
    try:
        return await get_current_user(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise

async def require_admin(user: TelegramUser = Depends(get_current_user)) -> TelegramUser:
    if not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user

async def check_not_blocked(user: TelegramUser = Depends(get_current_user)) -> TelegramUser:
    if user.username:
        blocked = await blocked_col.find_one({"username": user.username})
        if blocked and blocked.get("blocked"):
            raise HTTPException(403, "You are blocked from this platform")
    return user
