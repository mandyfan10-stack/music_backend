from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import httpx
from bs4 import BeautifulSoup
from groq import Groq
import json
from motor.motor_asyncio import AsyncIOMotorClient

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- НАСТРОЙКИ MONGODB ---
# Твоя ссылка (без угловых скобок!)
MONGO_URL = "mongodb+srv://mandyfan10_db_user:sergejkopo@cluster0.xixqzud.mongodb.net/?appName=Cluster0"
client_db = AsyncIOMotorClient(MONGO_URL)
db = client_db["raper_xxii_database"]

# Коллекции (Таблицы)
releases_col = db["releases"]
reviews_col = db["reviews"]
likes_col = db["likes"]

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client_ai = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# --- МОДЕЛИ ---
class LinkRequest(BaseModel):
    link: str

class Release(BaseModel):
    id: str
    name: str
    artist: str
    img: str
    link: str
    timestamp: float

class Review(BaseModel):
    id: str
    relId: str
    author: str
    text: str
    rating: int
    date: str
    timestamp: float

class LikeReq(BaseModel):
    releaseId: str
    username: str
    isLike: bool

# --- API ЭНДПОИНТЫ ДЛЯ РАБОТЫ С MONGODB ---

@app.get("/api/data")
async def get_all_data(username: str = ""):
    """Получение всех релизов, отзывов и лайков при входе"""
    releases = await releases_col.find().sort("timestamp", -1).to_list(length=100)
    reviews = await reviews_col.find().sort("timestamp", -1).to_list(length=500)
    
    # Убираем техническое поле _id от MongoDB, чтобы сайт не выдал ошибку
    for r in releases: r.pop("_id", None)
    for r in reviews: r.pop("_id", None)
    
    user_likes = []
    if username:
        likes = await likes_col.find({"username": username}).to_list(length=1000)
        user_likes = [l["releaseId"] for l in likes]
        
    return {"releases": releases, "reviews": reviews, "likes": user_likes}

@app.post("/api/releases")
async def add_release(rel: Release):
    """Добавление релиза"""
    await releases_col.update_one({"id": rel.id}, {"$set": rel.model_dump()}, upsert=True)
    return {"status": "ok"}

@app.delete("/api/releases/{rel_id}")
async def delete_release(rel_id: str):
    """Удаление релиза и всего, что с ним связано (для создателя)"""
    await releases_col.delete_one({"id": rel_id})
    await reviews_col.delete_many({"relId": rel_id})
    await likes_col.delete_many({"releaseId": rel_id})
    return {"status": "ok"}

@app.post("/api/reviews")
async def add_review(rev: Review):
    """Добавление отзыва"""
    await reviews_col.insert_one(rev.model_dump())
    return {"status": "ok"}

@app.post("/api/likes")
async def toggle_like(req: LikeReq):
    """Установка или снятие лайка"""
    if req.isLike:
        await likes_col.update_one(
            {"releaseId": req.releaseId, "username": req.username},
            {"$set": {"releaseId": req.releaseId, "username": req.username}},
            upsert=True
        )
    else:
        await likes_col.delete_one({"releaseId": req.releaseId, "username": req.username})
    return {"status": "ok"}

# --- ИИ ПАРСЕР ССЫЛОК ---
async def get_metadata_from_page(url: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers) as h_client:
            res = await h_client.get(url, timeout=10.0)
            soup = BeautifulSoup(res.text, 'html.parser')
            title = soup.title.string if soup.title else ""
            img = ""
            og_image = soup.find("meta", property="og:image")
            if og_image: img = og_image["content"]
            return title.strip(), img
    except: return "", ""

@app.post("/api/parse_link")
async def parse_link(req: LinkRequest):
    raw_title, found_image = await get_metadata_from_page(req.link)
    if not client_ai: return {"artist": "Артист", "name": "Релиз", "img": found_image}
    try:
        chat = client_ai.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "Return JSON: {'artist': '...', 'name': '...'}. Remove junk words."},
                      {"role": "user", "content": raw_title or req.link}],
            response_format={"type": "json_object"}
        )
        result = json.loads(chat.choices[0].message.content)
        result["img"] = found_image
        return result
    except: return {"artist": "Артист", "name": "Релиз", "img": found_image}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
