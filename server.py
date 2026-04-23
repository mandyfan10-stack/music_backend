from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Optional
import os
import html
import hmac
import hashlib
import json
import time
import httpx
import ipaddress
import socket
from urllib.parse import parse_qs
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
if not MONGO_URL:
    raise RuntimeError("MONGO_URL not set")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# Список admin-юзернеймов (через запятую, без @)
ADMIN_USERNAMES = set(
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "monetka_man,xllbloodxii").split(",")
    if u.strip()
)

client_db = AsyncIOMotorClient(MONGO_URL)
db = client_db["raper_xxii_database"]

releases_col = db["releases"]
reviews_col = db["reviews"]
likes_col = db["likes"]
blocked_col = db["blocked_users"]

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client_ai = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY and Groq is not None else None
GROQ_MODEL_PRIMARY = os.getenv("GROQ_MODEL_PRIMARY", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACKS = [
    m.strip() for m in os.getenv("GROQ_MODEL_FALLBACKS", "llama-3.1-8b-instant").split(",") if m.strip()
]
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "2"))


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

    auth_date = int(parsed.get("auth_date", ["0"])[0])
    if INIT_DATA_MAX_AGE > 0 and auth_date > 0:
        age = int(time.time()) - auth_date
        if age > INIT_DATA_MAX_AGE:
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
        username = (tg_user.get("username") or "").strip().lower()
        first_name = (tg_user.get("first_name") or "").strip()
    elif not TELEGRAM_BOT_TOKEN and DEV_MODE:
        # Dev-режим без Telegram: берём username из query/header
        dev_name = request.headers.get("X-Dev-Username", "").strip()
        if not dev_name:
            dev_name = request.query_params.get("username", "guest")
        clean = dev_name.replace("@", "").strip().lower()
        username = clean or "guest"
        first_name = dev_name
        user_id = hash(username) % 10**9
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
    except HTTPException:
        return None


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
@app.on_event("startup")
async def create_indexes():
    try:
        await releases_col.create_index("id", unique=True)
        await reviews_col.create_index("id", unique=True)
        await reviews_col.create_index("relId")
        await reviews_col.create_index("author")
        await likes_col.create_index([("releaseId", 1), ("userId", 1)], unique=True)
        await blocked_col.create_index("username", unique=True)
    except Exception as exc:
        print(f"Index warning: {exc}")


# ============================
# УТИЛИТЫ
# ============================
def clean_doc(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


# ============================
# API ЭНДПОИНТЫ
# ============================

@app.get("/api/data")
async def get_all_data(request: Request):
    """Получение каталога. Авторизация опциональна (гости видят каталог)."""
    tg_user = await get_optional_user(request)

    releases = await releases_col.find().sort("timestamp", -1).to_list(length=100)
    all_reviews = await reviews_col.find().sort("timestamp", -1).to_list(length=500)

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
    }


@app.post("/api/releases")
async def add_release(rel: Release, user: TelegramUser = Depends(require_admin)):
    """Добавить релиз — только Создатель."""
    data = rel.model_dump()
    data["createdBy"] = user.display_name
    data["createdById"] = user.user_id
    await releases_col.update_one({"id": rel.id}, {"$set": data}, upsert=True)
    return {"status": "ok"}


@app.delete("/api/releases/{rel_id}")
async def delete_release(rel_id: str, user: TelegramUser = Depends(require_admin)):
    """Удалить релиз + связанные рецензии и лайки — только Создатель."""
    await releases_col.delete_one({"id": rel_id})
    await reviews_col.delete_many({"relId": rel_id})
    await likes_col.delete_many({"releaseId": rel_id})
    return {"status": "ok"}


@app.post("/api/reviews")
async def add_review(rev: Review, user: TelegramUser = Depends(check_not_blocked)):
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
    await reviews_col.insert_one(data)
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
async def toggle_like(req: LikeReq, user: TelegramUser = Depends(check_not_blocked)):
    """Лайк/анлайк — только авторизованные, не заблокированные."""
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
    target = req.username.strip().lower().replace("@", "")
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
    target = username.strip().lower()
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
            title = str(soup.title.string) if soup.title and soup.title.string else ""
            img = ""
            genre = ""

            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                img = str(og["content"])

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
                    parts = desc.split("·")
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
            ]):
                return False
        return True
    except Exception:
        return False


def ai_extract_release(raw_title: str, link: str, detected_genre: str) -> dict:
    if not client_ai:
        return {"artist": "Артист", "name": "Релиз", "genre": detected_genre}

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
                                "Return JSON with keys: artist, name"
                                + (", genre" if not detected_genre else "")
                                + f". Remove junk words.{prompt_suffix}"
                            ),
                        },
                        {"role": "user", "content": raw_title or link},
                    ],
                    response_format={"type": "json_object"},
                )
                return json.loads(chat.choices[0].message.content)
            except Exception as exc:
                last_error = exc
                continue

    print(f"AI parsing fallback used due to error: {last_error}")
    return {"artist": "Артист", "name": "Релиз", "genre": detected_genre}


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
async def parse_link(req: LinkRequest, user: TelegramUser = Depends(require_admin)):
    """Распознавание ссылки — только Создатель. Возвращает artist, name, img, genre."""
    if not is_safe_public_url(req.link):
        raise HTTPException(400, "Unsafe or unsupported URL")

    raw_title, found_image, raw_genre = await get_metadata_from_page(req.link)

    detected_genre = normalize_genre(raw_genre)
    result = ai_extract_release(raw_title, req.link, detected_genre)
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
