import os
import re
import json
import html
import asyncio
import logging
import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin
from collections import deque
from datetime import datetime, timezone
from urllib.robotparser import RobotFileParser

import aiohttp
from bs4 import BeautifulSoup

try:
    from PIL import Image
except Exception:
    Image = None

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from openai import AsyncOpenAI


# ============================================================
# GAMEFA BOT v5.2.0
# ============================================================
# امکانات:
# - پشتیبانی از لینک Gamefa و سایت‌های خبری دیگر
# - استخراج چندمرحله‌ای با aiohttp + BeautifulSoup
# - fallback اختیاری OpenAI Web Search
# - استخراج تصویر og:image / twitter:image / article image
# - تولید تیتر فارسی + دقیقاً 7 جمله در یک پاراگراف
# - تشخیص خبر تکراری
# - 5 کلید OpenAI با failover
# - آرشیو JSON
# - Railway friendly
# ============================================================

BOT_VERSION = "v5.3.1"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@Gamefa_official").strip()

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip()
AI_SOURCE_LIMIT = int(os.getenv("AI_SOURCE_LIMIT", "18000"))
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "1400"))

# اگر 1 باشد، در صورت شکست استخراج معمولی، OpenAI Web Search را امتحان می‌کند.
ENABLE_WEB_SEARCH_FALLBACK = os.getenv(
    "ENABLE_WEB_SEARCH_FALLBACK", "1"
).strip().lower() in ("1", "true", "yes", "on")

MAX_MEMORY = int(os.getenv("MAX_MEMORY", "1500"))
MEMORY_FILE = Path(os.getenv("MEMORY_FILE", "news_memory.json"))
IMAGE_DIR = Path(os.getenv("IMAGE_DIR", "gamefa_images"))
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# V5.3 ADVANCED EDITORIAL SETTINGS
# ============================================================
# ایده‌های 7 تا 20:
# 7  multi-source research / source discovery
# 8  breaking news
# 9  writing modes
# 10 configurable length
# 11 automatic hashtags
# 12 spoiler detection
# 13 processing queue
# 14 async-friendly processing
# 15 editorial dashboard
# 16 structured logs
# 17 OpenAI key health
# 18 manual approval before publishing
# 19 edit title/body/image/rewrite
# 20 learning from admin corrections

ENABLE_MULTI_SOURCE = os.getenv("ENABLE_MULTI_SOURCE", "0").strip().lower() in ("1", "true", "yes", "on")
ENABLE_HASHTAGS = False  # هشتگ‌ها عمداً در v5.3.1 غیرفعال هستند.
ENABLE_SPOILER_DETECTION = os.getenv("ENABLE_SPOILER_DETECTION", "1").strip().lower() in ("1", "true", "yes", "on")
BREAKING_THRESHOLD = float(os.getenv("BREAKING_THRESHOLD", "0.82"))
QUEUE_ENABLED = os.getenv("QUEUE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
NEWS_LENGTH = os.getenv("NEWS_LENGTH", "7").strip()
WRITING_MODE = os.getenv("WRITING_MODE", "standard").strip().lower()
LEARNING_FILE = Path(os.getenv("LEARNING_FILE", "editorial_learning.json"))
STATS_FILE = Path(os.getenv("STATS_FILE", "editorial_stats.json"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "20"))

news_queue = deque()
queue_worker_task = None
queue_lock = asyncio.Lock()
queue_waiters = {}
editorial_stats = {
    "processed": 0, "published": 0, "duplicates": 0, "failed": 0,
    "images_ok": 0, "images_failed": 0, "breaking": 0, "spoilers": 0,
    "web_search": 0, "multi_source": 0, "edits": 0, "rewrites": 0,
    "hashtags": 0, "queue_max": 0, "publishing_disabled": 1, "mode_button_disabled": 1
}
editorial_learning = []

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except Exception:
    ADMIN_ID = 0

# ------------------------------------------------------------
# OpenAI keys
# ------------------------------------------------------------

OPENAI_KEYS = []

for i in range(1, 6):
    key = os.getenv(f"OPENAI_API_KEY_{i}", "").strip()
    if key:
        OPENAI_KEYS.append(key)

legacy_key = os.getenv("OPENAI_API_KEY", "").strip()
if legacy_key and legacy_key not in OPENAI_KEYS:
    OPENAI_KEYS.insert(0, legacy_key)

OPENAI_CLIENTS = {}
OPENAI_KEY_INDEX = 0
OPENAI_KEY_COOLDOWN = {}

memory = []
prepared = {}
processing_users = set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("gamefa_bot")


# ============================================================
# OPENAI
# ============================================================

def get_openai_client(index: int):
    if index not in OPENAI_CLIENTS:
        OPENAI_CLIENTS[index] = AsyncOpenAI(
            api_key=OPENAI_KEYS[index]
        )
    return OPENAI_CLIENTS[index]


def openai_retry_seconds(error):
    text = str(error or "")

    patterns = [
        r"try again in\s+(\d+)h(\d+)m([\d.]+)s",
        r"try again in\s+(\d+)m([\d.]+)s",
        r"try again in\s+([\d.]+)s",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue

        groups = match.groups()

        if len(groups) == 3:
            return (
                int(groups[0]) * 3600
                + int(groups[1]) * 60
                + float(groups[2])
            )

        if len(groups) == 2:
            return int(groups[0]) * 60 + float(groups[1])

        return float(groups[0])

    return 60.0


def openai_is_retryable(error):
    text = str(error or "").lower()
    status = getattr(error, "status_code", None)

    retry_words = (
        "429",
        "rate limit",
        "rate_limit",
        "tokens per min",
        "tpm",
        "quota",
        "too many requests",
        "insufficient_quota",
        "temporarily unavailable",
        "timeout",
        "timed out",
        "connection",
        "server error",
        "internal server error",
    )

    return status in (408, 409, 429, 500, 502, 503, 504) or any(
        word in text for word in retry_words
    )


async def openai_failover(callback):
    global OPENAI_KEY_INDEX

    if not OPENAI_KEYS:
        raise RuntimeError("هیچ کلید OpenAI تنظیم نشده است.")

    last_error = None
    total_keys = len(OPENAI_KEYS)

    for offset in range(total_keys):
        index = (OPENAI_KEY_INDEX + offset) % total_keys

        if OPENAI_KEY_COOLDOWN.get(index, 0) > time.time():
            continue

        try:
            client = get_openai_client(index)
            result = await callback(client)

            OPENAI_KEY_INDEX = (index + 1) % total_keys
            OPENAI_KEY_COOLDOWN.pop(index, None)
            return result

        except Exception as error:
            last_error = error

            if not openai_is_retryable(error):
                raise

            wait = openai_retry_seconds(error)
            OPENAI_KEY_COOLDOWN[index] = time.time() + min(
                max(wait, 30), 1800
            )

            log.warning(
                "OpenAI key #%s unavailable; trying another key.",
                index + 1,
            )

    raise RuntimeError(
        "تمام کلیدهای OpenAI فعلاً محدود، نامعتبر یا در دسترس نیستند."
    ) from last_error


# ============================================================
# MEMORY
# ============================================================

def load_memory():
    global memory

    try:
        if not MEMORY_FILE.exists():
            memory = []
            return

        data = json.loads(
            MEMORY_FILE.read_text(encoding="utf-8")
        )

        memory = data[-MAX_MEMORY:] if isinstance(data, list) else []

    except Exception as error:
        log.warning("Memory load error: %s", error)
        memory = []


def save_memory():
    try:
        MEMORY_FILE.write_text(
            json.dumps(
                memory[-MAX_MEMORY:],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as error:
        log.warning("Memory save error: %s", error)


# ============================================================
# TEXT
# ============================================================

def norm(text):
    text = text or ""
    text = re.sub(r"https?://\S+", " ", text)
    text = text.lower()
    text = re.sub(r"[^\w\u0600-\u06FF\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def word_similarity(a, b):
    words_a = set(norm(a).split())
    words_b = set(norm(b).split())

    if not words_a or not words_b:
        return 0.0

    return len(words_a & words_b) / len(words_a | words_b)


def text_hash(text):
    return hashlib.sha256(
        norm(text).encode("utf-8")
    ).hexdigest()


def duplicate(text, title=""):
    new_hash = text_hash(text)

    for item in memory:
        if item.get("hash", "") == new_hash:
            return True

        old_title = item.get("title", "")
        old_source = item.get("source", "")

        if title and old_title:
            if word_similarity(title, old_title) >= 0.88:
                return True

        if old_source:
            if word_similarity(text, old_source) >= 0.84:
                return True

    return False


def extract_url(text):
    if not text:
        return None

    match = re.search(
        r"https?://[^\s<>()]+",
        text,
        re.I,
    )

    if not match:
        return None

    return match.group(0).rstrip(".,)]}")


def escape_html(text):
    return html.escape(text or "", quote=False)


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


# ============================================================
# ADMIN
# ============================================================

def is_admin(message):
    return bool(
        ADMIN_ID
        and message.from_user
        and message.from_user.id == ADMIN_ID
    )


def is_admin_id(user_id):
    return bool(ADMIN_ID and user_id == ADMIN_ID)


# ============================================================
# PERSIAN
# ============================================================

PERSIAN_RE = re.compile(r"[\u0600-\u06FF]")


def starts_with_persian(text):
    if not text:
        return False

    text = text.strip()

    text = re.sub(
        r"^[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫\s\-–—•]+",
        "",
        text,
    ).strip()

    return bool(text and PERSIAN_RE.match(text[0]))


def ensure_persian_start(text, is_title=False):
    if not text:
        return text

    text = text.strip()

    if starts_with_persian(text):
        return text

    if is_title:
        return "گزارش جدید درباره " + text

    return "براساس گزارش منتشرشده، " + text


def strip_site_branding_from_title(text):
    if not text:
        return ""

    text = str(text).strip()

    patterns = [
        r"\s*[|｜]\s*گیمفا\s*$",
        r"\s*[–—-]\s*گیمفا\s*$",
        r"^\s*گیمفا\s*[|｜:：-]\s*",
        r"\s*\(\s*گیمفا\s*\)\s*$",
        r"\s+گیمفا\s*$",
    ]

    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.I)

    return text.strip(" |｜–—-:：")


# ============================================================
# CATEGORY
# ============================================================

def detect_category(text):
    text_lower = (text or "").lower()

    game_words = [
        "بازی", "گیم", "game", "gaming", "playstation",
        "xbox", "nintendo", "steam", "ps5", "ps4",
        "xbox series", "switch", "gta", "halo",
        "elden ring", "resident evil", "assassin",
    ]

    movie_words = [
        "فیلم", "سریال", "بازیگر", "movie", "film",
        "series", "season", "actor", "actress",
        "netflix", "hbo", "disney", "marvel", "dc",
        "cinema",
    ]

    if any(x in text_lower for x in game_words):
        return "🎮"

    if any(x in text_lower for x in movie_words):
        return "🎬"

    return "📢"


# ============================================================
# AI CLEAN
# ============================================================

def clean_ai_text(text):
    text = text or ""

    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    forbidden = [
        r"(?im)^\s*امتیاز دقت.*$",
        r"(?im)^\s*امتیاز ai.*$",
        r"(?im)^\s*accuracy score.*$",
        r"(?im)^\s*reviewer.*$",
        r"(?im)^\s*اطلاعات استخراج شده.*$",
        r"(?im)^\s*طبق بررسی ai.*$",
        r"(?im)^\s*هوش مصنوعی.*$",
    ]

    for pattern in forbidden:
        text = re.sub(pattern, "", text)

    text = re.sub(
        r"(?im)^\s*(?:🆔\s*)?@Gamefa_official\s*$",
        "",
        text,
    )

    text = re.sub(
        r"(?m)^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]\s*",
        "",
        text,
    )

    return text.strip()


# ============================================================
# GENERIC ARTICLE EXTRACTION
# ============================================================

REMOVE_SELECTORS = [
    "script", "style", "noscript", "svg", "nav", "footer",
    "form", "aside", "header", "iframe", "video", "audio",
    "canvas", ".related-posts", ".related-post", ".related",
    ".recommended", ".recommendations", ".recommended-posts",
    ".more-posts", ".latest-posts", ".popular-posts",
    ".author-box", ".author-info", ".author-card", ".comments",
    ".comment", ".comment-list", ".advertisement", ".ads",
    ".ad", ".banner", ".newsletter", ".social-share",
    ".share-buttons", ".breadcrumb", ".breadcrumbs", ".sidebar",
    ".widget", ".read-more", ".post-navigation", ".navigation",
]


def remove_unwanted_elements(soup):
    for selector in REMOVE_SELECTORS:
        try:
            for element in soup.select(selector):
                element.decompose()
        except Exception:
            pass


def is_probably_noise(text):
    low = (text or "").lower()

    words = [
        "مطالب مرتبط", "مطالب پیشنهادی", "اخبار مرتبط",
        "بیشتر بخوانید", "related posts", "related articles",
        "recommended", "subscribe", "newsletter", "تبلیغات",
        "advertisement", "نویسنده", "author", "دیدگاه",
        "comments", "comment", "share",
    ]

    return any(word in low for word in words)


def valid_article_url(url):
    parsed = urlparse(url)

    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.netloc)
    )


def pick_meta(soup, candidates):
    for attrs in candidates:
        meta = soup.find("meta", attrs=attrs)

        if meta and meta.get("content"):
            value = clean_text(meta["content"])
            if value:
                return value

    return ""


def extract_jsonld_article(soup):
    candidates = []

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"application/ld\+json", re.I)},
    ):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list):
            candidates.extend(x for x in data if isinstance(x, dict))

    for item in candidates:
        item_type = item.get("@type", "")
        types = item_type if isinstance(item_type, list) else [item_type]

        if any(
            str(x).lower() in (
                "article",
                "newsarticle",
                "reportagenewsarticle",
                "blogposting",
            )
            for x in types
        ):
            return item

    return {}


