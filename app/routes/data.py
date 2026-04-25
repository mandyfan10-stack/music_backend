from fastapi import APIRouter, HTTPException, Depends, Request
from pymongo.errors import DuplicateKeyError
import time
from typing import Optional
from app.db import releases_col, reviews_col, likes_col, blocked_col
from app.models import Release, Review, LikeReq, BlockReq, LinkRequest, TelegramUser
from app.security.telegram_auth import require_admin, check_not_blocked, get_current_user, get_optional_user

router = APIRouter()


def clean_doc(doc: dict) -> dict:
    if "_id" in doc:
        del doc["_id"]
    return doc


# ============================
# API ЭНДПОИНТЫ
# ============================

@router.get("/api/data")
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
