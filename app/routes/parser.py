from fastapi import APIRouter, HTTPException, Depends, Request
from pymongo.errors import DuplicateKeyError
import time
from typing import Optional
from app.security.rate_limiter import rate_limiter
from app.security.ssrf import is_safe_public_url
from app.services.parser import get_metadata_from_page, normalize_genre, ai_extract_release
from app.db import releases_col, reviews_col, likes_col, blocked_col
from app.models import Release, Review, LikeReq, BlockReq, LinkRequest, TelegramUser
from app.security.telegram_auth import require_admin, check_not_blocked, get_current_user, get_optional_user

router = APIRouter()

def clean_doc(doc: dict) -> dict:
    if "_id" in doc:
        del doc["_id"]
    return doc

@router.post("/api/parse_link")
async def parse_link(req: LinkRequest, user: TelegramUser = Depends(require_admin), _=Depends(rate_limiter)):
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
