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
# VERSION
# ============================================================

BOT_VERSION = "v5.1.3"


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    "@Gamefa_official"
).strip()

MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.4-mini"
).strip()

AI_SOURCE_LIMIT = int(
    os.getenv("AI_SOURCE_LIMIT", "18000")
)

AI_MAX_OUTPUT_TOKENS = int(
    os.getenv("AI_MAX_OUTPUT_TOKENS", "1400")
)

MAX_MEMORY = 1500

MEMORY_FILE = Path("news_memory.json")

IMAGE_DIR = Path("gamefa_images")
IMAGE_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# ADMIN
# ============================================================

try:
    ADMIN_ID = int(
        os.getenv("ADMIN_ID", "0")
    )
except Exception:
    ADMIN_ID = 0


# ============================================================
# OPENAI KEYS
# ============================================================

OPENAI_KEYS = []

for i in range(1, 6):

    key = os.getenv(
        f"OPENAI_API_KEY_{i}",
        ""
    ).strip()

    if key:
        OPENAI_KEYS.append(key)


# پشتیبانی از کلید قدیمی
legacy_key = os.getenv(
    "OPENAI_API_KEY",
    ""
).strip()

if legacy_key and legacy_key not in OPENAI_KEYS:
    OPENAI_KEYS.insert(
        0,
        legacy_key
    )


OPENAI_CLIENTS = {}

OPENAI_KEY_INDEX = 0

OPENAI_KEY_COOLDOWN = {}


# ============================================================
# MEMORY
# ============================================================

memory = []

prepared = {}

processing_users = set()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(
    "gamefa_bot"
)


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
        r"try again in\s+([\d.]+)s"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.I
        )

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

            return (
                int(groups[0]) * 60
                + float(groups[1])
            )

        if len(groups) == 1:

            return float(groups[0])

    return 60.0


def openai_is_retryable(error):

    text = str(error or "").lower()

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
        "connection"
    )

    return any(
        word in text
        for word in retry_words
    )


async def openai_failover(callback):

    global OPENAI_KEY_INDEX

    if not OPENAI_KEYS:

        raise RuntimeError(
            "هیچ کلید OpenAI تنظیم نشده است."
        )

    last_error = None

    attempted = set()

    total_keys = len(
        OPENAI_KEYS
    )

    for offset in range(total_keys):

        index = (
            OPENAI_KEY_INDEX
            + offset
        ) % total_keys

        if index in attempted:
            continue

        if OPENAI_KEY_COOLDOWN.get(
            index,
            0
        ) > time.time():

            continue

        attempted.add(index)

        try:

            client = get_openai_client(
                index
            )

            result = await callback(
                client
            )

            OPENAI_KEY_INDEX = (
                index + 1
            ) % total_keys

            OPENAI_KEY_COOLDOWN.pop(
                index,
                None
            )

            return result

        except Exception as error:

            last_error = error

            if not openai_is_retryable(
                error
            ):
                raise

            wait = openai_retry_seconds(
                error
            )

            OPENAI_KEY_COOLDOWN[index] = (
                time.time()
                + min(
                    max(
                        wait,
                        30
                    ),
                    1800
                )
            )

            log.warning(
                "OpenAI key #%s unavailable. Switching key.",
                index + 1
            )

    raise RuntimeError(
        "تمام کلیدهای OpenAI فعلاً محدود یا نامعتبر هستند."
    ) from last_error


def is_rate_limit_error(error):

    text = str(error or "").lower()

    status = getattr(
        error,
        "status_code",
        None
    )

    return (
        status == 429
        or "429" in text
        or "rate limit" in text
        or "rate_limit" in text
        or "tokens per min" in text
        or "tpm" in text
        or "quota" in text
        or "insufficient_quota" in text
    )


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
            MEMORY_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(
            data,
            list
        ):

            memory = data[
                -MAX_MEMORY:
            ]

        else:

            memory = []

    except Exception as error:

        log.warning(
            "Memory load error: %s",
            error
        )

        memory = []


