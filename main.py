from datetime import timezone
from urllib.parse import urlparse
import asyncio
import re
import time

import instaloader
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
    loader = instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )
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
    return {"service": "NEXORA Instagram Analyzer API", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest):
    try:
        username = clean_username(request.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    started = time.monotonic()
    try:
        result = await asyncio.to_thread(collect_profile, username)
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return result
    except instaloader.exceptions.ProfileNotExistsException:
        raise HTTPException(status_code=404, detail="Публичный профиль не найден.")
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise HTTPException(status_code=403, detail="Профиль закрытый. Доступны только публичные профили.")
    except instaloader.exceptions.InstaloaderException as exc:
        raise HTTPException(status_code=502, detail=f"Instagram не предоставил данные: {str(exc)[:240]}")
    except Exception:
        raise HTTPException(status_code=502, detail="Не удалось получить публичные данные Instagram. Возможно, Instagram временно ограничил запросы.")
