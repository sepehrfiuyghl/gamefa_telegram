import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

import gamefa_miniapp_bot as bot

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"

app = FastAPI(title="Gamefa Mini App API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

AUTH_TTL = int(os.getenv("MINIAPP_AUTH_TTL", "86400"))


def validate_init_data(init_data: str):
    if not init_data:
        raise HTTPException(401, "Telegram authorization data is missing")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Invalid Telegram authorization data")
    auth_date = int(pairs.get("auth_date", "0"))
    if not auth_date or time.time() - auth_date > AUTH_TTL:
        raise HTTPException(401, "Telegram session expired")
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Telegram authorization failed")
    user = json.loads(pairs.get("user", "{}"))
    if int(user.get("id", 0)) != bot.ADMIN_ID:
        raise HTTPException(403, "Admin access required")
    return user


def auth_from_header(x_telegram_init_data: str | None):
    return validate_init_data(x_telegram_init_data or "")


class ProcessRequest(BaseModel):
    url: HttpUrl


class EditRequest(BaseModel):
    title: str
    body: str


class FakeStatus:
    async def edit_text(self, *args, **kwargs):
        return None
    async def delete(self):
        return None


class FakeMessage:
    def __init__(self, user_id: int):
        self.from_user = type("User", (), {"id": user_id})()
        self.messages = []

    async def answer(self, text="", **kwargs):
        self.messages.append({"type": "text", "text": text})
        return FakeStatus()

    async def answer_photo(self, photo=None, caption=None, **kwargs):
        self.messages.append({"type": "photo", "caption": caption or ""})
        return FakeStatus()


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "version": bot.BOT_VERSION}


@app.post("/api/auth")
async def auth(x_telegram_init_data: str | None = Header(default=None)):
    user = auth_from_header(x_telegram_init_data)
    return {"ok": True, "user": user, "version": bot.BOT_VERSION}


@app.get("/api/dashboard")
async def dashboard(x_telegram_init_data: str | None = Header(default=None)):
    auth_from_header(x_telegram_init_data)
    web_count = sum(1 for item in bot.memory if item.get("web_search_used"))
    return {
        "version": bot.BOT_VERSION,
        "archive": len(bot.memory),
        "capacity": bot.MAX_MEMORY,
        "model": bot.MODEL,
        "keys": len(bot.OPENAI_KEYS),
        "web_search": web_count,
        "queue": len(bot.news_queue),
        "stats": bot.editorial_stats,
        "processing": len(bot.processing_users),
    }


@app.get("/api/keys")
async def keys(x_telegram_init_data: str | None = Header(default=None)):
    auth_from_header(x_telegram_init_data)
    results = await bot.v56_health_check(force=False)
    return {"results": results}


@app.get("/api/archive")
async def archive(x_telegram_init_data: str | None = Header(default=None)):
    auth_from_header(x_telegram_init_data)
    items = []
    for i, item in enumerate(reversed(bot.memory[-100:])):
        items.append({
            "id": len(bot.memory) - 1 - i,
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "domain": item.get("domain", ""),
            "created_at": item.get("created_at", 0),
            "breaking": item.get("breaking", False),
            "spoiler": item.get("spoiler", ""),
            "quality": item.get("quality", {}),
            "verify": item.get("v57_verify", {}),
        })
    return {"items": items}


@app.post("/api/process")
async def process(req: ProcessRequest, x_telegram_init_data: str | None = Header(default=None)):
    user = auth_from_header(x_telegram_init_data)
    user_id = int(user["id"])
    if user_id in bot.processing_users:
        raise HTTPException(409, "A news item is already being processed")
    fake = FakeMessage(user_id)
    await bot.v57_process_news(fake, str(req.url))
    item = bot.prepared.get(user_id)
    if not item:
        error = next((m["text"] for m in reversed(fake.messages) if m["type"] == "text" and "خطا" in m["text"]), "Processing failed")
        raise HTTPException(422, error)
    return {
        "ok": True,
        "title": item.get("title", ""),
        "body": item.get("body", ""),
        "post": item.get("text", ""),
        "image": item.get("image", ""),
        "quality": item.get("quality", {}),
        "verify": item.get("v57_verify", {}),
        "ready": item.get("ready", False),
        "source": item.get("source", {}),
    }


@app.post("/api/edit")
async def edit(req: EditRequest, x_telegram_init_data: str | None = Header(default=None)):
    user = auth_from_header(x_telegram_init_data)
    item = bot.prepared.get(int(user["id"]))
    if not item:
        raise HTTPException(404, "No prepared news")
    item["title"] = req.title.strip()
    item["body"] = req.body.strip()
    item["text"] = bot.build_custom_post(item["title"], item["body"], item.get("source", {}), item.get("facts", {}))
    bot.editorial_stats["edits"] = int(bot.editorial_stats.get("edits", 0)) + 1
    bot.save_editorial_state()
    return {"ok": True, "title": item["title"], "body": item["body"], "post": item["text"]}


@app.post("/api/rewrite")
async def rewrite(x_telegram_init_data: str | None = Header(default=None)):
    user = auth_from_header(x_telegram_init_data)
    item = bot.prepared.get(int(user["id"]))
    if not item:
        raise HTTPException(404, "No prepared news")
    generated = await bot.rewrite_news_with_settings(
        item.get("source", {}), item.get("facts", {}), item.get("length", 7), item.get("mode", "standard")
    )
    title, sentences = bot.split_sentences(generated)
    item["title"] = title
    item["body"] = " ".join(sentences)
    item["text"] = bot.build_custom_post(title, item["body"], item.get("source", {}), item.get("facts", {}))
    bot.editorial_stats["rewrites"] = int(bot.editorial_stats.get("rewrites", 0)) + 1
    bot.save_editorial_state()
    return {"ok": True, "title": title, "body": item["body"], "post": item["text"]}


async def run_bot():
    await bot.main()


async def run_server():
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def runner():
    await asyncio.gather(run_bot(), run_server())


if __name__ == "__main__":
    asyncio.run(runner())