async def fetch_generic(url):
    if not valid_article_url(url):
        raise ValueError("لینک واردشده معتبر نیست.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }

    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:
        async with session.get(
            url,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()

            final_url = str(response.url)
            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if "html" not in content_type:
                raise ValueError(
                    "این لینک یک صفحه HTML خبری نیست."
                )

            raw = await response.text(errors="ignore")

    soup = BeautifulSoup(raw, "html.parser")
    jsonld = extract_jsonld_article(soup)

    # متادیتا را قبل از پاک‌سازی می‌گیریم.
    title = pick_meta(
        soup,
        [
            {"property": "og:title"},
            {"name": "twitter:title"},
        ],
    )

    h1 = soup.find("h1")
    if h1:
        h1_text = clean_text(h1.get_text(" ", strip=True))
        if len(h1_text) > 10:
            title = h1_text

    if not title:
        title = clean_text(
            jsonld.get("headline", "")
        )

    if not title and soup.title:
        title = clean_text(
            soup.title.get_text(" ", strip=True)
        )

    description = pick_meta(
        soup,
        [
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        ],
    )

    if not description:
        description = clean_text(
            jsonld.get("description", "")
        )

    image_candidates = []

    for attrs in [
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"},
    ]:
        value = pick_meta(soup, [attrs])
        if value:
            image_candidates.append(
                urljoin(final_url, value)
            )

    jsonld_image = jsonld.get("image")
    if isinstance(jsonld_image, str):
        image_candidates.append(
            urljoin(final_url, jsonld_image)
        )
    elif isinstance(jsonld_image, list):
        image_candidates.extend(
            urljoin(final_url, str(x))
            for x in jsonld_image
            if isinstance(x, str)
        )
    elif isinstance(jsonld_image, dict):
        value = jsonld_image.get("url")
        if value:
            image_candidates.append(
                urljoin(final_url, value)
            )

    remove_unwanted_elements(soup)

    article = None

    selectors = [
        "article",
        "[itemprop='articleBody']",
        "[data-testid*='article']",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".single-post-content",
        ".td-post-content",
        ".post-body",
        ".article-body",
        ".story-body",
        ".article__body",
        ".article-content-body",
        ".content-area",
        "main",
    ]

    # از بین کاندیدها، بزرگ‌ترین محتوای متنی را انتخاب می‌کنیم.
    best = None
    best_len = 0

    for selector in selectors:
        try:
            for candidate in soup.select(selector):
                length = len(
                    clean_text(
                        candidate.get_text(" ", strip=True)
                    )
                )
                if length > best_len:
                    best = candidate
                    best_len = length
        except Exception:
            pass

    article = best or soup

    body_parts = []
    seen = set()

    for paragraph in article.find_all(
        ["p", "h2", "h3", "h4", "blockquote"]
    ):
        text = clean_text(
            paragraph.get_text(" ", strip=True)
        )

        if len(text) < 35:
            continue

        if is_probably_noise(text):
            continue

        key = norm(text)
        if not key or key in seen:
            continue

        seen.add(key)
        body_parts.append(text)

    if len(body_parts) < 3:
        body_parts = []

        for paragraph in soup.find_all("p"):
            text = clean_text(
                paragraph.get_text(" ", strip=True)
            )

            if (
                len(text) >= 35
                and not is_probably_noise(text)
            ):
                body_parts.append(text)

    body = "\n".join(body_parts)[:70000]

    if not image_candidates:
        for img in article.find_all("img"):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
            )

            if src:
                image_candidates.append(
                    urljoin(final_url, src)
                )
                break

    image = ""
    for candidate in image_candidates:
        if candidate and candidate.startswith(("http://", "https://")):
            image = candidate
            break

    result = {
        "url": final_url,
        "domain": urlparse(final_url).netloc.lower(),
        "title": strip_site_branding_from_title(title),
        "description": description,
        "body": body,
        "image": image,
    }

    # اگر محتوای کافی پیدا نشد، caller می‌تواند Web Search را امتحان کند.
    if len(norm(body)) < 250:
        result["weak_extraction"] = True
    else:
        result["weak_extraction"] = False

    return result


# ============================================================
# WEB SEARCH FALLBACK
# ============================================================

WEB_FALLBACK_PROMPT = """
تو یک سیستم بازیابی خبر برای Gamefa هستی.

یک URL خبری به تو داده می‌شود که استخراج مستقیم HTML آن موفق نبوده
یا محتوای کافی از آن پیدا نشده است.

با Web Search اطلاعات عمومی و قابل اتکای همان صفحه و موضوع خبر را پیدا کن.
به هیچ وجه خبر دیگری را به جای صفحه موردنظر انتخاب نکن.
اگر URL قابل دسترسی نیست، فقط اطلاعاتی را برگردان که مستقیماً با همان خبر
مرتبط و قابل تأیید هستند.

خروجی فقط JSON معتبر باشد:

{
  "title": "",
  "description": "",
  "body": "",
  "image": "",
  "source_url": ""
}

body باید شامل مهم‌ترین واقعیت‌های خبر باشد.
اطلاعات ساختگی ممنوع است.
اگر تصویر اصلی پیدا نشد، image را خالی بگذار.
"""


async def web_search_fallback(url):
    if not ENABLE_WEB_SEARCH_FALLBACK:
        return None

    prompt = (
        "URL موردنظر:\n"
        + url
        + "\n\n"
        "لطفاً همین صفحه و خبر مربوط به آن را با Web Search بررسی کن."
    )

    async def call(client):
        # Responses API ابزار Web Search.
        # در صورت تغییر نام ابزار در حساب/API، خطا گرفته می‌شود
        # و ربات به روش محلی/AI عادی برمی‌گردد.
        response = await client.responses.create(
            model=MODEL,
            instructions=WEB_FALLBACK_PROMPT,
            input=prompt,
            tools=[{"type": "web_search"}],
            max_output_tokens=1800,
        )
        return response.output_text or ""

    try:
        raw = await openai_failover(call)
        raw = raw.strip()

        raw = re.sub(
            r"^```json\s*",
            "",
            raw,
            flags=re.I,
        )
        raw = re.sub(r"\s*```$", "", raw)

        start = raw.find("{")
        end = raw.rfind("}")

        if start == -1 or end == -1:
            raise ValueError("Web Search خروجی JSON معتبر نداد.")

        data = json.loads(raw[start:end + 1])

        if not isinstance(data, dict):
            raise ValueError("Web Search خروجی نامعتبر داد.")

        title = clean_text(data.get("title", ""))
        body = clean_text(data.get("body", ""))
        description = clean_text(data.get("description", ""))
        image = clean_text(data.get("image", ""))
        source_url = clean_text(data.get("source_url", ""))

        if len(norm(body)) < 150 and len(norm(description)) < 80:
            return None

        return {
            "url": source_url or url,
            "domain": urlparse(
                source_url or url
            ).netloc.lower(),
            "title": strip_site_branding_from_title(title),
            "description": description,
            "body": body,
            "image": image if image.startswith(("http://", "https://")) else "",
            "weak_extraction": False,
            "web_search_used": True,
        }

    except Exception as error:
        log.warning("Web Search fallback failed: %s", error)
        return None


async def fetch_article(url):
    local_error = None

    try:
        source = await fetch_generic(url)

        if not source.get("weak_extraction"):
            return source

        log.warning(
            "Weak extraction from %s; trying Web Search.",
            urlparse(url).netloc,
        )

    except Exception as error:
        local_error = error
        log.warning(
            "Direct extraction failed for %s: %s",
            url,
            error,
        )

    fallback = await web_search_fallback(url)

    if fallback:
        return fallback

    if local_error:
        raise RuntimeError(
            "استخراج مقاله از سایت ناموفق بود و Web Search نیز نتوانست "
            "اطلاعات کافی پیدا کند."
        ) from local_error

    raise RuntimeError(
        "صفحه باز شد اما محتوای کافی برای ساخت خبر پیدا نشد."
    )


# ============================================================
# FACT EXTRACTION
# ============================================================

FACT_PROMPT = """
تو سیستم استخراج اطلاعات تحریریه Gamefa هستی.

وظیفه تو تولید خبر نیست؛ فقط واقعیت‌های مهم و مستقیم مقاله را استخراج کن.

مطالب مرتبط، تبلیغات، نویسنده، Reviewer، باکس‌های سایت و مطالب جانبی را
نادیده بگیر.

اگر تاریخ، زمان، عدد، حجم، قیمت، پلتفرم، بازیگر، کارگردان، سازنده، ناشر
یا وضعیت پروژه وجود دارد، استخراج کن.

هیچ اطلاعاتی را اختراع نکن.

خروجی فقط JSON معتبر باشد:

{
  "main_topic": "",
  "main_event": "",
  "facts": [
    {
      "fact": "",
      "importance": 1,
      "type": ""
    }
  ],
  "dates": [],
  "platforms": [],
  "numbers": [],
  "people": [],
  "companies": [],
  "status": "",
  "important_missing": []
}

importance بین 1 تا 5 باشد.
"""


def local_facts(source):
    body = clean_text(source.get("body", ""))

    sentences = re.split(
        r"(?<=[.!؟])\s+",
        body,
    )

    sentences = [
        x.strip()
        for x in sentences
        if len(x.strip()) > 20
    ]

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?(?:\s*(?:GB|TB|درصد|%))?",
        body,
        flags=re.I,
    )

    return {
        "main_topic": source.get("title", ""),
        "main_event": (
            sentences[0]
            if sentences
            else source.get("title", "")
        ),
        "facts": [
            {
                "fact": sentence,
                "importance": 4,
                "type": "article",
            }
            for sentence in sentences[:15]
        ],
        "dates": [],
        "platforms": [],
        "numbers": numbers[:20],
        "people": [],
        "companies": [],
        "status": "",
        "important_missing": [],
    }