def save_memory():

    try:

        MEMORY_FILE.write_text(
            json.dumps(
                memory[
                    -MAX_MEMORY:
                ],
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    except Exception as error:

        log.warning(
            "Memory save error: %s",
            error
        )


# ============================================================
# TEXT
# ============================================================

def norm(text):

    text = text or ""

    text = re.sub(
        r"https?://\S+",
        " ",
        text
    )

    text = text.lower()

    text = re.sub(
        r"[^\w\u0600-\u06FF\s]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def word_similarity(a, b):

    words_a = set(
        norm(a).split()
    )

    words_b = set(
        norm(b).split()
    )

    if not words_a or not words_b:
        return 0

    return len(
        words_a & words_b
    ) / len(
        words_a | words_b
    )


def text_hash(text):

    return hashlib.sha256(
        norm(text).encode(
            "utf-8"
        )
    ).hexdigest()


def duplicate(
    text,
    title=""
):

    new_hash = text_hash(
        text
    )

    for item in memory:

        if item.get(
            "hash",
            ""
        ) == new_hash:

            return True

        old_title = item.get(
            "title",
            ""
        )

        old_source = item.get(
            "source",
            ""
        )

        if title and old_title:

            if word_similarity(
                title,
                old_title
            ) >= 0.88:

                return True

        if old_source:

            if word_similarity(
                text,
                old_source
            ) >= 0.84:

                return True

    return False


# ============================================================
# URL
# ============================================================

def extract_url(text):

    if not text:
        return None

    match = re.search(
        r"https?://[^\s<>()]+",
        text
    )

    if not match:
        return None

    return match.group(0).rstrip(
        ".,)]}"
    )


# ============================================================
# HTML
# ============================================================

def escape_html(text):

    return html.escape(
        text or "",
        quote=False
    )


def clean_text(text):

    return re.sub(
        r"\s+",
        " ",
        text or ""
    ).strip()


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

    return bool(
        ADMIN_ID
        and user_id == ADMIN_ID
    )


# ============================================================
# PERSIAN
# ============================================================

PERSIAN_RE = re.compile(
    r"[\u0600-\u06FF]"
)


def starts_with_persian(text):

    if not text:
        return False

    text = text.strip()

    text = re.sub(
        r"^[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫\s\-–—•]+",
        "",
        text
    ).strip()

    if not text:
        return False

    return bool(
        PERSIAN_RE.match(
            text[0]
        )
    )


def ensure_persian_start(
    text,
    is_title=False
):

    if not text:
        return text

    text = text.strip()

    if starts_with_persian(
        text
    ):
        return text

    if is_title:

        return (
            "گزارش جدید درباره "
            + text
        )

    return (
        "براساس گزارش منتشرشده، "
        + text
    )


# ============================================================
# TITLE
# ============================================================

def strip_site_branding_from_title(
    text
):

    if not text:
        return ""

    text = str(
        text
    ).strip()

    patterns = [
        r"\s*[|｜]\s*گیمفا\s*$",
        r"\s*[–—-]\s*گیمفا\s*$",
        r"^\s*گیمفا\s*[|｜:：-]\s*",
        r"\s*\(\s*گیمفا\s*\)\s*$",
        r"\s+گیمفا\s*$"
    ]

    for pattern in patterns:

        text = re.sub(
            pattern,
            "",
            text,
            flags=re.I
        )

    return text.strip(
        " |｜–—-:："
    )


# ============================================================
# CATEGORY
# ============================================================

def detect_category(text):

    text_lower = (
        text or ""
    ).lower()

    game_words = [
        "بازی",
        "گیم",
        "game",
        "gaming",
        "playstation",
        "xbox",
        "nintendo",
        "steam",
        "ps5",
        "ps4",
        "xbox series",
        "switch",
        "gta",
        "halo",
        "elden ring",
        "resident evil",
        "assassin"
    ]

    movie_words = [
        "فیلم",
        "سریال",
        "بازیگر",
        "movie",
        "film",
        "series",
        "season",
        "actor",
        "actress",
        "netflix",
        "hbo",
        "disney",
        "marvel",
        "dc",
        "cinema"
    ]

    if any(
        x in text_lower
        for x in game_words
    ):
        return "🎮"

    if any(
        x in text_lower
        for x in movie_words
    ):
        return "🎬"

    return "📢"


# ============================================================
# AI CLEAN
# ============================================================

def clean_ai_text(text):

    text = text or ""

    text = re.sub(
        r"\*\*(.*?)\*\*",
        r"\1",
        text,
        flags=re.S
    )

    text = re.sub(
        r"__(.*?)__",
        r"\1",
        text,
        flags=re.S
    )

    text = re.sub(
        r"`(.*?)`",
        r"\1",
        text,
        flags=re.S
    )

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    forbidden = [
        r"(?im)^\s*امتیاز دقت.*$",
        r"(?im)^\s*امتیاز ai.*$",
        r"(?im)^\s*accuracy score.*$",
        r"(?im)^\s*reviewer.*$",
        r"(?im)^\s*اطلاعات استخراج شده.*$",
        r"(?im)^\s*طبق بررسی ai.*$",
        r"(?im)^\s*هوش مصنوعی.*$"
    ]

    for pattern in forbidden:

        text = re.sub(
            pattern,
            "",
            text
        )

    text = re.sub(
        r"(?im)^\s*(?:🆔\s*)?@Gamefa_official\s*$",
        "",
        text
    )

    text = re.sub(
        r"(?m)^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]\s*",
        "",
        text
    )

    return text.strip()


# ============================================================
# GAMEFA FETCH
# ============================================================

REMOVE_SELECTORS = [
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "footer",
    "form",
    "aside",
    "header",
    "iframe",
    "video",
    "audio",
    "canvas",
    ".related-posts",
    ".related-post",
    ".related",
    ".recommended",
    ".recommendations",
    ".recommended-posts",
    ".more-posts",
    ".latest-posts",
    ".popular-posts",
    ".author-box",
    ".author-info",
    ".author-card",
    ".comments",
    ".comment",
    ".comment-list",
    ".advertisement",
    ".ads",
    ".ad",
    ".banner",
    ".newsletter",
    ".social-share",
    ".share-buttons",
    ".breadcrumb",
    ".breadcrumbs",
    ".sidebar",
    ".widget",
    ".read-more",
    ".post-navigation",
    ".navigation"
]


def remove_unwanted_elements(
    soup
):

    for selector in REMOVE_SELECTORS:

        try:

            for element in soup.select(
                selector
            ):

                element.decompose()

        except Exception:
            pass


def is_probably_noise(text):

    low = (
        text or ""
    ).lower()

    words = [
        "مطالب مرتبط",
        "مطالب پیشنهادی",
        "اخبار مرتبط",
        "بیشتر بخوانید",
        "related posts",
        "related articles",
        "recommended",
        "subscribe",
        "newsletter",
        "تبلیغات",
        "advertisement",
        "نویسنده",
        "author",
        "دیدگاه",
        "comments",
        "comment",
        "share"
    ]

    return any(
        word in low
        for word in words
    )


async def fetch_gamefa(
    url
):

    parsed = urlparse(
        url
    )

    host = parsed.netloc.lower()

    if "gamefa.com" not in host:

        raise ValueError(
            "فقط لینک Gamefa پشتیبانی می‌شود."
        )

    headers = {
        "User-Agent":
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/151 Safari/537.36",

        "Accept-Language":
            "fa-IR,fa;q=0.9,en;q=0.8"
    }

    timeout = aiohttp.ClientTimeout(
        total=45
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        async with session.get(
            url,
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            final_url = str(
                response.url
            )

            raw = await response.text(
                errors="ignore"
            )

    soup = BeautifulSoup(
        raw,
        "html.parser"
    )

    remove_unwanted_elements(
        soup
    )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = ""

    h1 = soup.find(
        "h1"
    )

    if h1:

        title = clean_text(
            h1.get_text(
                " ",
                strip=True
            )
        )

    elif soup.title:

        title = clean_text(
            soup.title.get_text(
                " ",
                strip=True
            )
        )

    title = strip_site_branding_from_title(
        title
    )

    # --------------------------------------------------------
    # DESCRIPTION
    # --------------------------------------------------------

    description = ""

    meta_options = [
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"}
    ]

    for attrs in meta_options:

        meta = soup.find(
            "meta",
            attrs=attrs
        )

        if meta and meta.get(
            "content"
        ):

            description = clean_text(
                meta["content"]
            )

            break

    # --------------------------------------------------------
    # IMAGE
    # --------------------------------------------------------

    image_candidates = []

    image_meta = [
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"}
    ]

    for attrs in image_meta:

        meta = soup.find(
            "meta",
            attrs=attrs
        )

        if meta and meta.get(
            "content"
        ):

            image_candidates.append(
                urljoin(
                    final_url,
                    meta["content"].strip()
                )
            )

    # --------------------------------------------------------
    # ARTICLE
    # --------------------------------------------------------

    article = None

    selectors = [
        "article",
        "[itemprop='articleBody']",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".single-post-content",
        ".td-post-content",
        ".post-body",
        ".content-area"
    ]

    for selector in selectors:

        candidate = soup.select_one(
            selector
        )

        if candidate:

            article = candidate

            break

    if article is None:

        article = soup

    # --------------------------------------------------------
    # PARAGRAPHS
    # --------------------------------------------------------

    body_parts = []

    seen = set()

    for paragraph in article.find_all(
        [
            "p",
            "h2",
            "h3",
            "h4"
        ]
    ):

        text = clean_text(
            paragraph.get_text(
                " ",
                strip=True
            )
        )

        if len(text) < 35:
            continue

        if is_probably_noise(
            text
        ):
            continue

        key = norm(
            text
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        body_parts.append(
            text
        )

    if len(body_parts) < 3:

        body_parts = []

        for paragraph in soup.find_all(
            "p"
        ):

            text = clean_text(
                paragraph.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                len(text) >= 35
                and not is_probably_noise(
                    text
                )
            ):

                body_parts.append(
                    text
                )

    body = "\n".join(
        body_parts
    )

    body = body[:70000]

    # --------------------------------------------------------
    # IMAGE FALLBACK
    # --------------------------------------------------------

    if not image_candidates:

        for img in article.find_all(
            "img"
        ):

            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
            )

            if not src:
                continue

            image_candidates.append(
                urljoin(
                    final_url,
                    src
                )
            )

            break

    image = (
        image_candidates[0]
        if image_candidates
        else ""
    )

    return {
        "url": final_url,
        "title": title,
        "description": description,
        "body": body,
        "image": image
    }


# ============================================================
# FACT EXTRACTION
# ============================================================

FACT_PROMPT = """
تو سیستم استخراج اطلاعات تحریریه Gamefa هستی.

وظیفه تو تولید خبر نیست.

فقط واقعیت‌های مهم و مستقیم مقاله را استخراج کن.

مطالب مرتبط، تبلیغات، نویسنده، Reviewer، باکس‌های سایت و مطالب جانبی را نادیده بگیر.

اگر تاریخ، زمان، عدد، حجم، قیمت، پلتفرم، بازیگر، کارگردان، سازنده، ناشر یا وضعیت پروژه وجود دارد، حتماً استخراج کن.

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


def local_facts(
    source
):

    body = clean_text(
        source.get(
            "body",
            ""
        )
    )

    sentences = re.split(
        r"(?<=[.!؟])\s+",
        body
    )

    sentences = [
        x.strip()
        for x in sentences
        if len(x.strip()) > 20
    ]

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?(?:\s*(?:GB|TB|درصد|%))?",
        body,
        flags=re.I
    )

    return {
        "main_topic": source.get(
            "title",
            ""
        ),

        "main_event": (
            sentences[0]
            if sentences
            else source.get(
                "title",
                ""
            )
        ),

        "facts": [
            {
                "fact": sentence,
                "importance": 4,
                "type": "article"
            }
            for sentence in sentences[:15]
        ],

        "dates": [],
        "platforms": [],
        "numbers": numbers[:20],
        "people": [],
        "companies": [],
        "status": "",
        "important_missing": []
    }


async def extract_facts(
    source
):

    input_text = (
        "عنوان:\n"
        + source.get("title", "")
        + "\n\n"
        "توضیحات:\n"
        + source.get("description", "")
        + "\n\n"
        "متن مقاله:\n"
        + source.get("body", "")[
            :AI_SOURCE_LIMIT
        ]
    )

    try:

        response = await openai_failover(
            lambda client:
            client.responses.create(
                model=MODEL,
                instructions=FACT_PROMPT,
                input=input_text,
                max_output_tokens=1200
            )
        )

        raw = (
            response.output_text
            or ""
        ).strip()

        raw = re.sub(
            r"^```json\s*",
            "",
            raw,
            flags=re.I
        )

        raw = re.sub(
            r"\s*```$",
            "",
            raw
        )

        try:

            data = json.loads(
                raw
            )

        except Exception:

            start = raw.find(
                "{"
            )

            end = raw.rfind(
                "}"
            )

            if start == -1 or end == -1:

                raise ValueError(
                    "JSON نامعتبر از AI دریافت شد."
                )

            data = json.loads(
                raw[
                    start:end + 1
                ]
            )

        if not isinstance(
            data,
            dict
        ):

            return local_facts(
                source
            )

        return data

    except Exception as error:

        log.warning(
            "Fact extraction failed: %s",
            error
        )

        return local_facts(
            source
        )


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


def clean_sentence(
    sentence
):

    sentence = sentence.strip()

    sentence = re.sub(
        r"^[•\-–—\d.)]+\s*",
        "",
        sentence
    )

    sentence = re.sub(
        r"^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]+\s*",
        "",
        sentence
    )

    return sentence.strip()


def split_sentences(
    text
):

    text = clean_ai_text(
        text
    )

    lines = [
        x.strip()
        for x in text.replace(
            "\r",
            "\n"
        ).splitlines()
        if x.strip()
    ]

    if not lines:
        return "", []

    title = lines[0]

    body = " ".join(
        lines[1:]
    )

    body = re.sub(
        r"\s+",
        " ",
        body
    ).strip()

    sentences = re.split(
        r"(?<=[.!؟])\s+",
        body
    )

    sentences = [
        clean_sentence(x)
        for x in sentences
        if clean_sentence(x)
    ]

    return title, sentences


def local_news_fallback(
    source,
    facts
):

    title = strip_site_branding_from_title(
        clean_text(
            source.get(
                "title",
                ""
            )
        )
    )

    title = ensure_persian_start(
        title or "خبر جدید",
        True
    )

    pool = []

    for item in facts.get(
        "facts",
        []
    ):

        if isinstance(
            item,
            dict
        ):

            fact = clean_sentence(
                str(
                    item.get(
                        "fact",
                        ""
                    )
                )
            )

            if fact:
                pool.append(
                    fact
                )

    raw = clean_text(
        source.get(
            "body",
            ""
        )
    )

    body_parts = re.split(
        r"(?<=[.!؟])\s+",
        raw
    )

    for item in body_parts:

        item = clean_sentence(
            item
        )

        if len(item) >= 25:

            pool.append(
                item
            )

    unique = []

    seen = set()

    for item in pool:

        key = norm(
            item
        )

        if not key or key in seen:
            continue

        seen.add(
            key
        )

        unique.append(
            ensure_persian_start(
                item,
                False
            )
        )

    while len(unique) < 7:

        unique.append(
            "این خبر جزئیات بیشتری درباره موضوع اصلی ارائه می‌کند."
        )

    return (
        title
        + "\n"
        + " ".join(
            unique[:7]
        )
    )


async def generate_news(
    source,
    facts
):

    facts_json = json.dumps(
        facts,
        ensure_ascii=False,
        separators=(",", ":")
    )

    input_text = (
        "FACTS:\n"
        + facts_json
        + "\n\nعنوان:\n"
        + source.get(
            "title",
            ""
        )
        + "\n\nمتن اصلی:\n"
        + source.get(
            "body",
            ""
        )[:AI_SOURCE_LIMIT]
    )

    try:

        response = await openai_failover(
            lambda client:
            client.responses.create(
                model=MODEL,
                instructions=NEWS_PROMPT,
                input=input_text,
                max_output_tokens=AI_MAX_OUTPUT_TOKENS
            )
        )

        result = (
            response.output_text
            or ""
        ).strip()

        if not result:

            raise RuntimeError(
                "AI خروجی خالی تولید کرد."
            )

        return result

    except Exception as error:

        log.warning(
            "News generation failed: %s",
            error
        )

        return local_news_fallback(
            source,
            facts
        )


# ============================================================
# VALIDATION
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
    "fact"
]


def validate_generated_output(
    generated
):

    title, sentences = split_sentences(
        generated
    )

    if not title:
        return False

    if len(sentences) != 7:
        return False

    combined = (
        title
        + " "
        + " ".join(sentences)
    ).lower()

    for term in FORBIDDEN_OUTPUT_TERMS:

        if term.lower() in combined:

            return False

    if not starts_with_persian(
        title
    ):

        return False

    for sentence in sentences:

        if not starts_with_persian(
            sentence
        ):

            return False

    return True


# ============================================================
# FORMAT POST
# ============================================================

def format_post(
    generated
):

    generated = clean_ai_text(
        generated
    )

    title, sentences = split_sentences(
        generated
    )

    if len(sentences) != 7:

        return ""

    title = strip_site_branding_from_title(
        clean_sentence(
            title
        )
    )

    title = ensure_persian_start(
        title,
        True
    )

    fixed = []

    for sentence in sentences:

        sentence = ensure_persian_start(
            clean_sentence(sentence),
            False
        )

        fixed.append(
            sentence
        )

    category = detect_category(
        title
        + " "
        + " ".join(fixed)
    )

    title = (
        category
        + " "
        + title
    )

    body = " ".join(
        fixed
    )

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

async def download_image(
    url
):

    if not url:
        return None

    try:

        headers = {
            "User-Agent":
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151 Safari/537.36"
        }

        timeout = aiohttp.ClientTimeout(
            total=35
        )

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                allow_redirects=True
            ) as response:

                if response.status != 200:
                    return None

                data = await response.read()

                content_type = (
                    response.headers.get(
                        "Content-Type",
                        ""
                    ).lower()
                )

        if not data:
            return None

        if len(data) < 1000:
            return None

        if len(data) > 15 * 1024 * 1024:
            return None

        extension = Path(
            urlparse(url).path
        ).suffix.lower()

        if extension not in (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp"
        ):

            if "png" in content_type:
                extension = ".png"

            elif "webp" in content_type:
                extension = ".webp"

            else:
                extension = ".jpg"

        filename = (
            "gamefa_"
            + hashlib.md5(
                url.encode(
                    "utf-8"
                )
            ).hexdigest()
            + extension
        )

        path = (
            IMAGE_DIR
            / filename
        )

        path.write_bytes(
            data
        )

        return path

    except Exception as error:

        log.warning(
            "Image download error: %s",
            error
        )

        return None


async def find_best_image(
    source
):

    image = source.get(
        "image",
        ""
    )

    if not image:
        return None

    return await download_image(
        image
    )


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🔎 بررسی خبر جدید"
                ),
                KeyboardButton(
                    text="📁 آرشیو"
                )
            ],
            [
                KeyboardButton(
                    text="📊 آمار"
                ),
                KeyboardButton(
                    text="⚙️ تنظیمات"
                )
            ]
        ],
        resize_keyboard=True
    )


def news_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📝 ارسال خبر"
                ),
                KeyboardButton(
                    text="🔗 ارسال لینک Gamefa"
                )
            ],
            [
                KeyboardButton(
                    text="🔙 بازگشت"
                )
            ]
        ],
        resize_keyboard=True
    )


def archive_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📚 آخرین اخبار"
                ),
                KeyboardButton(
                    text="🗑 پاکسازی آرشیو"
                )
            ],
            [
                KeyboardButton(
                    text="🔙 بازگشت"
                )
            ]
        ],
        resize_keyboard=True
    )


def settings_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📢 کانال انتشار"
                ),
                KeyboardButton(
                    text="🧠 مدل AI"
                )
            ],
            [
                KeyboardButton(
                    text="🖼 سیستم تصویر"
                ),
                KeyboardButton(
                    text="✍️ قالب خبر"
                )
            ],
            [
                KeyboardButton(
                    text="🔙 بازگشت"
                )
            ]
        ],
        resize_keyboard=True
    )


def publish_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 انتشار در کانال",
                    callback_data="publish_current"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت",
                    callback_data="home"
                )
            ]
        ]
    )


# ============================================================
# PROCESS NEWS
# ============================================================

async def process_news(
    message,
    text
):

    user_id = message.from_user.id

    if user_id in processing_users:

        await message.answer(
            "⏳ یک خبر در حال پردازش است."
        )

        return

    processing_users.add(
        user_id
    )

    status = None

    image_path = None

    try:

        url = extract_url(
            text
        )

        if url:

            status = await message.answer(
                "⏳ در حال دریافت مقاله Gamefa..."
            )

            source = await fetch_gamefa(
                url
            )

            await status.edit_text(
                "🧠 مقاله دریافت شد.\n"
                "در حال استخراج اطلاعات..."
            )

        else:

            source = {
                "url": "",
                "title": "",
                "description": "",
                "body": text,
                "image": ""
            }

        duplicate_text = (
            source.get(
                "title",
                ""
            )
            + "\n"
            + source.get(
                "body",
                ""
            )
        )

        if duplicate(
            duplicate_text,
            source.get(
                "title",
                ""
            )
        ):

            if status:

                await status.delete()

            await message.answer(
                "⚠️ این خبر یا خبر بسیار مشابه آن قبلاً در آرشیو وجود دارد.",
                reply_markup=main_keyboard()
            )

            return

        facts = await extract_facts(
            source
        )

        if status:

            await status.edit_text(
                "🧠 اطلاعات اصلی استخراج شد.\n"
                "در حال ساخت خبر..."
            )

        generated = await generate_news(
            source,
            facts
        )

        if not validate_generated_output(
            generated
        ):

            generated = local_news_fallback(
                source,
                facts
            )

        post = format_post(
            generated
        )

        if not post:

            generated = local_news_fallback(
                source,
                facts
            )

            post = format_post(
                generated
            )

        if not post:

            raise RuntimeError(
                "تولید متن نهایی ناموفق بود."
            )

        image_path = await find_best_image(
            source
        )

        memory.append(
            {
                "hash": text_hash(
                    duplicate_text
                ),
                "title": source.get(
                    "title",
                    ""
                ),
                "source": duplicate_text[
                    :25000
                ],
                "post": post,
                "url": url or ""
            }
        )

        memory[:] = memory[
            -MAX_MEMORY:
        ]

        save_memory()

        prepared[user_id] = {
            "text": post,
            "image": (
                str(image_path)
                if image_path
                else ""
            )
        }

        if status:

            try:
                await status.delete()
            except Exception:
                pass

        # ----------------------------------------------------
        # PREVIEW
        # ----------------------------------------------------

        if image_path and len(post) <= 1024:

            try:

                await message.answer_photo(
                    FSInputFile(
                        image_path
                    ),
                    caption=post,
                    parse_mode=ParseMode.HTML,
                    reply_markup=publish_keyboard()
                )

            except Exception as error:

                log.warning(
                    "Photo preview failed: %s",
                    error
                )

                await message.answer(
                    post,
                    parse_mode=ParseMode.HTML,
                    reply_markup=publish_keyboard()
                )

        elif image_path:

            try:

                await message.answer_photo(
                    FSInputFile(
                        image_path
                    )
                )

            except Exception as error:

                log.warning(
                    "Image preview failed: %s",
                    error
                )

            await message.answer(
                post,
                parse_mode=ParseMode.HTML,
                reply_markup=publish_keyboard()
            )

        else:

            await message.answer(
                post,
                parse_mode=ParseMode.HTML,
                reply_markup=publish_keyboard()
            )

        await message.answer(
            "✅ خبر آماده انتشار است.",
            reply_markup=main_keyboard()
        )

    except Exception as error:

        log.exception(
            "News processing error"
        )

        if status:

            try:
                await status.delete()
            except Exception:
                pass

        await message.answer(
            "❌ خطا هنگام پردازش خبر:\n\n"
            + str(error)[:1500],
            reply_markup=main_keyboard()
        )

    finally:

        processing_users.discard(
            user_id
        )


# ============================================================
# PUBLISH
# ============================================================

async def publish_news(
    message,
    user_id
):

    item = prepared.get(
        user_id
    )

    if not item:

        await message.answer(
            "❌ خبری برای انتشار آماده نیست."
        )

        return

    text = item.get(
        "text",
        ""
    )

    image = item.get(
        "image",
        ""
    )

    try:

        if (
            image
            and Path(image).exists()
        ):

            if len(text) <= 1024:

                await message.bot.send_photo(
                    CHANNEL_ID,
                    FSInputFile(
                        image
                    ),
                    caption=text,
                    parse_mode=ParseMode.HTML
                )

            else:

                await message.bot.send_photo(
                    CHANNEL_ID,
                    FSInputFile(
                        image
                    )
                )

                await message.bot.send_message(
                    CHANNEL_ID,
                    text,
                    parse_mode=ParseMode.HTML
                )

        else:

            await message.bot.send_message(
                CHANNEL_ID,
                text,
                parse_mode=ParseMode.HTML
            )

        prepared.pop(
            user_id,
            None
        )

        await message.answer(
            "✅ خبر با موفقیت در کانال منتشر شد.",
            reply_markup=main_keyboard()
        )

    except Exception as error:

        log.exception(
            "Publish error"
        )

        await message.answer(
            "❌ خطا هنگام انتشار:\n\n"
            + str(error)[:1500],
            reply_markup=main_keyboard()
        )


# ============================================================
# ROUTER
# ============================================================

router = Router()


# ============================================================
# START
# ============================================================

@router.message(
    Command("start")
)
async def start_handler(
    message: Message
):

    if not is_admin(message):

        await message.answer(
            "⛔ این ربات خصوصی است."
        )

        return

    await message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>\n\n"
        f"نسخه: <b>{BOT_VERSION}</b>\n\n"
        "ربات آماده دریافت خبر است.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# ============================================================
# PUBLISH CALLBACK
# ============================================================

@router.callback_query(
    F.data == "publish_current"
)
async def publish_callback(
    callback
):

    if not is_admin_id(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )

        return

    await callback.answer(
        "در حال انتشار..."
    )

    await publish_news(
        callback.message,
        callback.from_user.id
    )


@router.callback_query(
    F.data == "home"
)
async def home_callback(
    callback
):

    if not is_admin_id(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# ============================================================
# MENU
# ============================================================

@router.message(
    F.text == "🔎 بررسی خبر جدید"
)
async def news_menu(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🔎 <b>بررسی خبر جدید</b>\n\n"
        "لینک Gamefa یا متن خبر را ارسال کن.",
        parse_mode=ParseMode.HTML,
        reply_markup=news_keyboard()
    )


@router.message(
    F.text == "📝 ارسال خبر"
)
async def text_news(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📝 متن خبر را ارسال کن."
    )


@router.message(
    F.text == "🔗 ارسال لینک Gamefa"
)
async def link_news(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🔗 لینک مقاله Gamefa را ارسال کن."
    )


# ============================================================
# ARCHIVE
# ============================================================

@router.message(
    F.text == "📁 آرشیو"
)
async def archive(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📁 <b>آرشیو</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=archive_keyboard()
    )


@router.message(
    F.text == "📚 آخرین اخبار"
)
async def latest_news(
    message: Message
):

    if not is_admin(message):
        return

    if not memory:

        await message.answer(
            "📚 آرشیو خالی است."
        )

        return

    lines = [
        "📚 <b>آخرین اخبار</b>",
        ""
    ]

    for index, item in enumerate(
        reversed(memory[-10:]),
        1
    ):

        title = item.get(
            "title",
            "خبر بدون عنوان"
        )

        lines.append(
            f"{index}. {escape_html(title[:120])}"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=archive_keyboard()
    )


# ============================================================
# STATS
# ============================================================

@router.message(
    F.text == "📊 آمار"
)
async def stats(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📊 <b>آمار ربات</b>\n\n"
        f"📰 آرشیو: <b>{len(memory)}</b>\n"
        f"💾 ظرفیت: <b>{MAX_MEMORY}</b>\n"
        f"🧠 مدل: <code>{escape_html(MODEL)}</code>\n"
        f"🔑 کلیدهای OpenAI: <b>{len(OPENAI_KEYS)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# ============================================================
# SETTINGS
# ============================================================

@router.message(
    F.text == "⚙️ تنظیمات"
)
async def settings(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "⚙️ <b>تنظیمات</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard()
    )


@router.message(
    F.text == "📢 کانال انتشار"
)
async def channel_setting(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📢 کانال:\n\n"
        f"<code>{escape_html(CHANNEL_ID)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard()
    )


@router.message(
    F.text == "🧠 مدل AI"
)
async def model_setting(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🧠 مدل:\n\n"
        f"<code>{escape_html(MODEL)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard()
    )


@router.message(
    F.text == "🖼 سیستم تصویر"
)
async def image_setting(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🖼 سیستم تصویر\n\n"
        "ربات ابتدا og:image مقاله را دانلود می‌کند.\n"
        "در صورت شکست، خبر بدون تصویر منتشر می‌شود.",
        reply_markup=settings_keyboard()
    )


@router.message(
    F.text == "✍️ قالب خبر"
)
async def format_setting(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "✍️ قالب خبر:\n\n"
        "• تیتر فارسی\n"
        "• ۷ جمله\n"
        "• یک پاراگراف\n"
        "• اطلاعات مهم\n"
        "• تصویر مقاله\n"
        "• امضای Gamefa",
        reply_markup=settings_keyboard()
    )


# ============================================================
# BACK
# ============================================================

@router.message(
    F.text == "🔙 بازگشت"
)
async def back(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


# ============================================================
# COMMANDS
# ============================================================

@router.message(
    Command("publish")
)
async def publish_command(
    message: Message
):

    if not is_admin(message):
        return

    await publish_news(
        message,
        message.from_user.id
    )


@router.message(
    Command("stats")
)
async def stats_command(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        f"📊 تعداد اخبار آرشیو: {len(memory)}"
    )


@router.message(
    Command("clear")
)
async def clear_command(
    message: Message
):

    if not is_admin(message):
        return

    memory.clear()

    prepared.clear()

    save_memory()

    await message.answer(
        "✅ آرشیو پاک شد.",
        reply_markup=main_keyboard()
    )


# ============================================================
# TEXT HANDLER
# ============================================================

@router.message(
    F.text
)
async def text_handler(
    message: Message
):

    if not is_admin(message):
        return

    text = (
        message.text
        or ""
    ).strip()

    if not text:
        return

    if text.startswith("/"):
        return

    menu_words = {
        "🔎 بررسی خبر جدید",
        "📁 آرشیو",
        "📊 آمار",
        "⚙️ تنظیمات",
        "📝 ارسال خبر",
        "🔗 ارسال لینک Gamefa",
        "📚 آخرین اخبار",
        "🗑 پاکسازی آرشیو",
        "📢 کانال انتشار",
        "🧠 مدل AI",
        "🖼 سیستم تصویر",
        "✍️ قالب خبر",
        "🔙 بازگشت"
    }

    if text in menu_words:
        return

    await process_news(
        message,
        text
    )


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

    bot = Bot(
        token=BOT_TOKEN
    )

    dp = Dispatcher()

    dp.include_router(
        router
    )

    log.info(
        "======================================"
    )

    log.info(
        "Gamefa Bot %s started",
        BOT_VERSION
    )

    log.info(
        "OpenAI keys: %s",
        len(OPENAI_KEYS)
    )

    log.info(
        "Admin ID: %s",
        ADMIN_ID
    )

    log.info(
        "Channel: %s",
        CHANNEL_ID
    )

    log.info(
        "Model: %s",
        MODEL
    )

    log.info(
        "Memory: %s",
        len(memory)
    )

    log.info(
        "======================================"
    )

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )