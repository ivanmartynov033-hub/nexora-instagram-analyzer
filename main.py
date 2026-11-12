from collections import OrderedDict
from copy import deepcopy
from datetime import timezone
from threading import RLock
import os
import re
import time

import asyncio
import instaloader
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from urllib.parse import urlparse

CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 200
_profile_cache = OrderedDict()
_cache_lock = RLock()
_instagram_blocked_until = 0.0
INSTAGRAM_COOLDOWN_SECONDS = 600

def _cache_get(username: str):
    key = username.lower()
    now = time.monotonic()
    with _cache_lock:
        expired = [item for item, value in _profile_cache.items() if now - value["stored_at"] >= CACHE_TTL_SECONDS]
        for item in expired:
            _profile_cache.pop(item, None)
        entry = _profile_cache.get(key)
        if entry is None:
            return None
        _profile_cache.move_to_end(key)
        return deepcopy(entry["result"])

def _cache_set(username: str, result: dict):
    key = username.lower()
    now = time.monotonic()
    with _cache_lock:
        expired = [item for item, value in _profile_cache.items() if now - value["stored_at"] >= CACHE_TTL_SECONDS]
        for item in expired:
            _profile_cache.pop(item, None)
        _profile_cache[key] = {"stored_at": now, "result": deepcopy(result)}
        _profile_cache.move_to_end(key)
        while len(_profile_cache) > CACHE_MAX_ENTRIES:
            _profile_cache.popitem(last=False)

def _is_instagram_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    message = str(exc).lower()
    return status == 429 or "429" in message or "too many requests" in message or "rate limit" in message

def make_loader():
    return instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
    )

app = FastAPI(title="NEXORA Instagram Analyzer API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    username: str = Field(min_length=1, max_length=300)

def clean_username(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
            raise ValueError("Укажите ссылку на instagram.com или имя пользователя.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("В ссылке не найден username.")
        value = parts[0]
    value = value.lstrip("@").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value):
        raise ValueError("Некорректный username Instagram.")
    return value

def collect_profile(username: str) -> dict:
    loader = make_loader()
    profile = instaloader.Profile.from_username(loader.context, username)

    posts = []
    likes_total = 0
    comments_total = 0
    sampled = 0

    for post in profile.get_posts():
        if sampled >= 12:
            break
        likes = int(post.likes or 0)
        comments = int(post.comments or 0)
        likes_total += likes
        comments_total += comments
        sampled += 1
        published = post.date_utc.replace(tzinfo=timezone.utc).isoformat()
        posts.append({
            "shortcode": post.shortcode,
            "date": published,
            "caption": post.caption or "",
            "likes": likes,
            "comments": comments,
            "views": int(post.video_view_count) if post.is_video and post.video_view_count else None,
            "type": "Видео" if post.is_video else "Публикация",
            "url": f"https://www.instagram.com/p/{post.shortcode}/",
        })

    avg_likes = round(likes_total / sampled, 2) if sampled else 0
    avg_comments = round(comments_total / sampled, 2) if sampled else 0
    followers = int(profile.followers or 0)
    engagement_rate = round(((avg_likes + avg_comments) / followers) * 100, 2) if followers else 0

    return {
        "status": "ok",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": 0,
        "notice": "Получены доступные публичные данные. Instagram может ограничивать автоматические запросы; статистика публикаций рассчитана по последним доступным публикациям.",
        "account": {
            "username": profile.username,
            "full_name": profile.full_name or "",
            "biography": profile.biography or "",
            "followers": followers,
            "following": int(profile.followees or 0),
            "post_count": int(profile.mediacount or 0),
            "engagement_rate": engagement_rate,
            "avg_likes": avg_likes,
            "avg_comments": avg_comments,
            "sampled_posts": sampled,
            "profile_url": f"https://www.instagram.com/{profile.username}/",
        },
        "posts": posts,
    }

@app.get("/")
def root():
    # Serve the browser app at the Render root URL; API endpoints remain unchanged.
    index_path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(index_path, media_type="text/html")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/health")
def api_health():
    return {"status": "ok", "service": "NEXORA Instagram Analyzer API"}


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest):
    # Public mode uses Instaloader first and Firecrawl only as a fallback for Instagram rate limits (429).
    try:
        username = clean_username(request.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    global _instagram_blocked_until
    started = time.monotonic()
    cached = _cache_get(username)
    if cached is not None:
        cached["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return cached

    if time.monotonic() < _instagram_blocked_until:
        remaining = max(1, int(_instagram_blocked_until - time.monotonic()))
        raise HTTPException(
            status_code=429,
            detail=f"Instagram временно ограничил автоматические запросы. Повторная проверка будет доступна примерно через {remaining // 60 + 1} мин. Сторонние API-ключи не требуются.",
            headers={"Retry-After": str(remaining)},
        )

    try:
        try:
            result = await asyncio.to_thread(collect_profile, username)
            result["data_source"] = "instaloader"
        except instaloader.exceptions.ProfileNotExistsException:
            raise HTTPException(status_code=404, detail="Публичный профиль не найден.")
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            raise HTTPException(status_code=403, detail="Профиль закрытый. Доступны только публичные профили.")
        except instaloader.exceptions.InstaloaderException as instagram_exc:
            if _is_instagram_rate_limit(instagram_exc):
                _instagram_blocked_until = time.monotonic() + INSTAGRAM_COOLDOWN_SECONDS
                raise HTTPException(
                    status_code=429,
                    detail="Instagram временно ограничил автоматические запросы с сервера NEXORA (429). Приложение не использует сторонние API-ключи. Подождите около 10 минут перед следующей попыткой.",
                    headers={"Retry-After": str(INSTAGRAM_COOLDOWN_SECONDS)},
                )
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось получить публичные данные Instagram: {str(instagram_exc)[:200]}",
            )
        except Exception as instagram_exc:
            raise HTTPException(
                status_code=502,
                detail=f"Ошибка при получении данных Instagram: {str(instagram_exc)[:200]}",
            )

        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        _cache_set(username, result)
        return result
    except HTTPException:
        raise