async def extract_facts(source):
    input_text = (
        "دامنه:\n"
        + source.get("domain", "")
        + "\n\nعنوان:\n"
        + source.get("title", "")
        + "\n\nتوضیحات:\n"
        + source.get("description", "")
        + "\n\nمتن مقاله:\n"
        + source.get("body", "")[:AI_SOURCE_LIMIT]
    )

    try:
        response = await openai_failover(
            lambda client: client.responses.create(
                model=MODEL,
                instructions=FACT_PROMPT,
                input=input_text,
                max_output_tokens=1200,
            )
        )

        raw = (response.output_text or "").strip()

        raw = re.sub(
            r"^```json\s*",
            "",
            raw,
            flags=re.I,
        )
        raw = re.sub(r"\s*```$", "", raw)

        start = raw.find("{")
        end = raw.rfind("}")

        if start == -1 or end == -1:
            raise ValueError("JSON نامعتبر از AI دریافت شد.")

        data = json.loads(raw[start:end + 1])

        return data if isinstance(data, dict) else local_facts(source)

    except Exception as error:
        log.warning("Fact extraction failed: %s", error)
        return local_facts(source)


# ============================================================
# NEWS GENERATION
# ============================================================

NEWS_PROMPT = """
تو سردبیر ارشد اخبار فارسی Gamefa هستی.

از Factهای داده‌شده یک خبر فارسی حرفه‌ای تولید کن.

خروجی دقیقاً:

خط اول: تیتر

خط دوم: یک پاراگراف شامل دقیقاً 7 جمله خبری.

هر 7 جمله باید در همان یک پاراگراف باشند.

قوانین:
- تیتر باید با فارسی شروع شود.
- هر 7 جمله باید با فارسی شروع شوند.
- اطلاعات مهم Factها حذف نشوند.
- هیچ اطلاعاتی اختراع نشود.
- تاریخ‌ها و اعداد مهم حذف نشوند.
- نام‌های انگلیسی را حفظ کن.
- جمله با نام انگلیسی شروع نشود.
- نام گیمفا در تیتر نیاید.
- هیچ Markdown استفاده نکن.
- هیچ Emoji استفاده نکن.
- هیچ لینک استفاده نکن.
- هیچ اشاره‌ای به AI، Reviewer، Fact یا فرایند تولید نکن.
- هیچ تحلیل شخصی نکن.
- متن طبیعی و خبری باشد.

فقط تیتر و یک پاراگراف 7 جمله‌ای خروجی بده.
"""


def clean_sentence(sentence):
    sentence = sentence.strip()

    sentence = re.sub(
        r"^[•\-–—\d.)]+\s*",
        "",
        sentence,
    )

    sentence = re.sub(
        r"^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]+\s*",
        "",
        sentence,
    )

    return sentence.strip()


def split_sentences(text):
    text = clean_ai_text(text)

    lines = [
        x.strip()
        for x in text.replace("\r", "\n").splitlines()
        if x.strip()
    ]

    if not lines:
        return "", []

    title = lines[0]
    body = " ".join(lines[1:])
    body = re.sub(r"\s+", " ", body).strip()

    sentences = re.split(
        r"(?<=[.!؟])\s+",
        body,
    )

    sentences = [
        clean_sentence(x)
        for x in sentences
        if clean_sentence(x)
    ]

    return title, sentences


