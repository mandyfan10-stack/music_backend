import json
import httpx
from app.security.ssrf import is_safe_public_url
from app.config import GROQ_API_KEY, GROQ_MODEL_PRIMARY, GROQ_MODEL_FALLBACKS, GROQ_MAX_RETRIES
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
try:
    from groq import Groq
except ImportError:
    Groq = None

client_ai = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY and Groq is not None else None

async def get_metadata_from_page(url: str):
    if not BeautifulSoup:
        return "", "", ""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        async with httpx.AsyncClient(follow_redirects=False, headers=headers, timeout=10.0) as h_client:
            current_url = url
            for _ in range(5):
                if not is_safe_public_url(current_url):
                    return "", "", ""
                res = await h_client.get(current_url)
                if res.is_redirect:
                    current_url = str(res.next_request.url)
                    continue
                res.raise_for_status()
                break
            else:
                return "", "", ""

            soup = BeautifulSoup(res.text, "html.parser")
            title = str(soup.title.string) if soup.title and soup.title.string else ""
            img = ""
            genre = ""

            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                img = str(og["content"])

            for prop in ["og:music:genre", "music:genre", "genre"]:
                tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                if tag and tag.get("content"):
                    genre = tag["content"].strip()
                    break

            if "music.yandex" in url:
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(script.string)
                        if isinstance(ld, dict):
                            g = ld.get("genre") or ld.get("@graph", [{}])[0].get("genre", "")
                            if g:
                                genre = g if isinstance(g, str) else ", ".join(g)
                                break
                    except Exception:
                        pass
                if not genre:
                    for a in soup.find_all("a", class_=lambda c: c and "genre" in c.lower() if c else False):
                        genre = a.get_text(strip=True)
                        if genre:
                            break

            if "spotify.com" in url and not genre:
                og_desc = soup.find("meta", property="og:description")
                if og_desc and og_desc.get("content"):
                    desc = og_desc["content"]
                    parts = desc.split("·")
                    if len(parts) >= 2:
                        candidate = parts[-1].strip().rstrip(".")
                        if len(candidate) < 30 and not candidate.isdigit():
                            genre = candidate

            return title.strip(), img, genre
    except Exception:
        return "", "", ""

def ai_extract_release(raw_title: str, link: str, detected_genre: str) -> dict:
    if not client_ai:
        return {"artist": "Артист", "name": "Релиз", "genre": detected_genre}

    models = [GROQ_MODEL_PRIMARY, *GROQ_MODEL_FALLBACKS]
    prompt_suffix = "" if detected_genre else " Also guess 'genre' from context."
    last_error = None

    for model_name in models:
        for _ in range(max(1, GROQ_MAX_RETRIES)):
            try:
                chat = client_ai.chat.completions.create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Return JSON with keys: artist, name"
                                + (", genre" if not detected_genre else "")
                                + f". Remove junk words.{prompt_suffix}"
                            ),
                        },
                        {"role": "user", "content": raw_title or link},
                    ],
                    response_format={"type": "json_object"},
                )
                return json.loads(chat.choices[0].message.content)
            except Exception as exc:
                last_error = exc
                continue

    print(f"AI parsing fallback used due to error: {last_error}")
    return {"artist": "Артист", "name": "Релиз", "genre": detected_genre}

GENRE_MAP = {
    "rap": "Рэп", "hip hop": "Хип-хоп", "hip-hop": "Хип-хоп", "hiphop": "Хип-хоп",
    "trap": "Трэп", "r&b": "R&B", "rnb": "R&B", "pop": "Поп",
    "rock": "Рок", "electronic": "Электронная", "edm": "Электронная",
    "jazz": "Джаз", "metal": "Метал",
    "рэп": "Рэп", "хип-хоп": "Хип-хоп", "трэп": "Трэп", "поп": "Поп",
    "рок": "Рок", "электронная": "Электронная", "джаз": "Джаз", "метал": "Метал",
    "indie": "Рок", "alternative": "Рок", "soul": "R&B",
    "drill": "Трэп", "phonk": "Трэп", "lo-fi": "Хип-хоп",
}

def normalize_genre(raw: str) -> str:
    if not raw:
        return ""
    low = raw.strip().lower()
    if low in GENRE_MAP:
        return GENRE_MAP[low]
    for key, val in GENRE_MAP.items():
        if key in low:
            return val
    return "Другое"
