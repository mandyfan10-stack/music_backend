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

@router.post("/api/block")
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


@router.delete("/api/reviews/by-author/{username}")
async def delete_all_reviews_by_author(username: str, admin: TelegramUser = Depends(require_admin)):
    """Удалить все рецензии пользователя — только Создатель."""
    target = username.strip().lower()
    result = await reviews_col.delete_many({"authorUsername": target})
    return {"status": "ok", "deleted": result.deleted_count}
