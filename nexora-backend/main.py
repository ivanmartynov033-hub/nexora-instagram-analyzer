from datetime import datetime, timezone
import re
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="NEXORA Instagram Analyzer API",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    username: str

@app.get("/")
def home():
    return {"service": "NEXORA API", "status": "online"}

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.post("/api/analyze")
def analyze(data: AnalyzeRequest):
    username = data.username.strip().lstrip("@")

    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return {"status": "error", "message": "Некорректный username"}

    return {
        "username": username,
        "profile_url": f"https://www.instagram.com/{username}/",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "provider_not_configured",
        "message": "Источник публичных данных пока не подключён",
        "metrics": {}
    }