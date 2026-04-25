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

@router.post("/api/likes")
async def toggle_like(req: LikeReq, user: TelegramUser = Depends(check_not_blocked), _=Depends(rate_limiter)):
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