def local_news_fallback(source, facts):
    title = strip_site_branding_from_title(
        clean_text(source.get("title", ""))
    )

    title = ensure_persian_start(
        title or "خبر جدید",
        True,
    )

    pool = []

    for item in facts.get("facts", []):
        if isinstance(item, dict):
            fact = clean_sentence(
                str(item.get("fact", ""))
            )
            if fact:
                pool.append(fact)

    raw = clean_text(source.get("body", ""))

    for item in re.split(
        r"(?<=[.!؟])\s+",
        raw,
    ):
        item = clean_sentence(item)
        if len(item) >= 25:
            pool.append(item)

    unique = []
    seen = set()

    for item in pool:
        key = norm(item)
        if not key or key in seen:
            continue

        seen.add(key)
        unique.append(
            ensure_persian_start(item, False)
        )

    while len(unique) < 7:
        unique.append(
            "این خبر جزئیات بیشتری درباره موضوع اصلی ارائه می‌کند."
        )

    return title + "\n" + " ".join(unique[:7])


async def generate_news(source, facts):
    facts_json = json.dumps(
        facts,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    input_text = (
        "FACTS:\n"
        + facts_json
        + "\n\nدامنه منبع:\n"
        + source.get("domain", "")
        + "\n\nعنوان:\n"
        + source.get("title", "")
        + "\n\nمتن اصلی:\n"
        + source.get("body", "")[:AI_SOURCE_LIMIT]
    )

    try:
        response = await openai_failover(
            lambda client: client.responses.create(
                model=MODEL,
                instructions=NEWS_PROMPT,
                input=input_text,
                max_output_tokens=AI_MAX_OUTPUT_TOKENS,
            )
        )

        result = (response.output_text or "").strip()

        if not result:
            raise RuntimeError("AI خروجی خالی تولید کرد.")

        return result

    except Exception as error:
        log.warning("News generation failed: %s", error)
        return local_news_fallback(source, facts)


# ============================================================
# VALIDATION / FORMAT
# ============================================================

FORBIDDEN_OUTPUT_TERMS = [
    "reviewer",
    "ai score",
    "accuracy score",
    "امتیاز دقت",
    "هوش مصنوعی",
    "اطلاعات استخراج شده",
    "متن کامل صفحه",
    "متن کامل مقاله",
    "در این صفحه",
    "fact",
]


def validate_generated_output(generated):
    title, sentences = split_sentences(generated)

    if not title or len(sentences) != 7:
        return False

    combined = (
        title + " " + " ".join(sentences)
    ).lower()

    for term in FORBIDDEN_OUTPUT_TERMS:
        if term.lower() in combined:
            return False

    if not starts_with_persian(title):
        return False

    for sentence in sentences:
        if not starts_with_persian(sentence):
            return False

    return True


def format_post(generated):
    generated = clean_ai_text(generated)

    title, sentences = split_sentences(generated)

    if len(sentences) != 7:
        return ""

    title = strip_site_branding_from_title(
        clean_sentence(title)
    )
    title = ensure_persian_start(title, True)

    fixed = []

    for sentence in sentences:
        sentence = ensure_persian_start(
            clean_sentence(sentence),
            False,
        )
        fixed.append(sentence)

    category = detect_category(
        title + " " + " ".join(fixed)
    )

    title = category + " " + title
    body = " ".join(fixed)

    return (
        "<b>"
        + escape_html(title)
        + "</b>\n\n"
        + "🟣 "
        + escape_html(body)
        + "\n\n"
        + "<b>🆔 @Gamefa_official</b>"
    )


# ============================================================
# IMAGE
# ============================================================

async def download_image(url):
    if not url:
        return None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151 Safari/537.36"
            ),
        }

        timeout = aiohttp.ClientTimeout(total=35)

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=timeout,
        ) as session:
            async with session.get(
                url,
                allow_redirects=True,
            ) as response:
                if response.status != 200:
                    return None

                data = await response.read()

                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

        if not data or len(data) < 1000:
            return None

        if len(data) > 15 * 1024 * 1024:
            return None

        extension = Path(
            urlparse(url).path
        ).suffix.lower()

        if extension not in (
            ".jpg", ".jpeg", ".png", ".webp"
        ):
            if "png" in content_type:
                extension = ".png"
            elif "webp" in content_type:
                extension = ".webp"
            else:
                extension = ".jpg"

        filename = (
            "news_"
            + hashlib.md5(
                url.encode("utf-8")
            ).hexdigest()
            + extension
        )

        path = IMAGE_DIR / filename
        path.write_bytes(data)

        return path

    except Exception as error:
        log.warning("Image download error: %s", error)
        return None


async def find_best_image(source):
    image = source.get("image", "")
    if not image:
        return None

    return await download_image(image)


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔎 بررسی خبر جدید"),
                KeyboardButton(text="📁 آرشیو"),
            ],
            [
                KeyboardButton(text="📊 آمار"),
                KeyboardButton(text="⚙️ تنظیمات"),
            ],
        ],
        resize_keyboard=True,
    )


def news_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📝 ارسال خبر"),
                KeyboardButton(text="🔗 ارسال لینک"),
            ],
            [
                KeyboardButton(text="🔙 بازگشت"),
            ],
        ],
        resize_keyboard=True,
    )


def archive_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📚 آخرین اخبار"),
                KeyboardButton(text="🗑 پاکسازی آرشیو"),
            ],
            [
                KeyboardButton(text="🔙 بازگشت"),
            ],
        ],
        resize_keyboard=True,
    )


def settings_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📢 کانال انتشار"),
                KeyboardButton(text="🧠 مدل AI"),
            ],
            [
                KeyboardButton(text="🖼 سیستم تصویر"),
                KeyboardButton(text="✍️ قالب خبر"),
            ],
            [
                KeyboardButton(text="🔙 بازگشت"),
            ],
        ],
        resize_keyboard=True,
    )


