from fastapi import APIRouter, HTTPException, Depends, Request
from pymongo.errors import DuplicateKeyError
import time
from typing import Optional
from app.security.rate_limiter import rate_limiter
from app.db import releases_col, reviews_col, likes_col, blocked_col
from app.models import Release, Review, LikeReq, BlockReq, LinkRequest, TelegramUser
from app.security.telegram_auth import require_admin, check_not_blocked, get_current_user, get_optional_user

router = APIRouter()

def clean_doc(doc: dict) -> dict:
    if "_id" in doc:
        del doc["_id"]
    return doc

@router.post("/api/reviews")
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


@router.delete("/api/reviews/{review_id}")
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
