from datetime import timezone
import json
import os
from urllib.request import Request, urlopen

def collect_profile_firecrawl(username: str) -> dict:
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not configured")

    payload = {
        "alexandria": {
            "provider": "oriane",
            "capability": "instagram/profile-lookup",
            "options": {
                "url": f"https://www.instagram.com/{username}/",
                "projection": "basic"
            }
        }
    }
    req = Request(
        "https://api.firecrawl.dev/v2/scrape",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=35) as response:
        body = json.loads(response.read().decode("utf-8"))

    alexandria = (body.get("data") or {}).get("alexandria") or []
    if isinstance(alexandria, dict):
        alexandria = [alexandria]
    if not alexandria:
        raise RuntimeError("Firecrawl returned no Alexandria result")
    result = alexandria[0]
    if result.get("error"):
        raise RuntimeError("Firecrawl profile lookup failed")
    result_data = result.get("data") or {}
    records = result_data.get("results") or result.get("records") or []
    if not records:
        raise LookupError("Профиль не найден в индексе Firecrawl.")
    profile = records[0]
    handle = profile.get("handle") or username
    if profile.get("isPrivate") is True:
        raise PermissionError("Профиль закрытый. Доступны только публичные профили.")

    return {
        "status": "ok",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": 0,
        "data_source": "firecrawl_index",
        "notice": "Данные получены из индексированного каталога Firecrawl, а не в реальном времени. Показатели могут отличаться от текущих. Данные публикаций этим источником не предоставлены, поэтому ER и средние показатели не рассчитаны.",
        "account": {
            "username": handle,
            "full_name": profile.get("displayName") or "",
            "biography": profile.get("bio") or "",
            "followers": int(profile.get("followersCount") or 0),
            "following": int(profile.get("followingCount") or 0),
            "post_count": int(profile.get("postsCount") or 0),
            "engagement_rate": 0,
            "avg_likes": 0,
            "avg_comments": 0,
            "sampled_posts": 0,
            "profile_url": profile.get("profileUrl") or f"https://www.instagram.com/{handle}/",
        },
        "posts": [],
    }


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
        result["data_source"] = "instaloader"
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return result
    except instaloader.exceptions.ProfileNotExistsException:
        raise HTTPException(status_code=404, detail="Публичный профиль не найден.")
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise HTTPException(status_code=403, detail="Профиль закрытый. Доступны только публичные профили.")
    except instaloader.exceptions.InstaloaderException as exc:
        try:
            result = await asyncio.to_thread(collect_profile_firecrawl, username)
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            return result
        except PermissionError as fallback_exc:
            raise HTTPException(status_code=403, detail=str(fallback_exc))
        except LookupError:
            raise HTTPException(status_code=404, detail="Профиль не найден в публичных данных Instagram и индексе Firecrawl.")
        except Exception:
            raise HTTPException(status_code=502, detail=f"Instagram ограничил запрос, а резервный источник Firecrawl не смог вернуть профиль. Исходная ошибка: {str(exc)[:160]}")
    except Exception:
        try:
            result = await asyncio.to_thread(collect_profile_firecrawl, username)
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            return result
        except PermissionError as fallback_exc:
            raise HTTPException(status_code=403, detail=str(fallback_exc))
        except LookupError:
            raise HTTPException(status_code=404, detail="Профиль не найден в публичных данных Instagram и индексе Firecrawl.")
        except Exception:
            raise HTTPException(status_code=502, detail="Не удалось получить данные ни через Instagram, ни через резервный источник Firecrawl.")
