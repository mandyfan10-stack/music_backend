from motor.motor_asyncio import AsyncIOMotorClient
from app.config import MONGO_URL

client_db = AsyncIOMotorClient(MONGO_URL)
db = client_db["raper_xxii_database"]

releases_col = db["releases"]
reviews_col = db["reviews"]
likes_col = db["likes"]
blocked_col = db["blocked_users"]

async def create_indexes():
    try:
        await releases_col.create_index("id", unique=True)
        await reviews_col.create_index("id", unique=True)
        await reviews_col.create_index("relId")
        await reviews_col.create_index("author")
        await reviews_col.create_index([("relId", 1), ("authorId", 1)], unique=True)
        await likes_col.create_index([("releaseId", 1), ("userId", 1)], unique=True)
        await blocked_col.create_index("username", unique=True)
    except Exception as exc:
        print(f"Index warning: {exc}")