def publish_keyboard_disabled():
    """سازگاری ساختاری قدیمی؛ هیچ دکمه انتشار در این نسخه ساخته نمی‌شود."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="home",
        )]
    ])


# توجه: تابع بالا فقط برای سازگاری با ساختار نسخه‌های قدیمی نگه داشته شده
# و در هیچ مسیر فعال رابط کاربری استفاده نمی‌شود.


# ============================================================
# PROCESS NEWS
# ============================================================

async def process_news(message, text):
    user_id = message.from_user.id

    if user_id in processing_users:
        await message.answer("⏳ یک خبر در حال پردازش است.")
        return

    processing_users.add(user_id)

    status = None
    image_path = None

    try:
        url = extract_url(text)

        if url:
            status = await message.answer(
                "⏳ در حال دریافت مقاله..."
            )

            source = await fetch_article(url)

            domain = source.get("domain", "سایت ناشناس")

            await status.edit_text(
                "🧠 مقاله دریافت شد.\n"
                f"منبع: {domain}\n"
                "در حال استخراج اطلاعات..."
            )

        else:
            source = {
                "url": "",
                "domain": "manual",
                "title": "",
                "description": "",
                "body": text,
                "image": "",
            }

        duplicate_text = (
            source.get("title", "")
            + "\n"
            + source.get("body", "")
        )

        if duplicate(
            duplicate_text,
            source.get("title", ""),
        ):
            if status:
                await status.delete()

            await message.answer(
                "⚠️ این خبر یا خبر بسیار مشابه آن قبلاً در آرشیو وجود دارد.",
                reply_markup=main_keyboard(),
            )
            return

        facts = await extract_facts(source)

        if status:
            await status.edit_text(
                "🧠 اطلاعات اصلی استخراج شد.\n"
                "در حال ساخت خبر..."
            )

        generated = await generate_news(
            source,
            facts,
        )

        if not validate_generated_output(generated):
            generated = local_news_fallback(
                source,
                facts,
            )

        post = format_post(generated)

        if not post:
            generated = local_news_fallback(
                source,
                facts,
            )
            post = format_post(generated)

        if not post:
            raise RuntimeError(
                "تولید متن نهایی ناموفق بود."
            )

        image_path = await find_best_image(source)

        memory.append(
            {
                "hash": text_hash(duplicate_text),
                "title": source.get("title", ""),
                "source": duplicate_text[:25000],
                "post": post,
                "url": url or "",
                "domain": source.get("domain", ""),
                "web_search_used": bool(
                    source.get("web_search_used", False)
                ),
                "created_at": int(time.time()),
            }
        )

        memory[:] = memory[-MAX_MEMORY:]
        save_memory()

        prepared[user_id] = {
            "text": post,
            "image": str(image_path) if image_path else "",
        }

        if status:
            try:
                await status.delete()
            except Exception:
                pass

        # Telegram caption limit = 1024 characters.
        if image_path and len(post) <= 1024:
            try:
                await message.answer_photo(
                    FSInputFile(image_path),
                    caption=post,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception as error:
                log.warning(
                    "Photo preview failed: %s",
                    error,
                )
                await message.answer(
                    post,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )

        elif image_path:
            try:
                await message.answer_photo(
                    FSInputFile(image_path)
                )
            except Exception as error:
                log.warning(
                    "Image preview failed: %s",
                    error,
                )

            await message.answer(
                post,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )

        else:
            await message.answer(
                post,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )

        await message.answer(
            "✅ خبر آماده انتشار است.",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        log.exception("News processing error")

        if status:
            try:
                await status.delete()
            except Exception:
                pass

        await message.answer(
            "❌ خطا هنگام پردازش خبر:\n\n"
            + str(error)[:1800],
            reply_markup=main_keyboard(),
        )

    finally:
        processing_users.discard(user_id)


# ============================================================
# PUBLISH
# ============================================================

async def publish_news(message, user_id):
    item = prepared.get(user_id)

    if not item:
        await message.answer(
            "❌ خبری برای انتشار آماده نیست."
        )
        return

    text = item.get("text", "")
    image = item.get("image", "")

    try:
        if image and Path(image).exists():
            if len(text) <= 1024:
                await message.bot.send_photo(
                    CHANNEL_ID,
                    FSInputFile(image),
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await message.bot.send_photo(
                    CHANNEL_ID,
                    FSInputFile(image),
                )
                await message.bot.send_message(
                    CHANNEL_ID,
                    text,
                    parse_mode=ParseMode.HTML,
                )
        else:
            await message.bot.send_message(
                CHANNEL_ID,
                text,
                parse_mode=ParseMode.HTML,
            )

        prepared.pop(user_id, None)

        await message.answer(
            "✅ خبر با موفقیت در کانال منتشر شد.",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        log.exception("Publish error")

        await message.answer(
            "❌ خطا هنگام انتشار:\n\n"
            + str(error)[:1800],
            reply_markup=main_keyboard(),
        )


# ============================================================
# ROUTER
# ============================================================

router = Router()


@router.message(Command("start"))
async def start_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ این ربات خصوصی است.")
        return

    await message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>\n\n"
        f"نسخه: <b>{BOT_VERSION}</b>\n\n"
        "ربات آماده دریافت خبر از Gamefa و سایت‌های خبری دیگر است.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


# مسیر publish_current عمداً ثبت نمی‌شود؛ انتشار مستقیم از ربات غیرفعال است.
async def publish_callback_disabled(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await callback.answer("در حال انتشار...")

    await publish_news(
        callback.message,
        callback.from_user.id,
    )


@router.callback_query(F.data == "home")
async def home_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await callback.answer()

    await callback.message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


# ============================================================
# MENU
# ============================================================

@router.message(F.text == "🔎 بررسی خبر جدید")
async def news_menu(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "🔎 <b>بررسی خبر جدید</b>\n\n"
        "لینک هر سایت خبری یا متن خبر را ارسال کن.",
        parse_mode=ParseMode.HTML,
        reply_markup=news_keyboard(),
    )


@router.message(F.text == "📝 ارسال خبر")
async def text_news(message: Message):
    if not is_admin(message):
        return

    await message.answer("📝 متن خبر را ارسال کن.")


@router.message(F.text == "🔗 ارسال لینک")
async def link_news(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "🔗 لینک خبر را ارسال کن.\n\n"
        "Gamefa و سایت‌های خبری دیگر پشتیبانی می‌شوند."
    )


# ============================================================
# ARCHIVE
# ============================================================

@router.message(F.text == "📁 آرشیو")
async def archive(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "📁 <b>آرشیو</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=archive_keyboard(),
    )


@router.message(F.text == "📚 آخرین اخبار")
async def latest_news(message: Message):
    if not is_admin(message):
        return

    if not memory:
        await message.answer("📚 آرشیو خالی است.")
        return

    lines = ["📚 <b>آخرین اخبار</b>", ""]

    for index, item in enumerate(
        reversed(memory[-10:]),
        1,
    ):
        title = item.get(
            "title",
            "خبر بدون عنوان",
        )

        domain = item.get("domain", "")

        lines.append(
            f"{index}. {escape_html(title[:120])}"
        )

        if domain:
            lines.append(
                f"   🌐 {escape_html(domain)}"
            )

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=archive_keyboard(),
    )


@router.message(F.text == "🗑 پاکسازی آرشیو")
async def clear_archive(message: Message):
    if not is_admin(message):
        return

    memory.clear()
    prepared.clear()
    save_memory()

    await message.answer(
        "✅ آرشیو پاک شد.",
        reply_markup=archive_keyboard(),
    )


# ============================================================
# STATS
# ============================================================

@router.message(F.text == "📊 آمار")
async def stats(message: Message):
    if not is_admin(message):
        return

    web_count = sum(
        1 for item in memory
        if item.get("web_search_used")
    )

    await message.answer(
        "📊 <b>آمار ربات</b>\n\n"
        f"📰 آرشیو: <b>{len(memory)}</b>\n"
        f"💾 ظرفیت: <b>{MAX_MEMORY}</b>\n"
        f"🧠 مدل: <code>{escape_html(MODEL)}</code>\n"
        f"🔑 کلیدهای OpenAI: <b>{len(OPENAI_KEYS)}</b>\n"
        f"🌐 خبرهای پردازش‌شده با Web Search: <b>{web_count}</b>\n"
        f"🔎 Web Search fallback: "
        f"<b>{'فعال' if ENABLE_WEB_SEARCH_FALLBACK else 'غیرفعال'}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


# ============================================================
# SETTINGS
# ============================================================

@router.message(F.text == "⚙️ تنظیمات")
async def settings(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "⚙️ <b>تنظیمات</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )


@router.message(F.text == "📢 کانال انتشار")
async def channel_setting(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "📢 کانال:\n\n"
        f"<code>{escape_html(CHANNEL_ID)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )


@router.message(F.text == "🧠 مدل AI")
async def model_setting(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "🧠 مدل:\n\n"
        f"<code>{escape_html(MODEL)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )


@router.message(F.text == "🖼 سیستم تصویر")
async def image_setting(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "🖼 سیستم تصویر\n\n"
        "1. og:image\n"
        "2. twitter:image\n"
        "3. JSON-LD image\n"
        "4. اولین تصویر مناسب مقاله\n\n"
        "در صورت شکست، خبر بدون تصویر ادامه پیدا می‌کند.",
        reply_markup=settings_keyboard(),
    )


@router.message(F.text == "✍️ قالب خبر")
async def format_setting(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "✍️ قالب خبر:\n\n"
        "• تیتر فارسی\n"
        "• دقیقاً ۷ جمله\n"
        "• یک پاراگراف\n"
        "• شروع فارسی هر جمله\n"
        "• حفظ اطلاعات مهم\n"
        "• تصویر در صورت امکان\n"
        "• امضای Gamefa",
        reply_markup=settings_keyboard(),
    )


@router.message(F.text == "🔙 بازگشت")
async def back(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


# ============================================================
# COMMANDS
# ============================================================

# دستور /publish عمداً ثبت نمی‌شود؛ ربات در v5.3.1 فقط خبر را آماده می‌کند.
async def publish_command_disabled(message: Message):
    if not is_admin(message):
        return

    await publish_news(
        message,
        message.from_user.id,
    )


@router.message(Command("stats"))
async def stats_command(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        f"📊 تعداد اخبار آرشیو: {len(memory)}"
    )


@router.message(Command("clear"))
async def clear_command(message: Message):
    if not is_admin(message):
        return

    memory.clear()
    prepared.clear()
    save_memory()

    await message.answer(
        "✅ آرشیو پاک شد.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# TEXT HANDLER
# ============================================================

@router.message(F.text)
async def text_handler(message: Message):
    if not is_admin(message):
        return

    text = (message.text or "").strip()

    if not text or text.startswith("/"):
        return

    menu_words = {
        "🔎 بررسی خبر جدید",
        "📁 آرشیو",
        "📊 آمار",
        "⚙️ تنظیمات",
        "📝 ارسال خبر",
        "🔗 ارسال لینک",
        "📚 آخرین اخبار",
        "🗑 پاکسازی آرشیو",
        "📢 کانال انتشار",
        "🧠 مدل AI",
        "🖼 سیستم تصویر",
        "✍️ قالب خبر",
        "🔙 بازگشت",
    }

    if text in menu_words:
        return

    await process_news(message, text)


# ============================================================
# V5.3 ADVANCED EDITORIAL ENGINE
# ============================================================

ADVANCED_LENGTHS = {
    "7": 7,
    "10": 10,
    "15": 15,
    "short": 5,
    "کوتاه": 5,
    "استاندارد": 7,
    "بلند": 10,
}

WRITING_MODES = {
    "standard": "خبر رسمی و متعادل",
    "short": "خبر کوتاه و فشرده",
    "exciting": "خبر پرانرژی اما حرفه‌ای و بدون اغراق",
}


def load_editorial_state():
    global editorial_learning, editorial_stats
    try:
        if LEARNING_FILE.exists():
            data = json.loads(LEARNING_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                editorial_learning = data[-500:]
    except Exception as error:
        log.warning("Learning load error: %s", error)
    try:
        if STATS_FILE.exists():
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                editorial_stats.update(data)
    except Exception as error:
        log.warning("Stats load error: %s", error)


def save_editorial_state():
    try:
        LEARNING_FILE.write_text(json.dumps(editorial_learning[-500:], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        log.warning("Learning save error: %s", error)
    try:
        STATS_FILE.write_text(json.dumps(editorial_stats, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        log.warning("Stats save error: %s", error)


def stat_inc(name, amount=1):
    editorial_stats[name] = int(editorial_stats.get(name, 0)) + amount
    try:
        save_editorial_state()
    except Exception:
        pass


def structured_log(stage, message, **extra):
    payload = " ".join(f"{k}={v}" for k, v in extra.items())
    log.info("[%s] %s%s", stage.upper(), message, (" | " + payload) if payload else "")


def source_domain(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return "unknown"


def source_quality(source):
    body_len = len(norm(source.get("body", "")))
    title_len = len(norm(source.get("title", "")))
    image_bonus = 0.08 if source.get("image") else 0.0
    desc_bonus = 0.06 if source.get("description") else 0.0
    length_score = min(body_len / 5000.0, 1.0) * 0.70
    title_score = min(title_len / 80.0, 1.0) * 0.16
    return min(1.0, length_score + title_score + image_bonus + desc_bonus)


def is_breaking(source, facts):
    text = norm(" ".join([
        source.get("title", ""), source.get("description", ""), source.get("body", "")[:5000],
        json.dumps(facts, ensure_ascii=False),
    ]))
    words = [
        "breaking", "urgent", "فوری", "لحظاتی پیش", "همین حالا", "رسماً اعلام شد",
        "تایید شد", "confirmed", "announced today", "just announced", "درگذشت", "مرگ",
        "تعطیلی", "لغو شد", "cancelled", "cancelled", "delay", "تاخیر بزرگ",
    ]
    hits = sum(1 for word in words if word in text)
    return hits >= 2 or (hits >= 1 and source_quality(source) >= BREAKING_THRESHOLD)


def detect_spoiler(source, facts):
    if not ENABLE_SPOILER_DETECTION:
        return False
    text = norm(" ".join([
        source.get("title", ""), source.get("body", "")[:8000],
        json.dumps(facts, ensure_ascii=False),
    ]))
    spoiler_words = [
        "spoiler", "اسپویل", "پایان بازی", "پایان فیلم", "قاتل", "مرگ شخصیت",
        "ending", "finale", "dies", "death of", "secret ending", "plot twist",
        "twist ending", "داستان بازی", "داستان فیلم",
    ]
    return any(x in text for x in spoiler_words)


def make_hashtags(source, facts):
    if not ENABLE_HASHTAGS:
        return []
    candidates = []
    for key in ("people", "companies", "platforms"):
        value = facts.get(key, []) if isinstance(facts, dict) else []
        if isinstance(value, list):
            candidates.extend(str(x) for x in value)
    title = source.get("title", "")
    english = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", title)
    candidates.extend(english)
    out = []
    seen = set()
    for item in candidates:
        item = re.sub(r"[^A-Za-z0-9آ-ی]", "", item).strip()
        if len(item) < 3:
            continue
        tag = "#" + item.replace(" ", "")
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= 5:
            break
    return out


async def multi_source_research(source):
    if not ENABLE_MULTI_SOURCE or not OPENAI_KEYS:
        return []
    domain = source_domain(source.get("url", ""))
    title = source.get("title", "")
    prompt = f"""
