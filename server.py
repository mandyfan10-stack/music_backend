from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from pymongo.errors import DuplicateKeyError
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import os
import html
import hmac
import hashlib
import json
import re
import time
import httpx
import ipaddress
import socket
from urllib.parse import parse_qs
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.parse import unquote
from motor.motor_asyncio import AsyncIOMotorClient

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from groq import Groq
except ImportError:
    Groq = None

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mandyfan10-stack.github.io",
        "http://localhost:8888",
        "http://127.0.0.1:8888",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("music_backend")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

# ============================
# КОНФИГУРАЦИЯ (всё через env)
# ============================
MONGO_URL = os.getenv("MONGO_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
ENV = os.getenv("ENV", "development").strip().lower()

ADMIN_USERNAMES = set()


def normalize_username(value: str) -> str:
    return (value or "").strip().lower().replace("@", "")


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

    ADMIN_USERNAMES = {
        normalized
        for normalized in (normalize_username(u) for u in admin_users_str.split(","))
        if normalized
    }

validate_settings()

client_db = AsyncIOMotorClient(MONGO_URL)
db = client_db["raper_xxii_database"]

releases_col = db["releases"]
reviews_col = db["reviews"]
likes_col = db["likes"]
blocked_col = db["blocked_users"]
sync_events_col = db["sync_events"]
review_reactions_col = db["review_reactions"]
notif_subscribers_col = db["notification_subscribers"]
comments_col = db["review_comments"]

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL_PRIMARY = os.getenv("GROQ_MODEL_PRIMARY", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACKS = [
    m.strip() for m in os.getenv("GROQ_MODEL_FALLBACKS", "llama-3.1-8b-instant").split(",") if m.strip()
]
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "2"))
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "8"))
client_ai = Groq(api_key=GROQ_API_KEY, timeout=GROQ_TIMEOUT, max_retries=0) if GROQ_API_KEY and Groq is not None else None
YANDEX_MUSIC_API_BASE = os.getenv("YANDEX_MUSIC_API_BASE", "https://api.music.yandex.net").rstrip("/")
YANDEX_COVER_SIZE = os.getenv("YANDEX_COVER_SIZE", "1000x1000")
SYNC_POLL_INTERVAL_MS = int(os.getenv("SYNC_POLL_INTERVAL_MS", "500"))
SYNC_MAX_WAIT_MS = int(os.getenv("SYNC_MAX_WAIT_MS", "25000"))
SYNC_EVENT_TTL_SECONDS = int(os.getenv("SYNC_EVENT_TTL_SECONDS", str(48 * 3600)))
# Глубокая ссылка на Mini App для кнопки в push-уведомлении (например
# https://t.me/<bot>/<app>). Если не задана — уведомление шлётся без кнопки.
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
# Сколько релизов/рецензий отдаёт /api/data по умолчанию (можно переопределить
# query-параметром в пределах жёсткого максимума).
DATA_RELEASES_LIMIT = int(os.getenv("DATA_RELEASES_LIMIT", "200"))
DATA_REVIEWS_LIMIT = int(os.getenv("DATA_REVIEWS_LIMIT", "1000"))
DATA_COMMENTS_LIMIT = int(os.getenv("DATA_COMMENTS_LIMIT", "2000"))


def now_ms() -> float:
    return time.time() * 1000


def next_sync_token() -> int:
    return time.time_ns()


def sync_event_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=SYNC_EVENT_TTL_SECONDS)



def client_rate_key(request: Request) -> str:
    """Stable per-user key for rate limiting.

    The user id is trusted only when the initData signature is valid — an
    unverified id from the header could be rotated per request to mint a fresh
    bucket and bypass the limit entirely. The X-Forwarded-For header is
    client-controlled on the left; only the right-most hop is appended by the
    trusted proxy, so that is the single entry we can rely on for the IP key.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if init_data:
        try:
            user_id = validate_telegram_init_data(init_data).get("id")
            if user_id:
                return f"user:{user_id}"
        except Exception:
            pass
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        hops = [p.strip() for p in forwarded.split(",") if p.strip()]
        if hops:
            return f"ip:{hops[-1]}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.clients = defaultdict(list)

    async def __call__(self, request: Request):
        key = client_rate_key(request)
        now = time.time()
        # Drop stale timestamps and prune empty buckets to avoid unbounded growth.
        for stale_key in [k for k, v in self.clients.items() if not v or now - v[-1] >= 60]:
            if stale_key != key:
                del self.clients[stale_key]
        self.clients[key] = [t for t in self.clients[key] if now - t < 60]
        if len(self.clients[key]) >= self.requests_per_minute:
            raise HTTPException(status_code=429, detail="Too many requests")
        self.clients[key].append(now)

rate_limiter = RateLimiter(requests_per_minute=20)


# Strong refs to fire-and-forget tasks: without them asyncio may garbage-collect
# a running task before it finishes.
_background_tasks: set = set()


def spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ============================
# МОДЕЛИ
# ============================
# Фиксированный набор критериев оценки. Значения принудительно нормализуются на
# сервере — клиентский rating/objectiveRating не используется (защита от накрутки).
CRITERIA_KEYS = ("sound", "production", "originality", "meaning", "relevance", "image")


def normalize_criteria(raw) -> dict:
    """Возвращает dict только из известных ключей с int-значениями 1..10."""
    source = raw if isinstance(raw, dict) else {}
    result = {}
    for key in CRITERIA_KEYS:
        try:
            value = int(float(source.get(key, 5)))
        except (TypeError, ValueError):
            value = 5
        result[key] = max(1, min(10, value))
    return result


def compute_review_ratings(base_rating: int, criteria: dict) -> tuple[float, float]:
    """Серверный пересчёт: objectiveRating — среднее критериев, rating — среднее с base."""
    values = [criteria[key] for key in CRITERIA_KEYS]
    objective = round(sum(values) / len(values), 1)
    base = max(1, min(10, int(base_rating)))
    final = round((objective + base) / 2, 1)
    return objective, final


class LinkRequest(BaseModel):
    link: str = Field(min_length=1)

class Release(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    artist: str = Field(min_length=1)
    img: str = ""
    link: str = Field(min_length=1)
    genre: str = ""
    timestamp: float = 0

    @field_validator("link", mode="after")
    @classmethod
    def check_link(cls, v):
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("img", mode="after")
    @classmethod
    def check_img(cls, v):
        # Обложка — либо http(s)-ссылка, либо встроенный data:image (ручная
        # загрузка с устройства). Размер base64 ограничен, чтобы не раздувать БД.
        if not v:
            return v
        if v.startswith(("http://", "https://")):
            return v
        if v.startswith("data:image/"):
            if len(v) > 3_000_000:
                raise ValueError("Embedded image is too large")
            return v
        raise ValueError("img must be an http(s) URL or a data:image/ URI")

class Review(BaseModel):
    id: str = Field(min_length=1)
    relId: str = Field(min_length=1)
    text: str = Field(min_length=30, max_length=3000)
    baseRating: int = Field(ge=1, le=10, default=5)
    criteria: dict = Field(default_factory=dict)
    # rating / objectiveRating принимаются для совместимости, но пересчитываются на сервере.
    rating: float = Field(ge=0, le=10, default=5.0)
    objectiveRating: float = Field(ge=0, le=10, default=5.0)

    @field_validator("criteria", mode="before")
    @classmethod
    def coerce_criteria(cls, v):
        return normalize_criteria(v)

class LikeReq(BaseModel):
    releaseId: str = Field(min_length=1)
    isLike: bool

class ReactReq(BaseModel):
    reacted: bool

class BlockReq(BaseModel):
    username: str = Field(min_length=1)
    blocked: bool

class SubscribeReq(BaseModel):
    enabled: bool

class CommentReq(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=1000)


# ============================
# АВТОРИЗАЦИЯ ПО TELEGRAM initData
# ============================
class TelegramUser:
    """Авторизованный пользователь из Telegram initData"""
    def __init__(self, user_id: int, username: str, first_name: str, is_admin: bool):
        self.user_id = user_id
        self.username = username or ""  # без @, lowercase
        self.first_name = first_name or ""
        self.is_admin = is_admin
        self.display_name = f"@{self.username}" if self.username else self.first_name or f"user-{self.user_id}"


def validate_telegram_init_data(init_data: str) -> dict:
    """
    Проверяет подпись Telegram initData через HMAC-SHA256.
    Если TELEGRAM_BOT_TOKEN не задан — работает в dev-режиме (без криптопроверки).
    """
    parsed = parse_qs(init_data, keep_blank_values=True)

    # Парсим user из initData (есть всегда, даже без токена)
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
        logger.warning("DEV MODE: Telegram signature NOT verified (set TELEGRAM_BOT_TOKEN for production)")
        return user_data

    # Полная криптопроверка
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

    raw_auth_date = parsed.get("auth_date", [None])[0]
    if not raw_auth_date:
        raise HTTPException(401, "Missing auth_date in initData")

    try:
        auth_date = int(raw_auth_date)
    except ValueError:
        raise HTTPException(401, "Invalid auth_date in initData")

    now = int(time.time())
    if auth_date > now + 60:
        raise HTTPException(401, "auth_date is in the future")
    if INIT_DATA_MAX_AGE > 0 and now - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(401, "initData expired")

    return user_data


async def get_current_user(request: Request) -> TelegramUser:
    """
    Dependency: извлекает пользователя.
    - Если есть X-Telegram-Init-Data → парсит (с проверкой или без, зависит от токена)
    - Если нет initData, но есть X-Dev-Username → dev-режим (только если разрешен)
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()

    if init_data:
        tg_user = validate_telegram_init_data(init_data)
        user_id = tg_user.get("id", 0)
        username = normalize_username(tg_user.get("username") or "")
        first_name = (tg_user.get("first_name") or "").strip()
    elif not TELEGRAM_BOT_TOKEN and DEV_MODE:
        # Dev-режим без Telegram: берём username из query/header
        dev_name = request.headers.get("X-Dev-Username", "").strip()
        if not dev_name:
            dev_name = request.query_params.get("username", "guest")
        clean = normalize_username(dev_name)
        username = clean or "guest"
        first_name = dev_name
        user_id = int(hashlib.sha256(username.encode("utf-8")).hexdigest()[:15], 16) % 10**9
        logger.warning("DEV MODE user: %s", username)
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
    """Dependency: как get_current_user, но не бросает ошибку если нет заголовка."""
    try:
        return await get_current_user(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def require_admin(user: TelegramUser = Depends(get_current_user)) -> TelegramUser:
    """Dependency: требует роль Создатель."""
    if not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user


async def check_not_blocked(user: TelegramUser = Depends(get_current_user)) -> TelegramUser:
    """Dependency: проверяет что пользователь не заблокирован."""
    if user.username:
        blocked = await blocked_col.find_one({"username": user.username})
        if blocked and blocked.get("blocked"):
            raise HTTPException(403, "You are blocked from this platform")
    return user


# ============================
# ИНДЕКСЫ
# ============================
async def create_indexes():
    try:
        await releases_col.create_index("id", unique=True)
        await releases_col.create_index("timestamp")
        await releases_col.create_index("syncToken")
        await reviews_col.create_index("id", unique=True)
        await reviews_col.create_index("relId")
        await reviews_col.create_index("author")
        await reviews_col.create_index([("relId", 1), ("authorId", 1)], unique=True)
        await likes_col.create_index([("releaseId", 1), ("userId", 1)], unique=True)
        await likes_col.create_index("username")
        await blocked_col.create_index("username", unique=True)
        await sync_events_col.create_index("syncToken")
        await sync_events_col.create_index([("kind", 1), ("releaseId", 1)])
        await sync_events_col.create_index("expireAt", expireAfterSeconds=0)
        await review_reactions_col.create_index([("reviewId", 1), ("userId", 1)], unique=True)
        await review_reactions_col.create_index("reviewId")
        await review_reactions_col.create_index("userId")
        await notif_subscribers_col.create_index("userId", unique=True)
        await comments_col.create_index("id", unique=True)
        await comments_col.create_index("reviewId")
        await comments_col.create_index("timestamp")
    except Exception as exc:
        logger.warning("Index warning: %s", exc)


def close_db_client():
    client_db.close()


app.router.add_event_handler("startup", create_indexes)
app.router.add_event_handler("shutdown", close_db_client)


# ============================
# УТИЛИТЫ
# ============================
def clean_doc(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


def get_release_sync_token(doc: dict) -> int:
    return int(doc.get("syncToken") or doc.get("updatedAt") or doc.get("timestamp") or 0)


async def record_release_sync_event(kind: str, release_id: str, sync_token: int):
    await sync_events_col.insert_one({
        "kind": kind,
        "releaseId": release_id,
        "syncToken": sync_token,
        "timestamp": now_ms(),
        "expireAt": sync_event_expiry(),
    })


async def record_review_sync_event(kind: str, review_id: str, rel_id: str, sync_token: int):
    await sync_events_col.insert_one({
        "kind": kind,
        "reviewId": review_id,
        "relId": rel_id,
        "syncToken": sync_token,
        "timestamp": now_ms(),
        "expireAt": sync_event_expiry(),
    })


async def record_comment_sync_event(kind: str, comment_id: str, review_id: str, rel_id: str, sync_token: int):
    await sync_events_col.insert_one({
        "kind": kind,
        "commentId": comment_id,
        "reviewId": review_id,
        "relId": rel_id,
        "syncToken": sync_token,
        "timestamp": now_ms(),
        "expireAt": sync_event_expiry(),
    })


async def get_release_sync_events(since: int, limit: int) -> list[dict]:
    return await sync_events_col.find({"syncToken": {"$gt": since}}).sort("syncToken", 1).to_list(length=limit)


async def wait_for_release_sync_events(since: int, limit: int, wait_ms: int) -> list[dict]:
    max_wait_ms = min(wait_ms, SYNC_MAX_WAIT_MS)
    deadline = time.monotonic() + (max_wait_ms / 1000)

    while True:
        events = await get_release_sync_events(since, limit)
        if events or max_wait_ms <= 0:
            return events

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []

        interval = max(SYNC_POLL_INTERVAL_MS, 50) / 1000
        await asyncio.sleep(min(interval, remaining))


# ============================
# PUSH-УВЕДОМЛЕНИЯ О РЕЛИЗАХ
# ============================
def release_deep_link(rel_id: str) -> str:
    """Deep-link на Mini App, открывающий конкретный релиз (?startapp=<id>).

    Пустая строка, если MINI_APP_URL не задан.
    """
    if not MINI_APP_URL:
        return ""
    sep = "&" if "?" in MINI_APP_URL else "?"
    return f"{MINI_APP_URL}{sep}startapp={rel_id}"


async def is_notifications_enabled(user_id: int) -> bool:
    """Подписан ли пользователь на push. Отсутствие записи = подписан (opt-out)."""
    doc = await notif_subscribers_col.find_one({"userId": user_id})
    return doc.get("enabled", True) if doc else True


async def send_release_notifications(release: dict):
    """Рассылает подписчикам уведомление о новом релизе через Telegram Bot API.

    Запускается фоном и не должна влиять на ответ эндпоинта. Ошибки доставки
    отдельным пользователям подавляются; заблокировавших бота (403) отписываем.
    """
    if not TELEGRAM_BOT_TOKEN:
        return

    subscribers = await notif_subscribers_col.find(
        {"enabled": {"$ne": False}}
    ).to_list(length=10000)
    if not subscribers:
        return

    artist = (release.get("artist") or "").strip()
    name = (release.get("name") or "").strip()
    text = f"🎵 Новый релиз в XXII SOUND\n\n{artist} — {name}"

    reply_markup = None
    deep_link = release_deep_link(release.get("id", ""))
    if deep_link:
        reply_markup = {
            "inline_keyboard": [[{"text": "Открыть в приложении", "url": deep_link}]]
        }

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        for sub in subscribers:
            chat_id = sub.get("chatId") or sub.get("userId")
            if not chat_id:
                continue
            payload = {"chat_id": chat_id, "text": text}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = await client.post(api_url, json=payload)
                if resp.status_code == 403:
                    # Пользователь заблокировал бота — отписываем, чтобы не долбить.
                    await notif_subscribers_col.update_one(
                        {"userId": sub.get("userId")},
                        {"$set": {"enabled": False, "updatedAt": now_ms()}},
                    )
            except Exception as exc:
                logger.warning("Notification send failed for %s: %s", chat_id, exc)
            # Бережёмся лимитов Telegram (~30 сообщений/сек).
            await asyncio.sleep(0.05)


# ============================
# API ЭНДПОИНТЫ
# ============================

@app.get("/api/data")
async def get_all_data(
    request: Request,
    releasesLimit: int = Query(default=DATA_RELEASES_LIMIT, ge=1, le=1000),
    reviewsLimit: int = Query(default=DATA_REVIEWS_LIMIT, ge=1, le=5000),
    commentsLimit: int = Query(default=DATA_COMMENTS_LIMIT, ge=1, le=10000),
):
    """Получение каталога. Авторизация опциональна (гости видят каталог).

    `releasesLimit`/`reviewsLimit`/`commentsLimit` ограничивают размер выборки;
    `totalReleases`/`totalReviews`/`totalComments` в ответе позволяют клиенту
    понять, что каталог обрезан.
    """
    tg_user = await get_optional_user(request)

    releases_task = releases_col.find().sort("timestamp", -1).to_list(length=releasesLimit)
    reviews_task = reviews_col.find().sort("timestamp", -1).to_list(length=reviewsLimit)
    comments_task = comments_col.find().sort("timestamp", -1).to_list(length=commentsLimit)
    releases, all_reviews, all_comments = await asyncio.gather(
        releases_task, reviews_task, comments_task
    )

    # Counts, reaction tallies and the sync cursor are independent — fetch them
    # in parallel instead of sequential round-trips.
    total_releases, total_reviews, total_comments, reaction_agg, latest_events = await asyncio.gather(
        releases_col.estimated_document_count(),
        reviews_col.estimated_document_count(),
        comments_col.estimated_document_count(),
        review_reactions_col.aggregate(
            [{"$group": {"_id": "$reviewId", "count": {"$sum": 1}}}]
        ).to_list(length=10000),
        sync_events_col.find().sort("syncToken", -1).to_list(length=1),
    )
    reaction_counts = {row["_id"]: row["count"] for row in reaction_agg}

    release_cursor = max((get_release_sync_token(r) for r in releases), default=0)
    event_cursor = max((int(e.get("syncToken") or 0) for e in latest_events), default=0)
    sync_cursor = max(release_cursor, event_cursor)

    for r in releases: clean_doc(r)
    for r in all_reviews:
        clean_doc(r)
        # Роль автора вычисляется на сервере — клиент больше не получает список админов.
        r["authorIsAdmin"] = normalize_username(r.get("authorUsername", "")) in ADMIN_USERNAMES
        r["reactionCount"] = reaction_counts.get(r.get("id"), 0)
    for c in all_comments:
        clean_doc(c)
        c["authorIsAdmin"] = normalize_username(c.get("authorUsername", "")) in ADMIN_USERNAMES

    # Лайки текущего пользователя
    user_likes = []
    my_reactions = []
    is_admin = False
    display_name = "Гость"
    username = ""
    is_blocked = False
    notifications_enabled = True

    if tg_user:
        display_name = tg_user.display_name
        username = tg_user.username
        is_admin = tg_user.is_admin
        notifications_enabled = await is_notifications_enabled(tg_user.user_id)

        my_reaction_docs = await review_reactions_col.find({"userId": tg_user.user_id}).to_list(length=5000)
        my_reactions = [doc["reviewId"] for doc in my_reaction_docs]

        if tg_user.username:
            likes = await likes_col.find({
                "$or": [{"userId": tg_user.user_id}, {"username": tg_user.username}]
            }).to_list(length=1000)
            user_likes = [l["releaseId"] for l in likes]
            # Проверка блокировки
            blocked_doc = await blocked_col.find_one({"username": tg_user.username})
            is_blocked = bool(blocked_doc and blocked_doc.get("blocked"))

    # Список заблокированных (только для админов)
    blocked_list = []
    if is_admin:
        blocked_docs = await blocked_col.find({"blocked": True}).to_list(length=500)
        blocked_list = [clean_doc(d).get("username", "") for d in blocked_docs]

    return {
        "releases": releases,
        "reviews": all_reviews,
        "comments": all_comments,
        "likes": user_likes,
        "myReactions": my_reactions,
        "currentUser": {
            "userId": tg_user.user_id if tg_user else None,
            "displayName": display_name,
            "username": username,
            "isAdmin": is_admin,
            "isBlocked": is_blocked,
            "isAuthenticated": tg_user is not None,
            "notificationsEnabled": notifications_enabled,
        },
        "blockedUsers": blocked_list,
        # Курсор синхронизации — строкой: значение time_ns превышает
        # Number.MAX_SAFE_INTEGER, и числом JS терял бы точность.
        "syncCursor": str(sync_cursor),
        "miniAppUrl": MINI_APP_URL,
        "totalReleases": total_releases,
        "totalReviews": total_reviews,
        "totalComments": total_comments,
    }


@app.get("/api/sync/releases")
async def sync_releases(
    since: str = Query("0"),
    limit: int = Query(100, ge=1, le=500),
    waitMs: int = 0,
):
    """
    Fast incremental release sync.
    First call may use since=0; later calls should pass the returned cursor.

    `since`/`cursor` — строки: токены (time_ns) превышают
    Number.MAX_SAFE_INTEGER, поэтому числом JS терял бы точность.
    """
    since_token = int(since) if str(since).isdigit() else 0

    if since_token == 0:
        releases = await releases_col.find().sort("syncToken", -1).to_list(length=limit)
        for release in releases:
            clean_doc(release)

        cursor = max((get_release_sync_token(release) for release in releases), default=0)
        return {
            "cursor": str(cursor),
            "serverTime": next_sync_token(),
            "releases": releases,
            "deletedReleaseIds": [],
            "reviews": [],
            "deletedReviewIds": [],
            "comments": [],
            "deletedCommentIds": [],
            "hasMore": False,
        }

    events = await wait_for_release_sync_events(since_token, limit, waitMs)

    changed_release_ids = []
    deleted_release_ids = []
    changed_review_ids = []
    deleted_review_ids = []
    changed_comment_ids = []
    deleted_comment_ids = []
    for event in events:
        kind = event.get("kind")
        if kind == "release_deleted":
            if event.get("releaseId"):
                deleted_release_ids.append(event["releaseId"])
        elif kind == "release_upserted":
            if event.get("releaseId"):
                changed_release_ids.append(event["releaseId"])
        elif kind == "review_deleted":
            if event.get("reviewId"):
                deleted_review_ids.append(event["reviewId"])
        elif kind == "review_added":
            if event.get("reviewId"):
                changed_review_ids.append(event["reviewId"])
        elif kind == "comment_deleted":
            if event.get("commentId"):
                deleted_comment_ids.append(event["commentId"])
        elif kind == "comment_added":
            if event.get("commentId"):
                changed_comment_ids.append(event["commentId"])

    releases = []
    if changed_release_ids:
        unique_ids = list(dict.fromkeys(changed_release_ids))
        releases = await releases_col.find({"id": {"$in": unique_ids}}).to_list(length=len(unique_ids))
        releases.sort(key=get_release_sync_token)
        for release in releases:
            clean_doc(release)

    reviews = []
    if changed_review_ids:
        unique_review_ids = list(dict.fromkeys(changed_review_ids))
        reviews = await reviews_col.find({"id": {"$in": unique_review_ids}}).to_list(length=len(unique_review_ids))
        for review in reviews:
            clean_doc(review)
            review["authorIsAdmin"] = normalize_username(review.get("authorUsername", "")) in ADMIN_USERNAMES

    comments = []
    if changed_comment_ids:
        unique_comment_ids = list(dict.fromkeys(changed_comment_ids))
        comments = await comments_col.find({"id": {"$in": unique_comment_ids}}).to_list(length=len(unique_comment_ids))
        for comment in comments:
            clean_doc(comment)
            comment["authorIsAdmin"] = normalize_username(comment.get("authorUsername", "")) in ADMIN_USERNAMES

    cursor = max((int(event.get("syncToken") or since_token) for event in events), default=since_token)
    return {
        "cursor": str(cursor),
        "serverTime": next_sync_token(),
        "releases": releases,
        "deletedReleaseIds": list(dict.fromkeys(deleted_release_ids)),
        "reviews": reviews,
        "deletedReviewIds": list(dict.fromkeys(deleted_review_ids)),
        "comments": comments,
        "deletedCommentIds": list(dict.fromkeys(deleted_comment_ids)),
        "hasMore": len(events) == limit,
    }


@app.post("/api/releases")
async def add_release(rel: Release, user: TelegramUser = Depends(require_admin)):
    """Добавить релиз — только Создатель."""
    data = rel.model_dump()
    sync_token = next_sync_token()
    if data.get("timestamp", 0) <= 0:
        data["timestamp"] = now_ms()
    data["updatedAt"] = now_ms()
    data["syncToken"] = sync_token
    data["createdBy"] = user.display_name
    data["createdById"] = user.user_id
    result = await releases_col.update_one({"id": rel.id}, {"$set": data}, upsert=True)
    await record_release_sync_event("release_upserted", rel.id, sync_token)
    # Уведомляем подписчиков только о действительно новом релизе (вставка),
    # а не о правке существующего. Рассылка идёт фоном.
    if result.upserted_id is not None:
        spawn_background(send_release_notifications(data))
    return {"status": "ok", "syncToken": sync_token}


@app.delete("/api/releases/{rel_id}")
async def delete_release(rel_id: str, user: TelegramUser = Depends(require_admin)):
    """Удалить релиз + связанные рецензии, лайки и реакции — только Создатель."""
    sync_token = next_sync_token()
    doomed_reviews = await reviews_col.find({"relId": rel_id}).to_list(length=5000)
    review_ids = [r.get("id") for r in doomed_reviews if r.get("id")]
    await releases_col.delete_one({"id": rel_id})
    await reviews_col.delete_many({"relId": rel_id})
    await likes_col.delete_many({"releaseId": rel_id})
    if review_ids:
        await review_reactions_col.delete_many({"reviewId": {"$in": review_ids}})
        await comments_col.delete_many({"reviewId": {"$in": review_ids}})
    await record_release_sync_event("release_deleted", rel_id, sync_token)
    return {"status": "ok", "syncToken": sync_token}


@app.post("/api/releases/{rel_id}/share-message")
async def share_release_message(
    rel_id: str,
    user: TelegramUser = Depends(get_current_user),
    _=Depends(rate_limiter),
):
    """
    Готовит нативное Telegram-сообщение о релизе для шеринга.

    Через Bot API savePreparedInlineMessage сохраняется inline-результат с
    обложкой и кнопкой, ведущей в Mini App на этот релиз; клиент затем вызывает
    Telegram.WebApp.shareMessage с возвращённым id.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(503, "Sharing is not configured")
    deep_link = release_deep_link(rel_id)
    if not deep_link:
        raise HTTPException(503, "Sharing is not configured")

    release = await releases_col.find_one({"id": rel_id})
    if not release:
        raise HTTPException(404, "Release not found")

    artist = (release.get("artist") or "").strip() or "Артист"
    name = (release.get("name") or "").strip() or "Релиз"
    img = release.get("img") or ""
    caption = f"🎵 {artist} — {name}"
    reply_markup = {"inline_keyboard": [[{"text": "Открыть релиз", "url": deep_link}]]}

    if img.startswith(("http://", "https://")):
        # Обложка по публичному URL — отдаём фото-результат.
        result = {
            "type": "photo",
            "id": rel_id,
            "photo_url": img,
            "thumbnail_url": img,
            "title": name,
            "description": artist,
            "caption": caption,
            "reply_markup": reply_markup,
        }
    else:
        # Нет публичной обложки (пусто или data:-URI) — текстовый результат.
        result = {
            "type": "article",
            "id": rel_id,
            "title": f"{artist} — {name}",
            "description": "Поделиться релизом из XXII SOUND",
            "input_message_content": {"message_text": caption},
            "reply_markup": reply_markup,
        }

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/savePreparedInlineMessage"
    payload = {
        "user_id": user.user_id,
        "result": result,
        "allow_user_chats": True,
        "allow_group_chats": True,
        "allow_channel_chats": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(api_url, json=payload)
        data = resp.json()
    except Exception as exc:
        logger.warning("savePreparedInlineMessage failed: %s", exc)
        raise HTTPException(502, "Failed to prepare share message")

    prepared_id = data.get("result", {}).get("id") if data.get("ok") else None
    if not prepared_id:
        logger.warning("savePreparedInlineMessage rejected: %s", data)
        raise HTTPException(502, "Failed to prepare share message")

    return {"preparedMessageId": prepared_id}


@app.post("/api/reviews")
async def add_review(rev: Review, user: TelegramUser = Depends(check_not_blocked), _=Depends(rate_limiter)):
    """
    Добавить рецензию.
    - Автор определяется из Telegram (нельзя подделать).
    - Одна рецензия на релиз.
    - Заблокированные пользователи не могут писать.
    """
    # Проверяем что релиз существует
    release = await releases_col.find_one({"id": rev.relId})
    if not release:
        raise HTTPException(404, "Release not found")

    # Проверяем дубликат
    existing = await reviews_col.find_one({
        "relId": rev.relId,
        "authorId": user.user_id
    })
    if existing:
        raise HTTPException(409, "You already reviewed this release")

    data = rev.model_dump()
    # Рейтинг считается на сервере из критериев — клиентские значения игнорируются.
    objective, final = compute_review_ratings(data["baseRating"], data["criteria"])
    data["objectiveRating"] = objective
    data["rating"] = final
    data["author"] = user.display_name
    data["authorId"] = user.user_id
    data["authorUsername"] = user.username
    data["authorIsAdmin"] = user.is_admin
    data["date"] = time.strftime("%d.%m.%Y")
    data["timestamp"] = time.time() * 1000
    sync_token = next_sync_token()
    data["syncToken"] = sync_token
    try:
        await reviews_col.insert_one(data)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="User already reviewed this release")
    await record_review_sync_event("review_added", rev.id, rev.relId, sync_token)
    return {"status": "ok", "review": clean_doc(data), "syncToken": sync_token}


@app.delete("/api/reviews/{review_id}")
async def delete_review(review_id: str, user: TelegramUser = Depends(get_current_user)):
    """
    Удалить рецензию.
    - Владелец может удалить свою.
    - Создатель может удалить любую.
    """
    review = await reviews_col.find_one({"id": review_id})
    if not review:
        raise HTTPException(404, "Review not found")

    is_owner = review.get("authorId") == user.user_id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "You can only delete your own reviews")

    sync_token = next_sync_token()
    await reviews_col.delete_one({"id": review_id})
    await review_reactions_col.delete_many({"reviewId": review_id})
    await comments_col.delete_many({"reviewId": review_id})
    await record_review_sync_event("review_deleted", review_id, review.get("relId", ""), sync_token)
    return {"status": "ok", "syncToken": sync_token}


@app.post("/api/reviews/{review_id}/comments")
async def add_comment(
    review_id: str,
    req: CommentReq,
    user: TelegramUser = Depends(check_not_blocked),
    _=Depends(rate_limiter),
):
    """
    Добавить комментарий к рецензии.
    - Автор определяется из Telegram (нельзя подделать).
    - Несколько комментариев на рецензию разрешено.
    - Заблокированные пользователи не могут писать.
    """
    review = await reviews_col.find_one({"id": review_id})
    if not review:
        raise HTTPException(404, "Review not found")

    sync_token = next_sync_token()
    data = {
        "id": req.id,
        "reviewId": review_id,
        "relId": review.get("relId", ""),
        "text": req.text,
        "author": user.display_name,
        "authorId": user.user_id,
        "authorUsername": user.username,
        "authorIsAdmin": user.is_admin,
        "date": time.strftime("%d.%m.%Y"),
        "timestamp": now_ms(),
        "syncToken": sync_token,
    }
    try:
        await comments_col.insert_one(data)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Duplicate comment id")
    await record_comment_sync_event("comment_added", req.id, review_id, data["relId"], sync_token)
    return {"status": "ok", "comment": clean_doc(data), "syncToken": sync_token}


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: str, user: TelegramUser = Depends(get_current_user)):
    """
    Удалить комментарий.
    - Владелец может удалить свой.
    - Создатель может удалить любой.
    """
    comment = await comments_col.find_one({"id": comment_id})
    if not comment:
        raise HTTPException(404, "Comment not found")

    is_owner = comment.get("authorId") == user.user_id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "You can only delete your own comments")

    sync_token = next_sync_token()
    await comments_col.delete_one({"id": comment_id})
    await record_comment_sync_event(
        "comment_deleted", comment_id, comment.get("reviewId", ""), comment.get("relId", ""), sync_token
    )
    return {"status": "ok", "syncToken": sync_token}


@app.post("/api/likes")
async def toggle_like(req: LikeReq, user: TelegramUser = Depends(check_not_blocked), _=Depends(rate_limiter)):
    """Лайк/анлайк — только авторизованные, не заблокированные."""
    release = await releases_col.find_one({"id": req.releaseId})
    if not release:
        raise HTTPException(404, "Release not found")

    if req.isLike:
        await likes_col.update_one(
            {"releaseId": req.releaseId, "userId": user.user_id},
            {"$set": {
                "releaseId": req.releaseId,
                "username": user.username,
                "userId": user.user_id
            }},
            upsert=True,
        )
    else:
        await likes_col.delete_one({"releaseId": req.releaseId, "userId": user.user_id})
    return {"status": "ok"}


@app.post("/api/reviews/{review_id}/react")
async def react_to_review(
    review_id: str,
    req: ReactReq,
    user: TelegramUser = Depends(check_not_blocked),
    _=Depends(rate_limiter),
):
    """Реакция «полезно» на рецензию — toggle, только авторизованные и не заблокированные."""
    review = await reviews_col.find_one({"id": review_id})
    if not review:
        raise HTTPException(404, "Review not found")

    if req.reacted:
        await review_reactions_col.update_one(
            {"reviewId": review_id, "userId": user.user_id},
            {"$set": {"reviewId": review_id, "userId": user.user_id, "username": user.username}},
            upsert=True,
        )
    else:
        await review_reactions_col.delete_one({"reviewId": review_id, "userId": user.user_id})

    count = await review_reactions_col.count_documents({"reviewId": review_id})
    return {"status": "ok", "reactionCount": count}


@app.post("/api/block")
async def block_user(req: BlockReq, admin: TelegramUser = Depends(require_admin)):
    """Заблокировать / разблокировать пользователя — только Создатель."""
    target = normalize_username(req.username)
    if target in ADMIN_USERNAMES:
        raise HTTPException(400, "Cannot block an admin")

    await blocked_col.update_one(
        {"username": target},
        {"$set": {"username": target, "blocked": req.blocked, "blockedBy": admin.display_name}},
        upsert=True,
    )

    action = "blocked" if req.blocked else "unblocked"
    return {"status": "ok", "detail": f"User @{target} {action}"}


@app.delete("/api/reviews/by-author/{username}")
async def delete_all_reviews_by_author(username: str, admin: TelegramUser = Depends(require_admin)):
    """Удалить все рецензии пользователя — только Создатель."""
    target = normalize_username(username)
    # Собираем id заранее, чтобы разослать sync-события для real-time обновления.
    doomed = await reviews_col.find({"authorUsername": target}).to_list(length=1000)
    review_ids = [r.get("id") for r in doomed if r.get("id")]
    result = await reviews_col.delete_many({"authorUsername": target})
    if review_ids:
        await review_reactions_col.delete_many({"reviewId": {"$in": review_ids}})
        await comments_col.delete_many({"reviewId": {"$in": review_ids}})
    if doomed:
        await sync_events_col.insert_many([
            {
                "kind": "review_deleted",
                "reviewId": review.get("id", ""),
                "relId": review.get("relId", ""),
                "syncToken": next_sync_token(),
                "timestamp": now_ms(),
                "expireAt": sync_event_expiry(),
            }
            for review in doomed
        ])
    return {"status": "ok", "deleted": result.deleted_count}


# ============================
# ПАРСЕР ССЫЛОК
# ============================
def parse_yandex_music_url(url: str) -> dict:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"music.yandex.ru", "music.yandex.com"}:
        return {}

    parts = [part for part in parsed.path.split("/") if part]
    result = {}
    for index, part in enumerate(parts):
        if part in {"album", "track"} and index + 1 < len(parts):
            value = parts[index + 1]
            if value.isdigit():
                result[f"{part}_id"] = value
    query = parse_qs(parsed.query)
    track_id = query.get("track", [None])[0]
    if track_id and track_id.isdigit():
        result["track_id"] = track_id
    return result


def is_yandex_music_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"music.yandex.ru", "music.yandex.com"}


def yandex_cover_url(cover_uri: str) -> str:
    if not cover_uri:
        return ""
    uri = cover_uri.replace("%%", YANDEX_COVER_SIZE)
    if uri.startswith("//"):
        return f"https:{uri}"
    if uri.startswith(("http://", "https://")):
        return uri
    return f"https://{uri}"


def join_yandex_names(items: list[dict]) -> str:
    names = [item.get("name", "").strip() for item in items or [] if isinstance(item, dict) and item.get("name")]
    return ", ".join(dict.fromkeys(names))


def normalize_yandex_release_result(name: str, artist: str, img: str, genre: str = "") -> dict:
    return {
        "artist": clean_ai_text(artist, "Артист"),
        "name": clean_ai_text(name, "Релиз"),
        "img": img,
        "genre": normalize_genre(genre),
    }


def yandex_album_to_release(album: dict) -> dict:
    artist = join_yandex_names(album.get("artists")) or join_yandex_names(album.get("labels"))
    img = yandex_cover_url(album.get("coverUri") or album.get("ogImage") or album.get("cover", {}).get("uri", ""))
    genre = album.get("genre") or ""
    return normalize_yandex_release_result(album.get("title", ""), artist, img, genre)


def yandex_track_to_release(track: dict) -> dict:
    album = (track.get("albums") or [{}])[0] if isinstance(track.get("albums"), list) else {}
    artist = join_yandex_names(track.get("artists")) or join_yandex_names(album.get("artists")) or join_yandex_names(album.get("labels"))
    img = yandex_cover_url(track.get("coverUri") or track.get("ogImage") or album.get("coverUri") or album.get("ogImage", ""))
    genre = track.get("genre") or album.get("genre") or ""
    return normalize_yandex_release_result(track.get("title", ""), artist, img, genre)


async def get_yandex_music_release(url: str) -> Optional[dict]:
    ids = parse_yandex_music_url(url)
    if not ids:
        return None

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=8.0, follow_redirects=False) as h_client:
            if ids.get("track_id"):
                res = await h_client.get(f"{YANDEX_MUSIC_API_BASE}/tracks/{ids['track_id']}")
                res.raise_for_status()
                payload = res.json().get("result") or []
                if payload:
                    return yandex_track_to_release(payload[0])

            if ids.get("album_id"):
                res = await h_client.get(f"{YANDEX_MUSIC_API_BASE}/albums/{ids['album_id']}/with-tracks")
                res.raise_for_status()
                album = res.json().get("result") or {}
                if album:
                    return yandex_album_to_release(album)
    except Exception as exc:
        logger.warning("Yandex Music parser fallback used due to error: %s", exc)
    return None


async def get_metadata_from_page(url: str):
    if not BeautifulSoup:
        return "", "", ""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        async with httpx.AsyncClient(follow_redirects=False, headers=headers, timeout=10.0) as h_client:
            current_url = url
            for _ in range(5):
                if not is_safe_public_url(current_url):
                    return "", "", ""
                res = await h_client.get(current_url)
                if res.is_redirect:
                    current_url = str(res.next_request.url)
                    continue
                res.raise_for_status()
                break
            else:
                return "", "", ""

            soup = BeautifulSoup(res.text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            title = str(og_title["content"]) if og_title and og_title.get("content") else ""
            if not title:
                title = str(soup.title.string) if soup.title and soup.title.string else ""
            img = ""
            genre = ""

            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                candidate_img = urljoin(str(res.url), str(og["content"]).strip())
                if candidate_img.startswith(("http://", "https://")):
                    img = candidate_img

            # Пытаемся извлечь жанр из мета-тегов
            for prop in ["og:music:genre", "music:genre", "genre"]:
                tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                if tag and tag.get("content"):
                    genre = tag["content"].strip()
                    break

            # Яндекс Музыка: жанр в JSON-LD или в breadcrumb
            if "music.yandex" in url:
                # JSON-LD
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(script.string)
                        if isinstance(ld, dict):
                            g = ld.get("genre") or ld.get("@graph", [{}])[0].get("genre", "")
                            if g:
                                genre = g if isinstance(g, str) else ", ".join(g)
                                break
                    except Exception:
                        pass
                # Breadcrumb / page text fallback
                if not genre:
                    for a in soup.find_all("a", class_=lambda c: c and "genre" in c.lower() if c else False):
                        genre = a.get_text(strip=True)
                        if genre:
                            break

            # Spotify: genre иногда в мета
            if "spotify.com" in url and not genre:
                og_desc = soup.find("meta", property="og:description")
                if og_desc and og_desc.get("content"):
                    desc = og_desc["content"]
                    # "Listen to X on Spotify. Genre · Year"
                    parts = re.split(r"\s*[·•]\s*", desc)
                    if len(parts) >= 2:
                        candidate = parts[-1].strip().rstrip(".")
                        if len(candidate) < 30 and not candidate.isdigit():
                            genre = candidate

            return title.strip(), img, genre
    except Exception:
        return "", "", ""


def is_safe_public_url(url: str) -> bool:
    """SSRF protection: allow only http(s), standard ports and globally
    routable IP targets.

    DNS is resolved here and again by httpx at request time, so a narrow
    rebinding window remains; callers re-check every redirect hop and keep the
    gap between this check and the request minimal.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.port is not None and parsed.port not in (80, 443):
            return False
        host = parsed.hostname
        if not host:
            return False
        if host.lower() == "localhost":
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            # Unwrap IPv4-mapped IPv6 (::ffff:10.0.0.1) before classifying.
            mapped = getattr(ip, "ipv4_mapped", None)
            if mapped is not None:
                ip = mapped
            if not ip.is_global or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def clean_ai_text(value, fallback: str, max_length: int = 120) -> str:
    raw_value = value if isinstance(value, str) and value.strip() else fallback
    cleaned = re.sub(r"\s+", " ", str(raw_value or "")).strip(" \t\r\n\"'`")
    if not cleaned:
        cleaned = str(fallback or "")
    return cleaned[:max_length]


def parse_ai_json(content: str) -> dict:
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def guess_release_from_title(raw_title: str, link: str) -> dict:
    title = re.sub(r"\s+", " ", html.unescape(raw_title or "")).strip()
    title = re.sub(r"\s*\|\s*(Spotify|Apple Music|YouTube Music|Yandex Music|Яндекс Музыка)\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*[-–—]\s*(Spotify|Apple Music|YouTube Music|Yandex Music|Яндекс Музыка)\s*$", "", title, flags=re.I)

    by_match = re.match(r"(?P<name>.+?)\s+by\s+(?P<artist>.+)$", title, flags=re.I)
    if by_match:
        return {
            "artist": by_match.group("artist").strip(),
            "name": by_match.group("name").strip(),
            "genre": "",
        }

    for separator in (" - ", " – ", " — "):
        if separator in title:
            artist, name = title.split(separator, 1)
            return {"artist": artist.strip(), "name": name.strip(), "genre": ""}

    parsed = urlparse(link)
    path_name = unquote(parsed.path.rstrip("/").split("/")[-1]).replace("-", " ").strip()
    return {
        "artist": "",
        "name": title or path_name,
        "genre": "",
    }


def normalize_release_result(payload: dict, raw_title: str, link: str, detected_genre: str) -> dict:
    guessed = guess_release_from_title(raw_title, link)
    raw_genre = payload.get("genre", "") if isinstance(payload, dict) else ""
    genre = detected_genre or normalize_genre(raw_genre)
    return {
        "artist": clean_ai_text(payload.get("artist") if isinstance(payload, dict) else "", guessed["artist"], 120),
        "name": clean_ai_text(payload.get("name") if isinstance(payload, dict) else "", guessed["name"], 160),
        "genre": genre,
    }


def call_ai_extract_release(raw_title: str, link: str, detected_genre: str) -> dict:
    if not raw_title.strip():
        return normalize_release_result({}, raw_title, link, detected_genre)

    if not client_ai:
        return normalize_release_result({}, raw_title, link, detected_genre)

    models = [GROQ_MODEL_PRIMARY, *GROQ_MODEL_FALLBACKS]
    prompt_suffix = "" if detected_genre else " Also guess 'genre' from context."
    last_error = None

    for model_name in models:
        for _ in range(max(1, GROQ_MAX_RETRIES)):
            try:
                chat = client_ai.chat.completions.create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Extract a music release from the page title or URL. "
                                "Return only compact JSON with string keys: artist, name"
                                + (", genre" if not detected_genre else "")
                                + ". Use only the provided page title. Do not invent missing data. "
                                + f"Remove platform names, marketing words, and quotes.{prompt_suffix}"
                            ),
                        },
                        {"role": "user", "content": raw_title},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_completion_tokens=160,
                    timeout=GROQ_TIMEOUT,
                )
                payload = parse_ai_json(chat.choices[0].message.content)
                return normalize_release_result(payload, raw_title, link, detected_genre)
            except Exception as exc:
                last_error = exc
                continue

    logger.warning("AI parsing fallback used due to error: %s", last_error)
    return normalize_release_result({}, raw_title, link, detected_genre)


