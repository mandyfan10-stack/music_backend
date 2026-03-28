from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import httpx
from bs4 import BeautifulSoup
from groq import Groq
import json
import sqlite3

app = FastAPI()

# Разрешаем запросы с фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

DB_FILE = "database.db"

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ SQLite ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Таблица релизов
    c.execute('''CREATE TABLE IF NOT EXISTS releases (id TEXT PRIMARY KEY, name TEXT, artist TEXT, img TEXT, link TEXT)''')
    # Таблица рецензий
    c.execute('''CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, relId TEXT, author TEXT, text TEXT, rating INTEGER, date TEXT)''')
    # Таблица лайков
    c.execute('''CREATE TABLE IF NOT EXISTS likes (releaseId TEXT, username TEXT, PRIMARY KEY(releaseId, username))''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# --- МОДЕЛИ ДАННЫХ (Pydantic) ---
class LinkRequest(BaseModel):
    link: str

class Release(BaseModel):
    id: str
    name: str
    artist: str
    img: str
    link: str

class Review(BaseModel):
    id: str
    relId: str
    author: str
    text: str
    rating: int
    date: str

class LikeReq(BaseModel):
    releaseId: str
    username: str
    isLike: bool

# --- API ЭНДПОИНТЫ ДЛЯ РАБОТЫ С БД ---

@app.get("/api/data")
def get_all_data(username: str = ""):
    """Возвращает всё содержимое базы при запуске приложения"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM releases ORDER BY id DESC")
    releases = [dict(row) for row in c.fetchall()]
    
    c.execute("SELECT * FROM reviews ORDER BY id DESC")
    reviews = [dict(row) for row in c.fetchall()]
    
    likes = []
    if username:
        c.execute("SELECT releaseId FROM likes WHERE username=?", (username,))
        likes = [row["releaseId"] for row in c.fetchall()]
        
    conn.close()
    return {"releases": releases, "reviews": reviews, "likes": likes}

@app.post("/api/releases")
def add_release(rel: Release):
    """Сохранение нового релиза"""
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO releases (id, name, artist, img, link) VALUES (?,?,?,?,?)",
                  (rel.id, rel.name, rel.artist, rel.img, rel.link))
        conn.commit()
    except sqlite3.IntegrityError:
        pass # Если такой ID уже есть
    conn.close()
    return {"status": "ok"}

@app.delete("/api/releases/{rel_id}")
def delete_release(rel_id: str):
    """Удаление релиза создателем"""
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM releases WHERE id=?", (rel_id,))
    c.execute("DELETE FROM reviews WHERE relId=?", (rel_id,))
    c.execute("DELETE FROM likes WHERE releaseId=?", (rel_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/reviews")
def add_review(rev: Review):
    """Добавление рецензии"""
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO reviews (id, relId, author, text, rating, date) VALUES (?,?,?,?,?,?)",
              (rev.id, rev.relId, rev.author, rev.text, rev.rating, rev.date))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/likes")
def toggle_like(req: LikeReq):
    """Ставим или убираем лайк"""
    conn = get_db()
    c = conn.cursor()
    if req.isLike:
        c.execute("INSERT OR IGNORE INTO likes (releaseId, username) VALUES (?,?)", (req.releaseId, req.username))
    else:
        c.execute("DELETE FROM likes WHERE releaseId=? AND username=?", (req.releaseId, req.username))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# --- ИИ ПАРСЕР (Ваш старый код) ---
async def get_metadata_from_page(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers) as h_client:
            response = await h_client.get(url, timeout=10.0)
            soup = BeautifulSoup(response.text, 'html.parser')
            title = soup.title.string if soup.title else ""
            image = ""
            og_image = soup.find("meta", property="og:image")
            if og_image: image = og_image["content"]
            return title.strip(), image
    except Exception as e:
        return "", ""

@app.post("/api/parse_link")
async def parse_link(req: LinkRequest):
    if not client: raise HTTPException(status_code=500, detail="Сервер не настроен (API KEY).")
    raw_title, found_image = await get_metadata_from_page(req.link)
    if not raw_title: raw_title = f"Ссылка: {req.link}"

    try:
        system_prompt = """
        Ты - музыкальный парсер. Твоя цель: извлечь АРТИСТА и НАЗВАНИЕ трека/альбома из текста.
        Убери мусор: 'Яндекс Музыка', 'YouTube', 'Слушать онлайн', 'Official Video', 'Lyrics'.
        Верни ТОЛЬКО JSON: {"artist": "...", "name": "..."}.
        Если не нашел, используй "Артист" и "Трек".
        """
        chat_completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Текст для анализа: {raw_title}"}
            ],
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        result = json.loads(chat_completion.choices[0].message.content)
        result["img"] = found_image
        return result
    except Exception as e:
        return {"artist": "Артист", "name": "Релиз", "img": found_image}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
