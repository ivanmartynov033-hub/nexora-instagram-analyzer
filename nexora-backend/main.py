from datetime import datetime, timezone
from functools import lru_cache
import re
import time
from typing import Optional

import instaloader
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="NEXORA Instagram Analyzer API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Для публичного фронтенда GitHub Pages
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    username: str = Field(min_length=1, max_length=300)

def normalize_username(value: str) -> str:
    value = value.strip()
    match = re.search(r"(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", value, re.I)
    if match:
        value = match.group(1)
    value = value.lstrip("@").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value):
        raise HTTPException(status_code=422, detail="Некорректный Instagram username или ссылка.")
    return value

def make_loader():
    # Публичный запрос без логина и без передачи пароля Instagram.
    return instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=1,
    )

@app.get("/")
def home():
    return {"service": "NEXORA Instagram Analyzer API", "status": "online", "mode": "public_best_effort"}

@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "public_best_effort"}

@app.post("/api/analyze")
def analyze(request: AnalyzeRequest):
    username = normalize_username(request.username)
    started = time.time()
    try:
        loader = make_loader()
        profile = instaloader.Profile.from_username(loader.context, username)

        if profile.is_private:
            raise HTTPException(
                status_code=403,
                detail="Профиль закрытый. Публичный режим не анализирует закрытые аккаунты."
            )

        post_rows = []
        likes_values = []
        comments_values = []
        # Берём до 12 последних публикаций. Instagram может ограничить доступ к ленте.
        for post in profile.get_posts():
            if len(post_rows) >= 12:
                break
            likes = int(post.likes or 0)
            comments = int(post.comments or 0)
            views = getattr(post, "video_view_count", None)
            likes_values.append(likes)
            comments_values.append(comments)
            post_rows.append({
                "username": username,
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "date": post.date_utc.replace(tzinfo=timezone.utc).isoformat(),
                "type": "Видео" if post.is_video else "Фото/карусель",
                "likes": likes,
                "comments": comments,
                "views": int(views) if views is not None else None,
                "caption": (post.caption or "")[:500],
            })

        avg_likes = round(sum(likes_values) / len(likes_values), 2) if likes_values else 0
        avg_comments = round(sum(comments_values) / len(comments_values), 2) if comments_values else 0
        followers = int(profile.followers or 0)
        engagement_rate = round(((avg_likes + avg_comments) / followers) * 100, 2) if followers else 0

        account = {
            "username": profile.username,
            "full_name": profile.full_name or profile.username,
            "profile_url": f"https://www.instagram.com/{profile.username}/",
            "profile_pic_url": profile.profile_pic_url,
            "biography": profile.biography or "",
            "is_private": bool(profile.is_private),
            "is_verified": bool(profile.is_verified),
            "followers": followers,
            "following": int(profile.followees or 0),
            "post_count": int(profile.mediacount or 0),
            "avg_likes": avg_likes,
            "avg_comments": avg_comments,
            "engagement_rate": engagement_rate,
            "sampled_posts": len(post_rows),
        }
        return {
            "status": "ok",
            "username": profile.username,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 2),
            "account": account,
            "posts": post_rows,
            "notice": "Показатели активности рассчитаны по выборке последних доступных публикаций."
        }

    except HTTPException:
        raise
    except instaloader.exceptions.ProfileNotExistsException:
        raise HTTPException(status_code=404, detail="Профиль не найден или username указан неверно.")
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise HTTPException(status_code=403, detail="Профиль закрытый и недоступен для публичного анализа.")
    except instaloader.exceptions.LoginRequiredException:
        raise HTTPException(
            status_code=503,
            detail="Instagram потребовал авторизацию для этого запроса. В этой версии вход и пароль не используются; попробуйте позже или другой публичный профиль."
        )
    except instaloader.exceptions.TooManyRequestsException:
        raise HTTPException(status_code=429, detail="Instagram временно ограничил запросы. Подождите и попробуйте позже.")
    except instaloader.exceptions.ConnectionException as exc:
        raise HTTPException(status_code=502, detail=f"Instagram не отдал публичные данные: {str(exc)[:220]}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка получения публичных данных: {str(exc)[:220]}")
