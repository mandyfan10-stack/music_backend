from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import httpx
from bs4 import BeautifulSoup
from groq import Groq
import json

app = FastAPI()

# Разрешаем запросы с фронтенда (Telegram Web App)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class LinkRequest(BaseModel):
    link: str

async def fetch_page_title(url: str) -> str:
    """Асинхронно получает заголовок страницы (title) по URL"""
    try:
        # Притворяемся браузером, чтобы избежать блокировок от Spotify/YouTube
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            response = await http_client.get(url, headers=headers, timeout=5.0)
            soup = BeautifulSoup(response.text, 'html.parser')
            title = soup.title.string if soup.title else ""
            return title.strip()
    except Exception as e:
        print(f"Error fetching page title: {e}")
        return ""

@app.post("/api/parse_link")
async def parse_link_endpoint(req: LinkRequest):
    if not client:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY не настроен на сервере.")
    
    url = req.link.lower()
    
    # 1. Жесткая проверка: только YouTube и Spotify
    if not ("youtube.com" in url or "youtu.be" in url or "spotify.com" in url):
        raise HTTPException(status_code=400, detail="Разрешены ссылки только с YouTube и Spotify.")
    
    # 2. Получаем заголовок страницы по ссылке
    page_title = await fetch_page_title(req.link)
    
    if not page_title:
        # Если не смогли получить title, попробуем передать просто ссылку
        page_title = f"URL: {req.link}"

    try:
        # 3. Отправляем LLaMA задачу вычленить артиста и название
        system_prompt = """
        Ты - строгий музыкальный парсер. Тебе дают заголовок веб-страницы (YouTube или Spotify).
        Твоя задача извлечь имя артиста и название трека/альбома.
        Убери лишние слова вроде "YouTube", "Spotify", "Official Video", "Lyrics" и т.д.
        Верни СТРОГО валидный JSON формат, без какого-либо дополнительного текста, markdown или пояснений.
        Формат: {"artist": "Имя", "name": "Название"}
        Если не можешь определить, верни {"artist": "Аноним", "name": "Неизвестный релиз"}.
        """
        
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Страница: {page_title}"}
            ],
            temperature=0.1,
            max_tokens=200,
        )
        
        ai_text = response.choices[0].message.content.strip()
        
        # Очистка возможных markdown артефактов
        ai_text = ai_text.replace('```json', '').replace('```', '').strip()
        
        parsed_data = json.loads(ai_text)
        return parsed_data
        
    except Exception as e:
        print(f"LLaMA Error: {e}")
        raise HTTPException(status_code=500, detail="Ошибка обработки нейросетью.")
