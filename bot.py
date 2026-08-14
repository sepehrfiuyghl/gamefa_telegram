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

import aiohttp
from bs4 import BeautifulSoup

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

BOT_VERSION = "v5.2.0"

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


def publish_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 انتشار در کانال",
                    callback_data="publish_current",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت",
                    callback_data="home",
                )
            ],
        ]
    )


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
                    reply_markup=publish_keyboard(),
                )
            except Exception as error:
                log.warning(
                    "Photo preview failed: %s",
                    error,
                )
                await message.answer(
                    post,
                    parse_mode=ParseMode.HTML,
                    reply_markup=publish_keyboard(),
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
                reply_markup=publish_keyboard(),
            )

        else:
            await message.answer(
                post,
                parse_mode=ParseMode.HTML,
                reply_markup=publish_keyboard(),
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


@router.callback_query(F.data == "publish_current")
async def publish_callback(callback):
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

@router.message(Command("publish"))
async def publish_command(message: Message):
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
