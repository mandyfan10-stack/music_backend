import html
from pydantic import BaseModel, Field, field_validator

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

class TelegramUser:
    """Авторизованный пользователь из Telegram initData"""
    def __init__(self, user_id: int, username: str, first_name: str, is_admin: bool):
        self.user_id = user_id
        self.username = html.escape(username) if username else ""  # без @, lowercase
        self.first_name = html.escape(first_name) if first_name else ""
        self.is_admin = is_admin
        self.display_name = f"@{self.username}" if self.username else self.first_name or f"user-{self.user_id}"
