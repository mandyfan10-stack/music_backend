from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from pymongo.errors import DuplicateKeyError
from collections import defaultdict
import asyncio
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

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
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

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL_PRIMARY = os.getenv("GROQ_MODEL_PRIMARY", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACKS = [
    m.strip() for m in os.getenv("GROQ_MODEL_FALLBACKS", "llama-3.1-8b-instant").split(",") if m.strip()
]
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "2"))
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "8"))
client_ai = Groq(api_key=GROQ_API_KEY, timeout=GROQ_TIMEOUT, max_retries=0) if GROQ_API_KEY and Groq is not None else None


def now_ms() -> float:
    return time.time() * 1000


def next_sync_token() -> int:
    return time.time_ns()



class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.clients = defaultdict(list)

    async def __call__(self, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        self.clients[client_ip] = [t for t in self.clients[client_ip] if now - t < 60]
        if len(self.clients[client_ip]) >= self.requests_per_minute:
            raise HTTPException(status_code=429, detail="Too many requests")
        self.clients[client_ip].append(now)

rate_limiter = RateLimiter(requests_per_minute=20)


# ============================
# МОДЕЛИ
# ============================
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

    @field_validator("id", "name", "artist", "genre", mode="before")
    @classmethod
    def sanitize_strings(cls, v):
        if isinstance(v, str):
            return html.escape(v)
        return v

    @field_validator("img", "link", mode="after")
    @classmethod
    def check_urls(cls, v):
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

class Review(BaseModel):
    id: str = Field(min_length=1)
    relId: str = Field(min_length=1)
    text: str = Field(min_length=30, max_length=3000)
    rating: float = Field(ge=0, le=10)
    baseRating: int = Field(ge=1, le=10, default=5)
    criteria: dict = Field(default_factory=dict)
    objectiveRating: float = Field(ge=0, le=10, default=5.0)

    @field_validator("id", "relId", "text", mode="before")
    @classmethod
    def sanitize_strings(cls, v):
        if isinstance(v, str):
            return html.escape(v)
        return v

    @field_validator("criteria", mode="before")
    @classmethod
    def sanitize_criteria(cls, v):
        def sanitize(obj):
            if isinstance(obj, str):
                return html.escape(obj)
            elif isinstance(obj, dict):
                return {sanitize(k): sanitize(val) for k, val in obj.items()}
            elif isinstance(obj, list):
                return [sanitize(item) for item in obj]
            return obj
        return sanitize(v)

class LikeReq(BaseModel):
    releaseId: str = Field(min_length=1)
    isLike: bool

class BlockReq(BaseModel):
    username: str = Field(min_length=1)
    blocked: bool

    @field_validator("username", mode="before")
    @classmethod
    def sanitize_username(cls, v):
        if isinstance(v, str):
            return html.escape(v)
        return v


# ============================
# АВТОРИЗАЦИЯ ПО TELEGRAM initData
# ============================
class TelegramUser:
    """Авторизованный пользователь из Telegram initData"""
    def __init__(self, user_id: int, username: str, first_name: str, is_admin: bool):
        self.user_id = user_id
        self.username = html.escape(username) if username else ""  # без @, lowercase
        self.first_name = html.escape(first_name) if first_name else ""
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
        print("⚠️  DEV MODE: Telegram signature NOT verified (set TELEGRAM_BOT_TOKEN for production)")
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
        await releases_col.create_index("syncToken")
        await reviews_col.create_index("id", unique=True)
        await reviews_col.create_index("relId")
        await reviews_col.create_index("author")
        await reviews_col.create_index([("relId", 1), ("authorId", 1)], unique=True)
        await likes_col.create_index([("releaseId", 1), ("userId", 1)], unique=True)
        await blocked_col.create_index("username", unique=True)
        await sync_events_col.create_index("syncToken")
        await sync_events_col.create_index([("kind", 1), ("releaseId", 1)])
    except Exception as exc:
        print(f"Index warning: {exc}")


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
    })


async def get_current_sync_cursor(releases: list[dict]) -> int:
    release_cursor = max((get_release_sync_token(release) for release in releases), default=0)
    latest_events = await sync_events_col.find().sort("syncToken", -1).to_list(length=1)
    event_cursor = max((int(event.get("syncToken") or 0) for event in latest_events), default=0)
    return max(release_cursor, event_cursor)


# ============================
# API ЭНДПОИНТЫ
# ============================

@app.get("/api/data")
async def get_all_data(request: Request):
    """Получение каталога. Авторизация опциональна (гости видят каталог)."""
    tg_user = await get_optional_user(request)

    releases = await releases_col.find().sort("timestamp", -1).to_list(length=100)
    all_reviews = await reviews_col.find().sort("timestamp", -1).to_list(length=500)
    sync_cursor = await get_current_sync_cursor(releases)

    for r in releases: clean_doc(r)
    for r in all_reviews: clean_doc(r)

    # Лайки текущего пользователя
    user_likes = []
    is_admin = False
    display_name = "Гость"
    username = ""
    is_blocked = False

    if tg_user:
        display_name = tg_user.display_name
        username = tg_user.username
        is_admin = tg_user.is_admin

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
        "likes": user_likes,
        "currentUser": {
            "displayName": display_name,
            "username": username,
            "isAdmin": is_admin,
            "isBlocked": is_blocked,
            "isAuthenticated": tg_user is not None,
        },
        "blockedUsers": blocked_list,
        "syncCursor": sync_cursor,
    }