async def ai_extract_release(raw_title: str, link: str, detected_genre: str) -> dict:
    return await asyncio.to_thread(call_ai_extract_release, raw_title, link, detected_genre)


# Маппинг распространённых жанров на русский
GENRE_MAP = {
    "rap": "Рэп", "hip hop": "Хип-хоп", "hip-hop": "Хип-хоп", "hiphop": "Хип-хоп",
    "trap": "Трэп", "r&b": "R&B", "rnb": "R&B", "pop": "Поп",
    "rock": "Рок", "electronic": "Электронная", "edm": "Электронная",
    "jazz": "Джаз", "metal": "Метал",
    "рэп": "Рэп", "хип-хоп": "Хип-хоп", "трэп": "Трэп", "поп": "Поп",
    "рок": "Рок", "электронная": "Электронная", "джаз": "Джаз", "метал": "Метал",
    "indie": "Рок", "alternative": "Рок", "soul": "R&B",
    "drill": "Трэп", "phonk": "Трэп", "lo-fi": "Хип-хоп",
}


def normalize_genre(raw: str) -> str:
    """Нормализует жанр к одному из стандартных."""
    if not raw:
        return ""
    low = raw.strip().lower()
    # Прямое совпадение
    if low in GENRE_MAP:
        return GENRE_MAP[low]
    # Частичное совпадение
    for key, val in GENRE_MAP.items():
        if key in low:
            return val
    return "Другое"


