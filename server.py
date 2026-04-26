import logging
import os
from typing import Any, Optional

import groq
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from groq import AsyncGroq
from pydantic import BaseModel, Field, field_validator

LOG_FORMAT = "%(levelname)s: %(message)s"
GROQ_API_KEY_ENV = "GROQ_API_KEY"
ALLOWED_ORIGINS_ENV = "ALLOWED_ORIGINS"

SYSTEM_PROMPT = (
    "Ты изящный и умный ИИ-репетитор по информатике (ОГЭ). "
    "Отвечай на русском языке кратко, дружелюбно, "
    "используй эмодзи по минимуму."
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "img-src 'self' data: fastapi.tiangolo.com;"
    ),
}

SERVICE_UNAVAILABLE_REPLY = "Ошибка сервера: Сервис временно недоступен."
RATE_LIMIT_REPLY = (
    "Упс! Кажется, нейросеть сейчас немного перегружена запросами ⏳ "
    "Пожалуйста, подожди несколько секунд и попробуй снова!"
)
TIMEOUT_REPLY = (
    "Превышено время ожидания ответа от ИИ. Пожалуйста, попробуй позже."
)
GENERIC_ERROR_REPLY = (
    "Произошла ошибка на сервере при обращении к ИИ. "
    "Пожалуйста, попробуй позже."
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


app = FastAPI()


def get_allowed_origins() -> list[str]:
    origins = os.getenv(ALLOWED_ORIGINS_ENV, "")
    return [origin.strip() for origin in origins.split(",") if origin.strip()]


def create_groq_client() -> Optional[AsyncGroq]:
    api_key = os.getenv(GROQ_API_KEY_ENV)

    if not api_key:
        logger.warning("Ключ %s не найден в Environment Variables.", GROQ_API_KEY_ENV)
        return None

    return AsyncGroq(api_key=api_key, timeout=10.0)


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)

    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value

    return response


client = create_groq_client()


class ChatRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="User's chat message",
    )

    @field_validator("text", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()

        return value


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    if not client:
        return {"reply": SERVICE_UNAVAILABLE_REPLY}

    try:
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Вопрос ученика: {req.text}"},
            ],
            temperature=0.7,
            max_tokens=1024,
        )

        return {"reply": response.choices[0].message.content}

    except groq.RateLimitError:
        return {"reply": RATE_LIMIT_REPLY}
    except groq.APITimeoutError:
        logger.exception("Превышено время ожидания от API Groq.")
        return {"reply": TIMEOUT_REPLY}
    except Exception:
        logger.exception("Ошибка при обращении к API Groq.")
        return {"reply": GENERIC_ERROR_REPLY}
