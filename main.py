from collections import OrderedDict
from copy import deepcopy
from datetime import timezone
from threading import RLock
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import asyncio
import instaloader
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from urllib.parse import urlparse

class FirecrawlAuthError(Exception):
    """Firecrawl rejected the API key or request permissions."""

CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 200
_profile_cache = OrderedDict()
_cache_lock = RLock()

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

def collect_profile_firecrawl_scrape(username: str) -> dict:
    """Fallback to a live Firecrawl scrape when Oriane has no indexed profile."""
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not configured")

    schema = {
        "type": "object",
        "properties": {
            "username": {"type": "string"},
            "full_name": {"type": "string"},
            "biography": {"type": "string"},
            "followers": {"type": "integer"},
            "following": {"type": "integer"},
            "post_count": {"type": "integer"},
            "profile_url": {"type": "string"},
            "posts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "caption": {"type": "string"},
                        "date": {"type": "string"},
                        "likes": {"type": "integer"},
                        "comments": {"type": "integer"},
                        "views": {"type": ["integer", "null"]},
                        "type": {"type": "string"},
                        "url": {"type": "string"}
                    }
                }
            }
        },
        "required": ["username", "followers", "following", "post_count", "posts"]
    }
    payload = {
        "url": f"https://www.instagram.com/{username}/",
        "formats": ["json"],
        "jsonOptions": {
            "prompt": "Extract only the visible public Instagram profile information and recent public posts from this page. Do not guess missing values. Use numeric counts. Include up to 12 visible recent posts. If a field is unavailable, use an empty string, zero, or null as appropriate.",
            "schema": schema
        }
    }
    req = Request(
        "https://api.firecrawl.dev/v2/scrape",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise FirecrawlAuthError(f"Firecrawl вернул HTTP {exc.code}: проверьте API-ключ и права доступа.") from exc
        raise

    data = body.get("data") or {}
    extracted = data.get("json") or body.get("json") or {}
    if isinstance(extracted, dict) and isinstance(extracted.get("data"), dict):
        extracted = extracted["data"]
    if not isinstance(extracted, dict) or not extracted.get("username"):
        raise LookupError("Firecrawl scrape не смог извлечь публичные данные профиля.")

    handle = str(extracted.get("username") or username).lstrip("@")
    posts = extracted.get("posts") or []
    if not isinstance(posts, list):
        posts = []
    normalized_posts = []
    likes_total = 0
    comments_total = 0
    for post in posts[:12]:
        if not isinstance(post, dict):
            continue
        likes = int(post.get("likes") or 0)
        comments = int(post.get("comments") or 0)
        likes_total += likes
        comments_total += comments
        normalized_posts.append({
            "shortcode": "",
            "date": post.get("date") or "",
            "caption": post.get("caption") or "",
            "likes": likes,
            "comments": comments,
            "views": post.get("views"),
            "type": post.get("type") or "Публикация",
            "url": post.get("url") or "",
        })

    sampled = len(normalized_posts)
    followers = int(extracted.get("followers") or 0)
    avg_likes = round(likes_total / sampled, 2) if sampled else 0
    avg_comments = round(comments_total / sampled, 2) if sampled else 0
    engagement_rate = round(((avg_likes + avg_comments) / followers) * 100, 2) if followers else 0
    return {
        "status": "ok",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": 0,
        "data_source": "firecrawl_scrape",
        "notice": "Данные получены через Firecrawl Scrape из доступной публичной страницы Instagram. Доступность и полнота публикаций зависят от ответа Instagram и Firecrawl.",
        "account": {
            "username": handle,
            "full_name": extracted.get("full_name") or "",
            "biography": extracted.get("biography") or "",
            "followers": followers,
            "following": int(extracted.get("following") or 0),
            "post_count": int(extracted.get("post_count") or 0),
            "engagement_rate": engagement_rate,
            "avg_likes": avg_likes,
            "avg_comments": avg_comments,
            "sampled_posts": sampled,
            "profile_url": extracted.get("profile_url") or f"https://www.instagram.com/{handle}/",
        },
        "posts": normalized_posts,
    }


def collect_profile_firecrawl(username: str) -> dict:
    """Try the indexed provider first, then fall back to a normal Firecrawl scrape."""
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
    try:
        try:
            with urlopen(req, timeout=35) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise FirecrawlAuthError(f"Firecrawl вернул HTTP {exc.code}: проверьте API-ключ и права доступа.") from exc
            raise
        alexandria = (body.get("data") or {}).get("alexandria") or []
        if isinstance(alexandria, dict):
            alexandria = [alexandria]
        result = alexandria[0] if alexandria else {}
        result_data = result.get("data") or {}
        # Alexandria provider records are nested under data.data.results.
        provider_data = result_data.get("data") if isinstance(result_data, dict) else {}
        records = (
            (provider_data or {}).get("results")
            or result_data.get("results")
            or result.get("records")
            or []
        )
        if records:
            profile = records[0]
            handle = profile.get("handle") or username
            if profile.get("isPrivate") is True:
                raise PermissionError("Профиль закрытый. Доступны только публичные профили.")
            return {
                "status": "ok",
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_seconds": 0,
                "data_source": "firecrawl_index",
                "notice": "Данные получены из индексированного каталога Firecrawl, а не в реальном времени. Показатели могут отличаться от текущих. Индекс не предоставляет данные отдельных публикаций, поэтому ER и средние показатели не рассчитаны.",
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
    except (PermissionError, FirecrawlAuthError):
        raise
    except Exception:
        # A provider miss/error should not prevent trying the ordinary page scrape.
        pass

    return collect_profile_firecrawl_scrape(username)


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
    return {"service": "NEXORA Instagram Analyzer API", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/health")
def api_health():
    return {"status": "ok", "service": "NEXORA Instagram Analyzer API"}


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest):
    # Public mode intentionally uses Instaloader only; Firecrawl fallback is disabled.
    try:
        username = clean_username(request.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    started = time.monotonic()
    cached = _cache_get(username)
    if cached is not None:
        cached["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return cached

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
                raise HTTPException(
                    status_code=429,
                    detail="Instagram временно ограничил автоматические запросы (429). Резервный сбор отключён. Попробуйте позже.",
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