@app.post("/api/parse_link")
async def parse_link(req: LinkRequest, user: TelegramUser = Depends(require_admin), _=Depends(rate_limiter)):
    """Распознавание ссылки — только Создатель. Возвращает artist, name, img, genre."""
    if not is_safe_public_url(req.link):
        raise HTTPException(400, "Unsafe or unsupported URL")

    yandex_result = await get_yandex_music_release(req.link)
    if yandex_result:
        return yandex_result
    if is_yandex_music_url(req.link):
        raise HTTPException(502, "Could not fetch Yandex Music metadata")

    raw_title, found_image, raw_genre = await get_metadata_from_page(req.link)
    if not raw_title:
        raise HTTPException(422, "Could not read release metadata from link")

    detected_genre = normalize_genre(raw_genre)
    result = await ai_extract_release(raw_title, req.link, detected_genre)
    result["img"] = found_image
    if not detected_genre and result.get("genre"):
        detected_genre = normalize_genre(result["genre"])
    result["genre"] = detected_genre
    return result


@app.post("/api/notifications/subscribe")
async def set_notifications(req: SubscribeReq, user: TelegramUser = Depends(get_current_user)):
    """Включить/выключить push-уведомления о новых релизах для пользователя."""
    await notif_subscribers_col.update_one(
        {"userId": user.user_id},
        {"$set": {
            "userId": user.user_id,
            "username": user.username,
            "chatId": user.user_id,
            "enabled": req.enabled,
            "updatedAt": now_ms(),
        }},
        upsert=True,
    )
    return {"status": "ok", "enabled": req.enabled}


@app.get("/api/health")
async def health():
    return {"status": "ok", "auth": bool(TELEGRAM_BOT_TOKEN)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
