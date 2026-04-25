from fastapi import APIRouter
from app.config import TELEGRAM_BOT_TOKEN

router = APIRouter()

@router.get("/api/health")
async def health():
    return {"status": "ok", "auth": bool(TELEGRAM_BOT_TOKEN)}
