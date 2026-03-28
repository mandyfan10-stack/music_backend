from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import httpx
from bs4 import BeautifulSoup
from groq import Groq
import json
import re

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

class LinkRequest(BaseModel):
    link: str

async def get_metadata_from_page(url: str):
    """Скрапинг страницы для получения заголовка и обложки"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers) as h_client:
            response = await h_client.get(url, timeout=10.0)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Ищем заголовок
            title = soup.title.string if soup.title else ""
            
            # Ищем обложку (OpenGraph теги)
            image = ""
            og_image = soup.find("meta", property="og:image")
            if og_image:
                image = og_image["content"]
            
            return title.strip(), image
    except Exception as e:
        print(f"Scraping error: {e}")
        return "", ""

@app.post("/api/parse_link")
async def parse_link(req: LinkRequest):
    if not client:
        raise HTTPException(status_code=500, detail="Сервер не настроен (API KEY).")
    
    url = req.link
    
    # Получаем сырые данные со страницы
    raw_title, found_image = await get_metadata_from_page(url)
    
    if not raw_title:
        raw_title = f"Ссылка: {url}"

    try:
        # Просим LLaMA очистить название
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
        
        # Если обложка не найдена скрапером, возвращаем пустую строку (фронтенд сам найдет в iTunes)
        result["img"] = found_image
        return result
        
    except Exception as e:
        print(f"AI Error: {e}")
        return {"artist": "Артист", "name": "Релиз", "img": found_image}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