@app.get("/api/sync/releases")
async def sync_releases(
    since: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """
    Fast incremental release sync.
    First call may use since=0; later calls should pass the returned cursor.
    """
    if since == 0:
        releases = await releases_col.find().sort("syncToken", -1).to_list(length=limit)
        for release in releases:
            clean_doc(release)

        cursor = max((get_release_sync_token(release) for release in releases), default=0)
        return {
            "cursor": cursor,
            "serverTime": next_sync_token(),
            "releases": releases,
            "deletedReleaseIds": [],
            "hasMore": False,
        }

    events = await sync_events_col.find({"syncToken": {"$gt": since}}).sort("syncToken", 1).to_list(length=limit)

    changed_release_ids = []
    deleted_release_ids = []
    for event in events:
        release_id = event.get("releaseId")
        if not release_id:
            continue

        if event.get("kind") == "release_deleted":
            deleted_release_ids.append(release_id)
        elif event.get("kind") == "release_upserted":
            changed_release_ids.append(release_id)

    releases = []
    if changed_release_ids:
        unique_ids = list(dict.fromkeys(changed_release_ids))
        releases = await releases_col.find({"id": {"$in": unique_ids}}).to_list(length=len(unique_ids))
        releases.sort(key=get_release_sync_token)
        for release in releases:
            clean_doc(release)

    cursor = max((int(event.get("syncToken") or since) for event in events), default=since)
    return {
        "cursor": cursor,
        "serverTime": next_sync_token(),
        "releases": releases,
        "deletedReleaseIds": list(dict.fromkeys(deleted_release_ids)),
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
    await releases_col.update_one({"id": rel.id}, {"$set": data}, upsert=True)
    await record_release_sync_event("release_upserted", rel.id, sync_token)
    return {"status": "ok", "syncToken": sync_token}


@app.delete("/api/releases/{rel_id}")
async def delete_release(rel_id: str, user: TelegramUser = Depends(require_admin)):
    """Удалить релиз + связанные рецензии и лайки — только Создатель."""
    sync_token = next_sync_token()
    await releases_col.delete_one({"id": rel_id})
    await reviews_col.delete_many({"relId": rel_id})
    await likes_col.delete_many({"releaseId": rel_id})
    await record_release_sync_event("release_deleted", rel_id, sync_token)
    return {"status": "ok", "syncToken": sync_token}


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
    data["author"] = user.display_name
    data["authorId"] = user.user_id
    data["authorUsername"] = user.username
    data["date"] = time.strftime("%d.%m.%Y")
    data["timestamp"] = time.time() * 1000
    try:
        await reviews_col.insert_one(data)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="User already reviewed this release")
    return {"status": "ok", "review": clean_doc(data)}


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

    await reviews_col.delete_one({"id": review_id})
    return {"status": "ok"}


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
    result = await reviews_col.delete_many({"authorUsername": target})
    return {"status": "ok", "deleted": result.deleted_count}


# ============================
# ПАРСЕР ССЫЛОК
# ============================
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
    """Basic SSRF protection: allow only http(s) and public IP targets."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.hostname
        if not host:
            return False
        if host.lower() in {"localhost"}:
            return False
        addr_info = socket.getaddrinfo(host, None)
        for info in addr_info:
            ip = ipaddress.ip_address(info[4][0])
            if any([
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            ]):
                return False
        return True
    except Exception:
        return False


def clean_ai_text(value, fallback: str, max_length: int = 120) -> str:
    raw_value = value if isinstance(value, str) and value.strip() else fallback
    cleaned = re.sub(r"\s+", " ", str(raw_value or "")).strip(" \t\r\n\"'`")
    if not cleaned:
        cleaned = str(fallback or "")
    return html.escape(cleaned[:max_length])


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
    title = re.sub(r"\s+", " ", raw_title or "").strip()
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

    host = urlparse(link).hostname or ""
    return {
        "artist": "Артист",
        "name": title or host.replace("www.", "") or "Релиз",
        "genre": "",
    }


def normalize_release_result(payload: dict, raw_title: str, link: str, detected_genre: str) -> dict:
    guessed = guess_release_from_title(raw_title, link)
    raw_genre = payload.get("genre", "") if isinstance(payload, dict) else ""
    genre = detected_genre or normalize_genre(raw_genre)
    return {
        "artist": clean_ai_text(payload.get("artist") if isinstance(payload, dict) else "", guessed["artist"]),
        "name": clean_ai_text(payload.get("name") if isinstance(payload, dict) else "", guessed["name"]),
        "genre": genre,
    }


def call_ai_extract_release(raw_title: str, link: str, detected_genre: str) -> dict:
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
                                + f". Remove platform names, marketing words, and quotes.{prompt_suffix}"
                            ),
                        },
                        {"role": "user", "content": raw_title or link},
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

    print(f"AI parsing fallback used due to error: {last_error}")
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

    raw_title, found_image, raw_genre = await get_metadata_from_page(req.link)

    detected_genre = normalize_genre(raw_genre)
    result = await ai_extract_release(raw_title, req.link, detected_genre)
    result["img"] = found_image
    if not detected_genre and result.get("genre"):
        detected_genre = normalize_genre(result["genre"])
    result["genre"] = detected_genre
    return result


@app.get("/api/health")
async def health():
    return {"status": "ok", "auth": bool(TELEGRAM_BOT_TOKEN)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