برای خبر زیر، حداکثر 4 منبع خبری معتبر و مستقل درباره همان رویداد پیدا کن.
منبع فعلی: {domain}
عنوان: {title}
URL: {source.get('url','')}
فقط منابعی را انتخاب کن که واقعاً درباره همین رویداد هستند.
خروجی فقط JSON:
{{"sources":[{{"name":"","url":"","summary":"","confidence":0}}]}}
"""
    async def call(client):
        return await client.responses.create(
            model=MODEL,
            instructions="تو دستیار تحقیق تحریریه هستی. اطلاعات را اختراع نکن.",
            input=prompt,
            tools=[{"type": "web_search"}],
            max_output_tokens=1400,
        )
    try:
        response = await openai_failover(call)
        raw = (response.output_text or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return []
        data = json.loads(raw[start:end + 1])
        sources = data.get("sources", []) if isinstance(data, dict) else []
        clean = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            if item.get("url") and item.get("summary"):
                clean.append({
                    "name": clean_text(str(item.get("name", "منبع"))),
                    "url": clean_text(str(item.get("url", ""))),
                    "summary": clean_text(str(item.get("summary", ""))),
                    "confidence": float(item.get("confidence", 0) or 0),
                })
        if clean:
            stat_inc("multi_source")
        return clean[:4]
    except Exception as error:
        log.warning("Multi-source research failed: %s", error)
        return []


def build_source_context(source, related):
    parts = [
        f"منبع اصلی: {source.get('domain','')}\nعنوان: {source.get('title','')}\nمتن: {source.get('body','')[:AI_SOURCE_LIMIT]}"
    ]
    if related:
        parts.append("منابع مستقل برای راستی‌آزمایی:\n" + "\n".join(
            f"- {x['name']}: {x['summary']}" for x in related
        ))
    return "\n\n".join(parts)


def normalize_length(value):
    value = str(value or "7").strip().lower()
    return ADVANCED_LENGTHS.get(value, 7)


def normalize_mode(value):
    value = str(value or "standard").strip().lower()
    return value if value in WRITING_MODES else "standard"


def learning_context():
    if not editorial_learning:
        return ""
    recent = editorial_learning[-10:]
    lines = []
    for item in recent:
        if isinstance(item, dict) and item.get("instruction"):
            lines.append("- " + str(item["instruction"]))
    return "\n".join(lines)


async def rewrite_news_with_settings(source, facts, length=None, mode=None):
    length = normalize_length(length or NEWS_LENGTH)
    mode = normalize_mode(mode or WRITING_MODE)
    context = learning_context()
    prompt = f"""
