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

@router.post("/api/releases")
async def add_release(rel: Release, user: TelegramUser = Depends(require_admin)):
    """Добавить релиз — только Создатель."""
    data = rel.model_dump()
    data["createdBy"] = user.display_name
    data["createdById"] = user.user_id
    await releases_col.update_one({"id": rel.id}, {"$set": data}, upsert=True)
    return {"status": "ok"}


@router.delete("/api/releases/{rel_id}")
async def delete_release(rel_id: str, user: TelegramUser = Depends(require_admin)):
    """Удалить релиз + связанные рецензии и лайки — только Создатель."""
    await releases_col.delete_one({"id": rel_id})
    await reviews_col.delete_many({"relId": rel_id})
    await likes_col.delete_many({"releaseId": rel_id})
    return {"status": "ok"}