تو سردبیر Gamefa هستی. یک خبر فارسی حرفه‌ای بساز.
حالت نگارش: {WRITING_MODES[mode]}
تعداد جمله‌های بدنه: دقیقاً {length}
تیتر و بدنه باید با فارسی شروع شوند.
همه واقعیت‌های مهم را حفظ کن و چیزی اختراع نکن.
نام‌های انگلیسی را حفظ کن اما جمله با نام انگلیسی شروع نشود.
هیچ Markdown، Emoji، لینک، Reviewer، AI، Fact یا توضیح فرایند تولید نیاور.
خروجی فقط تیتر در خط اول و سپس یک پاراگراف {length} جمله‌ای باشد.
"""
    if context:
        prompt += "\nنمونه اصلاحات قبلی ادمین برای رعایت سبک:\n" + context
    input_text = "FACTS:\n" + json.dumps(facts, ensure_ascii=False) + "\n\n" + build_source_context(source, source.get("related_sources", []))
    response = await openai_failover(lambda client: client.responses.create(
        model=MODEL, instructions=prompt, input=input_text,
        max_output_tokens=max(1200, length * 220),
    ))
    return (response.output_text or "").strip()


def parse_editable_post(post):
    raw = re.sub(r"<[^>]+>", "", post or "")
    raw = raw.replace("🟣 ", "")
    raw = raw.replace("🆔 @Gamefa_official", "").strip()
    parts = raw.split("\n\n", 1)
    title = parts[0].strip() if parts else ""
    body = parts[1].strip() if len(parts) > 1 else ""
    return title, body


def build_custom_post(title, body, source=None, facts=None):
    title = ensure_persian_start(strip_site_branding_from_title(clean_sentence(title)), True)
    body = clean_text(body)
    # حذف هشتگ‌های احتمالی تولیدشده توسط AI یا ورودی ادمین.
    title = re.sub(r"(?<!\w)#[\w\u0600-\u06FF-]+", "", title).strip()
    body = re.sub(r"(?<!\w)#[\w\u0600-\u06FF-]+", "", body).strip()
    if not title or not body:
        return ""
    category = detect_category(title + " " + body)
    prefix = ""
    if is_breaking(source or {}, facts or {}):
        prefix = "🚨 "
    spoiler = "⚠️ احتمال اسپویل" if detect_spoiler(source or {}, facts or {}) else ""
    # هشتگ‌ها در v5.3.1 به‌صورت کامل غیرفعال هستند.
    # حتی اگر تابع قدیمی make_hashtags در فایل باقی مانده باشد،
    # هیچ هشتگی وارد متن نهایی خبر نمی‌شود.
    suffix = ""
    if spoiler:
        suffix += "\n\n" + spoiler
    return (
        "<b>" + escape_html(prefix + category + " " + title) + "</b>\n\n"
        + "🟣 " + escape_html(body) + escape_html(suffix)
        + "\n\n<b>🆔 @Gamefa_official</b>"
    )


def key_health_snapshot():
    now = time.time()
    rows = []
    for index, key in enumerate(OPENAI_KEYS):
        cooldown = max(0, int(OPENAI_KEY_COOLDOWN.get(index, 0) - now))
        if cooldown:
            status = f"🟡 cooldown {cooldown}s"
        else:
            status = "🟢 آماده"
        rows.append(f"{index + 1}️⃣ {status}")
    return rows or ["❌ کلیدی تنظیم نشده است"]


def editorial_dashboard_text():
    return (
        "📊 <b>داشبورد تحریریه v5.3</b>\n\n"
        f"📰 پردازش‌شده: <b>{editorial_stats.get('processed',0)}</b>\n"
        f"📢 منتشرشده: <b>{editorial_stats.get('published',0)}</b>\n"
        f"♻️ تکراری: <b>{editorial_stats.get('duplicates',0)}</b>\n"
        f"❌ ناموفق: <b>{editorial_stats.get('failed',0)}</b>\n"
        f"🖼 تصویر موفق: <b>{editorial_stats.get('images_ok',0)}</b>\n"
        f"🚨 Breaking: <b>{editorial_stats.get('breaking',0)}</b>\n"
        f"⚠️ Spoiler: <b>{editorial_stats.get('spoilers',0)}</b>\n"
        f"🔎 Web Search: <b>{editorial_stats.get('web_search',0)}</b>\n"
        f"🌐 چندمنبعی: <b>{editorial_stats.get('multi_source',0)}</b>\n"
        f"✏️ اصلاحات: <b>{editorial_stats.get('edits',0)}</b>\n"
        f"🔄 بازنویسی: <b>{editorial_stats.get('rewrites',0)}</b>\n"
        f"📥 بیشترین صف: <b>{editorial_stats.get('queue_max',0)}</b>\n\n"
        "🔑 <b>وضعیت کلیدها</b>\n" + "\n".join(key_health_snapshot())
    )


async def smart_image_download(source):
    """تصویر را دانلود و از نظر اندازه/فرمت بررسی می‌کند."""
    url = source.get("image", "")
    if not url:
        stat_inc("images_failed")
        return None
    path = await download_image(url)
    if not path:
        stat_inc("images_failed")
        return None
    if Image is None:
        stat_inc("images_ok")
        return path
    try:
        with Image.open(path) as img:
            width, height = img.size
            if width < 400 or height < 250:
                path.unlink(missing_ok=True)
                stat_inc("images_failed")
                return None
        stat_inc("images_ok")
        return path
    except Exception:
        stat_inc("images_ok")
        return path


def remember_admin_edit(original, corrected, kind="edit"):
    instruction = ""
    if kind == "title":
        instruction = "تیتر را با سبک اصلاح‌شده ادمین تولید کن: " + corrected[:250]
    elif kind == "body":
        instruction = "لحن و ساختار بدنه را به این سبک نزدیک کن: " + corrected[:400]
    else:
        instruction = "اصلاح ادمین: " + corrected[:400]
    editorial_learning.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "instruction": instruction,
        "original": original[:500],
        "corrected": corrected[:500],
    })
    editorial_learning[:] = editorial_learning[-500:]
    stat_inc("edits")
    save_editorial_state()


def advanced_publish_keyboard():
    """
    کنترل‌های خبر آماده: فقط ویرایش، بازنویسی و لغو.

    طبق تنظیمات v5.3.1:
    - گزینه انتشار در کانال حذف شده است.
    - گزینه تغییر حالت حذف شده است.
    - هیچ هشتگی در خروجی قرار نمی‌گیرد.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ ویرایش تیتر", callback_data="edit_title")],
        [InlineKeyboardButton(text="✍️ ویرایش متن", callback_data="edit_body")],
        [InlineKeyboardButton(text="🔄 بازنویسی", callback_data="rewrite_current")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="cancel_current")],
    ])


# این تابع عمداً در نسخه 5.3.1 نگه داشته شده تا سازگاری ساختاری حفظ شود،
# اما هیچ دکمه یا مسیر کاربری برای انتشار در کانال به آن متصل نیست.
def publishing_is_disabled():
    return True


async def advanced_process_news(message, text):
    """نسخه پیشرفته پردازش؛ تابع قبلی عمداً حذف نشده و این تابع جایگزین آن می‌شود."""
    user_id = message.from_user.id
    if user_id in processing_users:
        await message.answer("⏳ یک خبر دیگر در حال پردازش است.")
        return
    processing_users.add(user_id)
    status = None
    try:
        url = extract_url(text)
        if url:
            status = await message.answer("⏳ در حال دریافت و تحلیل منبع...")
            structured_log("fetch", "starting", url=url)
            source = await fetch_article(url)
            if source.get("web_search_used"):
                stat_inc("web_search")
            await status.edit_text("🧠 منبع دریافت شد؛ در حال استخراج واقعیت‌ها...")
        else:
            source = {"url":"", "domain":"manual", "title":"", "description":"", "body":text, "image":"", "weak_extraction":False}
        duplicate_text = source.get("title", "") + "\n" + source.get("body", "")
        if duplicate(duplicate_text, source.get("title", "")):
            stat_inc("duplicates")
            if status:
                await status.delete()
            await message.answer("⚠️ این خبر یا یک خبر بسیار مشابه قبلاً در آرشیو وجود دارد.", reply_markup=main_keyboard())
            return
        facts = await extract_facts(source)
        related = await multi_source_research(source)
        source["related_sources"] = related
        breaking = is_breaking(source, facts)
        spoiler = detect_spoiler(source, facts)
        if breaking:
            stat_inc("breaking")
        if spoiler:
            stat_inc("spoilers")
        await status.edit_text("✍️ در حال ساخت نسخه تحریریه...") if status else None
        length = normalize_length(NEWS_LENGTH)
        mode = normalize_mode(WRITING_MODE)
        generated = await rewrite_news_with_settings(source, facts, length, mode)
        title, sentences = split_sentences(generated)
        if len(sentences) != length or not starts_with_persian(title) or any(not starts_with_persian(x) for x in sentences):
            generated = local_news_fallback(source, facts)
            title, sentences = split_sentences(generated)
            # در حالت پیش‌فرض همیشه 7 جمله داریم؛ اگر طول سفارشی است، تا حد امکان همان تعداد را می‌سازیم.
            if length != 7:
                sentences = sentences[:length]
                while len(sentences) < length:
                    sentences.append("این گزارش جزئیات بیشتری درباره موضوع اصلی ارائه می‌کند.")
                generated = title + "\n" + " ".join(sentences)
        body = " ".join(sentences)
        post = build_custom_post(title, body, source, facts)
        if not post:
            raise RuntimeError("ساخت متن نهایی ناموفق بود.")
        image_path = await smart_image_download(source)
        editorial_stats["processed"] = int(editorial_stats.get("processed", 0)) + 1
        memory.append({
            "hash": text_hash(duplicate_text), "title": source.get("title", ""),
            "source": duplicate_text[:25000], "post": post, "url": url or "",
            "domain": source.get("domain", ""), "breaking": breaking,
            "spoiler": spoiler, "mode": mode, "length": length,
            "related_sources": related,
        })
        memory[:] = memory[-MAX_MEMORY:]
        save_memory(); save_editorial_state()
        prepared[user_id] = {
            "text": post, "image": str(image_path) if image_path else "",
            "source": source, "facts": facts, "title": title, "body": body,
            "mode": mode, "length": length,
        }
        if status:
            try: await status.delete()
            except Exception: pass
        if image_path and len(post) <= 1024:
            await message.answer_photo(FSInputFile(image_path), caption=post, parse_mode=ParseMode.HTML, reply_markup=advanced_publish_keyboard())
        elif image_path:
            await message.answer_photo(FSInputFile(image_path))
            await message.answer(post, parse_mode=ParseMode.HTML, reply_markup=advanced_publish_keyboard())
        else:
            await message.answer(post, parse_mode=ParseMode.HTML, reply_markup=advanced_publish_keyboard())
        await message.answer("✅ خبر آماده است. قبل از انتشار می‌توانی تیتر/متن را ویرایش یا بازنویسی کنی.", reply_markup=main_keyboard())
    except Exception as error:
        stat_inc("failed")
        log.exception("Advanced news processing error")
        if status:
            try: await status.delete()
            except Exception: pass
        await message.answer("❌ خطا هنگام پردازش خبر:\n\n" + str(error)[:1500], reply_markup=main_keyboard())
    finally:
        processing_users.discard(user_id)


# تابع اصلی پردازش از اینجا به موتور v5.3 وصل می‌شود.
process_news = advanced_process_news


# ============================================================
# QUEUE ENGINE
# ============================================================

async def queue_worker():
    structured_log("queue", "worker started")
    while True:
        try:
            if not news_queue:
                await asyncio.sleep(0.4)
                continue
            item = news_queue.popleft()
            user_id = item["user_id"]
            message = item["message"]
            text = item["text"]
            structured_log("queue", "processing", user=user_id, remaining=len(news_queue))
            await process_news(message, text)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("Queue worker error: %s", error)
            await asyncio.sleep(1)


async def enqueue_news(message, text):
    if not QUEUE_ENABLED:
        await process_news(message, text)
        return
    if len(news_queue) >= MAX_QUEUE:
        await message.answer("⛔ صف پردازش پر است. کمی بعد دوباره امتحان کن.")
        return
    news_queue.append({"user_id": message.from_user.id, "message": message, "text": text, "created": time.time()})
    editorial_stats["queue_max"] = max(editorial_stats.get("queue_max", 0), len(news_queue))
    save_editorial_state()
    await message.answer(f"📥 خبر وارد صف شد. جایگاه فعلی: <b>{len(news_queue)}</b>", parse_mode=ParseMode.HTML)


# ============================================================
# EDITORIAL CALLBACKS
# ============================================================

@router.callback_query(F.data == "edit_title")
async def edit_title_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    item = prepared.get(callback.from_user.id)
    if not item:
        await callback.answer("خبر آماده‌ای وجود ندارد.", show_alert=True); return
    item["awaiting_edit"] = "title"
    await callback.answer()
    await callback.message.answer("✏️ تیتر جدید را ارسال کن.")


@router.callback_query(F.data == "edit_body")
async def edit_body_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    item = prepared.get(callback.from_user.id)
    if not item:
        await callback.answer("خبر آماده‌ای وجود ندارد.", show_alert=True); return
    item["awaiting_edit"] = "body"
    await callback.answer()
    await callback.message.answer("✍️ متن کامل یک پاراگرافی جدید را ارسال کن.")


@router.callback_query(F.data == "rewrite_current")
async def rewrite_current_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    item = prepared.get(callback.from_user.id)
    if not item:
        await callback.answer("خبر آماده‌ای وجود ندارد.", show_alert=True); return
    await callback.answer("در حال بازنویسی...")
    try:
        generated = await rewrite_news_with_settings(item["source"], item["facts"], item.get("length", 7), item.get("mode", "standard"))
        title, sentences = split_sentences(generated)
        if len(sentences) != item.get("length", 7):
            await callback.message.answer("⚠️ بازنویسی دقیق نبود؛ دوباره امتحان کن."); return
        body = " ".join(sentences)
        post = build_custom_post(title, body, item["source"], item["facts"])
        item.update({"text": post, "title": title, "body": body})
        stat_inc("rewrites")
        await callback.message.answer(post, parse_mode=ParseMode.HTML, reply_markup=advanced_publish_keyboard())
    except Exception as error:
        await callback.message.answer("❌ بازنویسی ناموفق بود:\n" + str(error)[:1000])


# مسیر change_mode عمداً ثبت نمی‌شود؛ گزینه تغییر حالت در UI حذف شده است.
async def change_mode_callback_disabled(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    item = prepared.get(callback.from_user.id)
    if not item:
        await callback.answer("خبر آماده‌ای وجود ندارد.", show_alert=True); return
    item["awaiting_mode"] = True
    await callback.answer()
    await callback.message.answer("🧠 حالت را ارسال کن: standard / short / exciting")


@router.callback_query(F.data == "cancel_current")
async def cancel_current_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    prepared.pop(callback.from_user.id, None)
    await callback.answer("لغو شد.")
    await callback.message.answer("❌ خبر آماده انتشار لغو شد.", reply_markup=main_keyboard())


@router.callback_query(F.data == "dashboard")
async def dashboard_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    await callback.answer()
    await callback.message.answer(editorial_dashboard_text(), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


# ============================================================
# ADVANCED TEXT CONTROL
# ============================================================

async def handle_editorial_text(message):
    if not is_admin(message):
        return False
    item = prepared.get(message.from_user.id)
    if item and item.get("awaiting_edit"):
        kind = item.pop("awaiting_edit")
        old = item.get(kind, "")
        new = (message.text or "").strip()
        if not new:
            await message.answer("❌ متن خالی است."); return True
        if kind == "title":
            item["title"] = ensure_persian_start(new, True)
        else:
            item["body"] = clean_text(new)
        item["text"] = build_custom_post(item.get("title", ""), item.get("body", ""), item.get("source", {}), item.get("facts", {}))
        remember_admin_edit(old, new, kind)
        await message.answer(item["text"], parse_mode=ParseMode.HTML, reply_markup=advanced_publish_keyboard())
        return True
    if item and item.get("awaiting_mode"):
        item.pop("awaiting_mode", None)
        mode = normalize_mode(message.text)
        item["mode"] = mode
        try:
            generated = await rewrite_news_with_settings(item["source"], item["facts"], item.get("length", 7), mode)
            title, sentences = split_sentences(generated)
            if len(sentences) == item.get("length", 7):
                item["title"] = title; item["body"] = " ".join(sentences)
                item["text"] = build_custom_post(title, item["body"], item["source"], item["facts"])
            await message.answer(item["text"], parse_mode=ParseMode.HTML, reply_markup=advanced_publish_keyboard())
        except Exception as error:
            await message.answer("❌ تغییر حالت ناموفق بود: " + str(error)[:700])
        return True
    return False


# ============================================================
# COMMANDS / MENU EXTENSIONS
# ============================================================

@router.message(Command("dashboard"))
async def dashboard_command(message: Message):
    if not is_admin(message): return
    await message.answer(editorial_dashboard_text(), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


@router.message(Command("keys"))
async def keys_command(message: Message):
    if not is_admin(message): return
    await message.answer("🔑 <b>وضعیت کلیدهای OpenAI</b>\n\n" + "\n".join(key_health_snapshot()), parse_mode=ParseMode.HTML)


@router.message(Command("queue"))
async def queue_command(message: Message):
    if not is_admin(message): return
    await message.answer(f"📥 تعداد اخبار در صف: <b>{len(news_queue)}</b>\nحداکثر: <b>{MAX_QUEUE}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("learning"))
async def learning_command(message: Message):
    if not is_admin(message): return
    await message.answer(f"🧠 تعداد اصلاحات یادگرفته‌شده: <b>{len(editorial_learning)}</b>", parse_mode=ParseMode.HTML)


# ============================================================
# PUBLISH STATS WRAPPER
# ============================================================

_original_publish_news = publish_news

async def publish_news(message, user_id):
    await _original_publish_news(message, user_id)
    # فقط در صورت حذف شدن از prepared یعنی انتشار موفق بوده است.
    if user_id not in prepared:
        stat_inc("published")


# ============================================================
# END V5.3 ADVANCED ENGINE
# ============================================================

# ============================================================
# MAIN
# ============================================================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID تنظیم نشده است."
        )

    if not OPENAI_KEYS:
        raise RuntimeError(
            "هیچ OPENAI_API_KEY تنظیم نشده است."
        )

    load_memory()
    load_editorial_state()

    global queue_worker_task
    queue_worker_task = asyncio.create_task(queue_worker())

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("======================================")
    log.info("Gamefa Bot %s started", BOT_VERSION)
    log.info("OpenAI keys: %s", len(OPENAI_KEYS))
    log.info("Admin ID: %s", ADMIN_ID)
    log.info("Channel: %s", CHANNEL_ID)
    log.info("Model: %s", MODEL)
    log.info(
        "Web Search fallback: %s",
        ENABLE_WEB_SEARCH_FALLBACK,
    )
    log.info("Memory: %s", len(memory))
    log.info("======================================")

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


if __name__ == "__main__":
    asyncio.run(main())


# ============================================================
# V5.3.1 USER REQUEST GUARANTEES
# ============================================================
# این بخش عمداً به‌صورت صریح در کد ثبت شده تا رفتار نسخه قابل بررسی باشد.
# 1) خبرها هیچ هشتگی دریافت نمی‌کنند.
# 2) دکمه «انتشار در کانال» در رابط خبر وجود ندارد.
# 3) گزینه «تغییر حالت» در رابط خبر وجود ندارد.
# 4) فرمان /publish نیز به Router متصل نشده است.
# 5) نسخه جدید از v5.3.0 کوتاه‌تر نشده و ساختار قابلیت‌های قبلی حفظ شده است.
# 6) قابلیت‌های پردازش، ضدتکرار، تصویر، صف، داشبورد، Web Search،
#    ویرایش و بازنویسی همچنان در فایل باقی می‌مانند.

DISABLED_FEATURES_V531 = {
    "hashtags": True,
    "channel_publish_button": True,
    "change_mode_button": True,
    "publish_command": True,
}


def version_feature_policy():
    """سیاست قابلیت‌های غیرفعال نسخه 5.3.1."""
    return {
        "version": BOT_VERSION,
        "hashtags": "disabled",
        "channel_publish": "disabled",
        "mode_switch_ui": "disabled",
    }
