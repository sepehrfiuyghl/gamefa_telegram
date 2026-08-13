import os
import re
import json
import html
import asyncio
import logging
import hashlib
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

from google import genai
from google.genai import types


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    "@Gamefa_official"
).strip()

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
).strip()

try:
    ADMIN_ID = int(
        os.getenv("ADMIN_ID", "0") or "0"
    )
except (ValueError, TypeError):
    ADMIN_ID = 0

MEMORY_FILE = Path("news_memory.json")

MAX_MEMORY = 1500

IMAGE_DIR = Path("gamefa_images")
IMAGE_DIR.mkdir(
    parents=True,
    exist_ok=True
)

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

log = logging.getLogger("gamefa_bot")


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

        if isinstance(data, list):
            memory = data[-MAX_MEMORY:]
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
                memory[-MAX_MEMORY:],
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
# TEXT NORMALIZATION
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


def similarity(a, b):

    return word_similarity(
        a,
        b
    )


def text_hash(text):

    normalized = norm(text)

    return hashlib.sha256(
        normalized.encode(
            "utf-8"
        )
    ).hexdigest()


def duplicate(text, title=""):

    new_text = text or ""
    new_title = title or ""

    new_hash = text_hash(
        new_text
    )

    for item in memory:

        old_hash = item.get(
            "hash",
            ""
        )

        if old_hash and old_hash == new_hash:
            return True

        old_source = item.get(
            "source",
            ""
        )

        old_title = item.get(
            "title",
            ""
        )

        if new_title and old_title:

            title_score = similarity(
                new_title,
                old_title
            )

            if title_score >= 0.88:
                return True

        if old_source:

            source_score = similarity(
                new_text,
                old_source
            )

            if source_score >= 0.84:
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

    text = text or ""

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


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
# PERSIAN START
# ============================================================

PERSIAN_RE = re.compile(
    r"[\u0600-\u06FF]"
)


def starts_with_persian(text):

    if not text:
        return False

    clean = text.strip()

    # حذف علائم و ایموجی‌های ابتدایی
    clean = re.sub(
        r"^[🎮🎬📱🟣📢📰🔵🟢🟡🟠⚪⚫\s\-\–—•]+",
        "",
        clean
    ).strip()

    if not clean:
        return False

    return bool(
        PERSIAN_RE.match(
            clean[0]
        )
    )


def make_persian_start(
    text,
    is_title=False
):

    if not text:
        return text

    text = text.strip()

    if starts_with_persian(text):
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
# ENSURE PERSIAN START
# ============================================================

def ensure_persian_start(
    text,
    is_title=False
):

    """
    اگر متن با انگلیسی شروع شده باشد،
    قبل از آن یک عبارت فارسی مناسب قرار می‌دهد.
    """

    if not text:
        return text

    text = text.strip()

    # حذف علامت‌های اضافی
    text = re.sub(
        r"^[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫\s]+",
        "",
        text
    ).strip()

    if starts_with_persian(text):
        return text

    if is_title:
        return "گزارش جدید درباره " + text

    return "براساس گزارش منتشرشده، " + text


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
        "doom",
        "gta",
        "resident evil",
        "halo",
        "final fantasy",
        "devil may cry",
        "assassin",
        "elden ring",
        "sony",
        "microsoft",
        "ps5",
        "ps4",
        "xbox series",
        "switch"
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
        word in text_lower
        for word in game_words
    ):
        return "🎮"

    if any(
        word in text_lower
        for word in movie_words
    ):
        return "🎬"

    return "📢"


# ============================================================
# AI CLEANER
# ============================================================

def clean_ai_text(text):

    text = text or ""

    # Markdown bold
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

    # Italic
    text = re.sub(
        r"\*(.*?)\*",
        r"\1",
        text,
        flags=re.S
    )

    # Code
    text = re.sub(
        r"`(.*?)`",
        r"\1",
        text,
        flags=re.S
    )

    # Markdown links
    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    forbidden_patterns = [
        r"(?im)^\s*امتیاز دقت.*$",
        r"(?im)^\s*امتیاز ai.*$",
        r"(?im)^\s*امتیاز هوش مصنوعی.*$",
        r"(?im)^\s*اطلاعاتی که reviewer.*$",
        r"(?im)^\s*reviewer.*$",
        r"(?im)^\s*ai score.*$",
        r"(?im)^\s*accuracy score.*$",
        r"(?im)^\s*اطلاعات استخراج شده.*$",
        r"(?im)^\s*اطلاعات بررسی شده.*$",
        r"(?im)^\s*متن کامل صفحه.*$",
        r"(?im)^\s*مقاله شامل.*$",
        r"(?im)^\s*طبق بررسی ai.*$",
        r"(?im)^\s*هوش مصنوعی.*$"
    ]

    for pattern in forbidden_patterns:

        text = re.sub(
            pattern,
            "",
            text
        )

    # Channel
    text = re.sub(
        r"(?im)^\s*(?:🆔\s*)?@Gamefa_official\s*$",
        "",
        text
    )

    # Emojis at line beginning
    text = re.sub(
        r"(?m)^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]\s*",
        "",
        text
    )

    return text.strip()


# ============================================================
# ARTICLE CLEANING
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
    ".wp-block-latest-posts",
    ".read-more",
    ".post-navigation",
    ".navigation"
]


def remove_unwanted_elements(soup):

    for selector in REMOVE_SELECTORS:

        try:

            for element in soup.select(
                selector
            ):
                element.decompose()

        except Exception:
            pass


def is_probably_noise(text):

    if not text:
        return True

    low = text.lower()

    noise_words = [
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

    if any(
        word in low
        for word in noise_words
    ):
        return True

    return False


# ============================================================
# GAMEFA FETCH (MODIFIED & ROBUST)
# ============================================================

async def fetch_gamefa(url):

    parsed = urlparse(url)

    if "gamefa.com" not in (
        parsed.netloc.lower()
    ):
        raise ValueError(
            "فقط لینک Gamefa پشتیبانی می‌شود."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7"
    }

    timeout = aiohttp.ClientTimeout(
        total=45,
        connect=30
    )

    try:
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
    except asyncio.TimeoutError:
        log.error("Timeout Error when connecting to Gamefa: %s", url)
        raise RuntimeError("تایم‌آوت در برقراری ارتباط با سایت گیمفا.")
    except aiohttp.ClientError as e:
        log.error("Network Error when fetching Gamefa: %s", e)
        raise RuntimeError(f"خطای شبکه در دریافت اخبار گیمفا: {e}")

    soup = BeautifulSoup(
        raw,
        "html.parser"
    )

    remove_unwanted_elements(
        soup
    )

    # ========================================================
    # TITLE
    # ========================================================

    title = ""

    h1 = soup.find("h1")

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

    # ========================================================
    # DESCRIPTION
    # ========================================================

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

    # ========================================================
    # IMAGE
    # ========================================================

    image_candidates = []

    for attrs in [
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"}
    ]:

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

    # ========================================================
    # ARTICLE
    # ========================================================

    article = None

    article_selectors = [
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

    for selector in article_selectors:

        candidate = soup.select_one(
            selector
        )

        if candidate:

            article = candidate
            break

    if article is None:
        article = soup

    # ========================================================
    # PARAGRAPHS
    # ========================================================

    paragraphs = article.find_all(
        [
            "p",
            "h2",
            "h3",
            "h4"
        ]
    )

    body_parts = []

    seen_paragraphs = set()

    for paragraph in paragraphs:

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

        paragraph_key = norm(
            text
        )

        if paragraph_key in seen_paragraphs:
            continue

        seen_paragraphs.add(
            paragraph_key
        )

        body_parts.append(
            text
        )

    # ========================================================
    # FALLBACK
    # ========================================================

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

            if len(text) >= 35:

                if not is_probably_noise(
                    text
                ):
                    body_parts.append(
                        text
                    )

    # ========================================================
    # FULL BODY
    # ========================================================

    body = "\n".join(
        body_parts
    )

    body = body[:70000]

    # ========================================================
    # IMAGE FALLBACK
    # ========================================================

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

            src = urljoin(
                final_url,
                src
            )

            image_candidates.append(
                src
            )

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
# AI CLIENT
# ============================================================

def get_ai_client():

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY تنظیم نشده است."
        )

    return genai.Client(
        api_key=GEMINI_API_KEY
    )


async def gemini_generate(
    *,
    system_instruction,
    prompt,
    max_output_tokens=1800,
    response_mime_type=None
):
    """
    ارسال درخواست به Gemini به‌صورت asynchronous.
    SDK رسمی google-genai از client.aio استفاده می‌کند.
    """

    client = get_ai_client()

    config_kwargs = {
        "system_instruction": system_instruction,
        "max_output_tokens": max_output_tokens,
        "temperature": 0.2,
    }

    if response_mime_type:
        config_kwargs["response_mime_type"] = response_mime_type

    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                **config_kwargs
            )
        )

        result = (
            getattr(response, "text", None)
            or ""
        ).strip()

        if not result:
            raise RuntimeError(
                "Gemini خروجی خالی تولید کرد."
            )

        return result

    except Exception as error:
        message = str(error)

        if "429" in message or "RESOURCE_EXHAUSTED" in message:
            raise RuntimeError(
                "سهمیه Gemini تمام شده یا محدودیت درخواست فعال شده است."
            ) from error

        if "401" in message or "403" in message or "PERMISSION_DENIED" in message:
            raise RuntimeError(
                "کلید Gemini معتبر نیست یا دسترسی API برای آن فعال نشده است."
            ) from error

        raise RuntimeError(
            f"خطا در ارتباط با Gemini: {message}"
        ) from error


# ============================================================
# FACT EXTRACTION PROMPT
# ============================================================

FACT_PROMPT = r"""
تو یک سیستم استخراج اطلاعات برای تحریریه Gamefa هستی.

وظیفه تو تولید خبر نیست.

وظیفه تو این است که مقاله را کامل بخوانی و فقط واقعیت‌های مهم و مستقیم مربوط به موضوع اصلی مقاله را استخراج کنی.

ممکن است صفحه شامل موارد زیر باشد:

- مطالب مرتبط
- مطالب پیشنهادی
- مقالات دیگر
- تبلیغات
- اطلاعات نویسنده
- زمان انتشار
- باکس‌های سایت
- لینک‌های داخلی
- متن‌های جانبی
- Reviewer
- اطلاعات مربوط به عملکرد AI

هیچ‌کدام از این موارد را به‌عنوان محتوای اصلی خبر در نظر نگیر.

فقط اطلاعاتی را استخراج کن که مستقیماً درباره موضوع اصلی مقاله هستند.

اطلاعات مهمی که باید در صورت وجود استخراج شوند:

- اتفاق اصلی
- نام افراد
- نام بازی
- نام فیلم یا سریال
- نام شرکت‌ها
- سازنده
- ناشر
- پلتفرم‌ها
- تاریخ عرضه
- زمان عرضه
- تاریخ انتشار
- تاریخ دسترسی زودهنگام
- زمان پیش‌دانلود
- حجم دانلود
- قیمت
- نسخه‌ها
- وضعیت پروژه
- بازیگران
- کارگردان
- نویسنده
- فروش
- آمار
- تعداد
- ویژگی‌های مهم
- نقل‌قول مهم
- وضعیت تأیید یا شایعه بودن خبر

اگر تاریخ عرضه در مقاله وجود دارد، حتماً آن را استخراج کن.

اگر عدد یا آمار مهمی در مقاله وجود دارد، آن را حذف نکن.

اگر اطلاعاتی وجود ندارد، آن را اختراع نکن.

مقالات مرتبط و مطالب جانبی را با موضوع اصلی قاطی نکن.

خروجی فقط JSON معتبر باشد.

ساختار:

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

importance باید عددی بین 1 تا 5 باشد.

فقط اطلاعاتی را وارد کن که واقعاً در مقاله وجود دارند.
"""


# ============================================================
# EXTRACT FACTS
# ============================================================

async def extract_facts(source):

    prompt_input = (
        "عنوان مقاله:\n"
        + source.get("title", "")
        + "\n\n"
        "توضیحات:\n"
        + source.get("description", "")
        + "\n\n"
        "متن کامل مقاله:\n"
        + source.get("body", "")
    )

    raw = await gemini_generate(
        system_instruction=FACT_PROMPT,
        prompt=prompt_input,
        max_output_tokens=3000,
        response_mime_type="application/json"
    )

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

        start = raw.find("{")
        end = raw.rfind("}")

        if start == -1 or end == -1:

            raise RuntimeError(
                "AI نتوانست Factهای مقاله را استخراج کند."
            )

        try:

            data = json.loads(
                raw[start:end + 1]
            )

        except Exception as error:

            raise RuntimeError(
                "JSON استخراج Fact نامعتبر است."
            ) from error

    if not isinstance(
        data,
        dict
    ):

        raise RuntimeError(
            "ساختار Fact نامعتبر است."
        )

    return data


# ============================================================
# NEWS GENERATION
# ============================================================

NEWS_PROMPT = r"""
تو سردبیر ارشد اخبار فارسی Gamefa هستی.

از اطلاعات استخراج‌شده از مقاله، یک خبر حرفه‌ای فارسی تولید کن.

قانون بسیار مهم:

خروجی نهایی باید فقط شامل این موارد باشد:

خط اول:
تیتر

خطوط بعدی:
دقیقاً 7 جمله خبری.

اما در خروجی نهایی، 7 جمله خبر باید همگی در یک پاراگراف قرار بگیرند و بین جمله‌ها Enter نزن.

ساختار:

تیتر

جمله اول. جمله دوم. جمله سوم. جمله چهارم. جمله پنجم. جمله ششم. جمله هفتم.

---

مهم‌ترین قانون:

فقط از Factهای استخراج‌شده استفاده کن.

هیچ اطلاعاتی را حدس نزن.

هیچ اطلاعاتی را از خودت اضافه نکن.

---

قانون بسیار مهم درباره شروع متن:

تیتر MUST با یک کلمه یا عبارت فارسی شروع شود.

هیچ تیتر یا جمله‌ای نباید با کلمه انگلیسی شروع شود.

اگر نام انگلیسی در ابتدای جمله لازم است، ابتدا یک عبارت فارسی کوتاه و طبیعی قرار بده.

مثلاً:

درست:
جیکوب الوردی برای پیوستن به فیلم Scapegoat وارد مذاکره شده است

درست:
بازی GTA 6 طبق گزارش جدید...

درست:
فیلم Supergirl با...

غلط:
Jacob Elordi is in talks...

غلط:
GTA 6 will...

غلط:
Supergirl is...

این قانون برای تک‌تک 7 جمله الزامی است، نه فقط تیتر.

---

اطلاعات مهم نباید حذف شوند.

اگر Factهای مقاله شامل یکی از موارد زیر هستند، در صورت مرتبط بودن باید در خبر استفاده شوند:

- تاریخ عرضه
- زمان عرضه
- تاریخ انتشار
- پیش‌دانلود
- حجم بازی
- قیمت
- پلتفرم
- نسخه‌ها
- بازیگران
- کارگردان
- سازنده
- ناشر
- آمار
- اعداد
- وضعیت پروژه

اگر مقاله درباره حجم و زمان عرضه بازی است و تاریخ عرضه در Factها وجود دارد، حذف تاریخ عرضه ممنوع است.

---

درباره منابع:

نگو:
«طبق توضیحات مقاله»

نگو:
«متن کامل صفحه نشان می‌دهد»

نگو:
«Reviewer گفته»

نگو:
«هوش مصنوعی تشخیص داد»

نگو:
«امتیاز دقت»

نگو:
«در این صفحه»

نگو:
«مقاله با تیتر دیگری همراه است»

نگو:
«اطلاعاتی که Reviewer بررسی کرده»

هیچ اشاره‌ای به سیستم AI، Reviewer، Fact، مقاله ورودی یا فرایند تولید نکن.

---

مطالب مرتبط:

اگر در صفحه اطلاعات مربوط به مقاله دیگری وجود دارد، آن را وارد خبر نکن.

مثلاً اگر موضوع اصلی مقاله درباره Jacob Elordi و فیلم Scapegoat است و صفحه در پایین خود مطلبی درباره The Dog Stars دارد، اطلاعات The Dog Stars نباید وارد خبر Scapegoat شود؛ مگر اینکه مستقیماً در متن اصلی خبر درباره موضوع Scapegoat استفاده شده باشد.

---

تیتر:

کوتاه و خبری باشد.

حتماً با فارسی شروع شود.

---

سبک:

فارسی روان و طبیعی.

لحن خبری.

بدون اغراق.

بدون تحلیل شخصی.

بدون نظر شخصی.

بدون ساخت اطلاعات.

نام‌های انگلیسی مهم را حفظ کن، اما هرگز اجازه نده جمله با آن‌ها شروع شود.

---

این موارد ممنوع هستند:

- Markdown
- Bold
- Bullet
- شماره‌گذاری
- Emoji
- لینک
- آیدی کانال
- Reviewer
- AI Score
- Accuracy Score
- توضیح درباره مقاله
- توضیح درباره فرآیند تولید
- توضیح درباره Factها

---

خروجی فقط:

تیتر
یک پاراگراف شامل دقیقاً 7 جمله خبری

هیچ چیز دیگری ننویس.
"""


async def generate_news(
    source,
    facts,
    retry_instruction=""
):

    facts_json = json.dumps(
        facts,
        ensure_ascii=False,
        indent=2
    )

    input_text = (
        "FACTS استخراج‌شده از مقاله:\n\n"
        + facts_json
        + "\n\n"
        "عنوان اصلی مقاله:\n"
        + source.get("title", "")
        + "\n\n"
        "متن اصلی مقاله برای بررسی نهایی:\n"
        + source.get("body", "")
        + "\n\n"
        + retry_instruction
    )

    result = await gemini_generate(
        system_instruction=NEWS_PROMPT,
        prompt=input_text,
        max_output_tokens=1800
    )

    if not result:

        raise RuntimeError(
            "AI خروجی خالی تولید کرد."
        )

    return result


# ============================================================
# SENTENCE SPLITTER
# ============================================================

def split_sentences(text):
    """Extract title + exactly seven sentences as robustly as possible."""
    text = clean_ai_text(text)
    text = text.replace("\r", "\n")

    # Remove accidental labels commonly emitted by models.
    text = re.sub(r"(?im)^\s*(?:تیتر|عنوان)\s*[:：]\s*", "", text)
    text = re.sub(r"(?im)^\s*(?:خبر|متن خبر)\s*[:：]\s*", "", text)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", []

    title = clean_sentence(lines[0])
    body = " ".join(lines[1:])
    body = re.sub(r"\s+", " ", body).strip()

    # Sentence punctuation: Persian/Latin full stops and question/exclamation marks.
    parts = re.split(r"(?<=[.!؟])\s+", body)
    parts = [x.strip() for x in parts if x.strip()]

    # Some models omit punctuation. If there are seven separate non-empty lines,
    # use those lines as sentences.
    if len(parts) < 7 and len(lines) == 8:
        parts = [x.strip() for x in lines[1:] if x.strip()]

    # If the model returns one paragraph without punctuation, split by lines first.
    if len(parts) < 7:
        line_parts = [x.strip() for x in lines[1:] if x.strip()]
        if len(line_parts) == 7:
            parts = line_parts

    return title, parts


# ============================================================
# CLEAN SENTENCE
# ============================================================

def clean_sentence(sentence):

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

    sentence = re.sub(
        r"(?i)\b(?:reviewer|ai score|accuracy score)\b.*$",
        "",
        sentence
    )

    return sentence.strip()


# ============================================================
# INTERNAL OUTPUT VALIDATION
# ============================================================

FORBIDDEN_OUTPUT_TERMS = [
    "reviewer", "ai score", "accuracy score", "امتیاز دقت ai",
    "امتیاز دقت", "اطلاعاتی که reviewer", "هوش مصنوعی بررسی",
    "متن کامل صفحه", "متن کامل مقاله", "در این صفحه",
    "مقاله با تیتر دیگری", "تیتر دیگری", "اطلاعات استخراج شده", "fact"
]


def validate_generated_output(generated):
    title, sentences = split_sentences(generated)
    if not title or len(sentences) != 7:
        return False, title, sentences

    combined = (title + " " + " ".join(sentences)).lower()
    if any(term.lower() in combined for term in FORBIDDEN_OUTPUT_TERMS):
        return False, title, sentences

    if not starts_with_persian(title):
        return False, title, sentences

    for sentence in sentences:
        if not starts_with_persian(sentence):
            return False, title, sentences

    if len(format_post(generated)) > 1024:
        return False, title, sentences

    return True, title, sentences


# ============================================================
# FACT COVERAGE
# ============================================================

def fact_text_list(facts):

    result = []

    for item in facts.get(
        "facts",
        []
    ):

        if not isinstance(
            item,
            dict
        ):
            continue

        fact = str(
            item.get(
                "fact",
                ""
            )
        ).strip()

        importance = item.get(
            "importance",
            0
        )

        try:

            importance = int(
                importance
            )

        except Exception:

            importance = 0

        if fact and importance >= 4:

            result.append(
                fact
            )

    return result


def check_important_fact_coverage(
    generated,
    facts
):

    important_facts = fact_text_list(
        facts
    )

    if not important_facts:
        return True

    generated_norm = norm(
        generated
    )

    generated_words = set(
        generated_norm.split()
    )

    missed = 0

    for fact in important_facts:

        fact_norm = norm(
            fact
        )

        fact_words = set(
            fact_norm.split()
        )

        if not fact_words:
            continue

        overlap = len(
            fact_words
            & generated_words
        ) / len(
            fact_words
        )

        # بررسی اعداد
        numbers = re.findall(
            r"\d+(?:[.,]\d+)?",
            fact
        )

        if numbers:

            if not any(
                number in generated
                for number in numbers
            ):

                missed += 1
                continue

        if overlap < 0.25:

            missed += 1

    return missed <= max(
        1,
        len(important_facts) // 3
    )


# ============================================================
# FORMAT POST (COMPLETED)
# ============================================================

def format_post(generated, facts=None):
    generated = clean_ai_text(generated)
    title, sentences = split_sentences(generated)

    sentences = [clean_sentence(x) for x in sentences if clean_sentence(x)]
    if len(sentences) != 7:
        return ""

    title = ensure_persian_start(clean_sentence(title), is_title=True)
    sentences = [ensure_persian_start(x) for x in sentences]

    category_emoji = detect_category(title + " " + " ".join(sentences))

    # قالب‌بندی پست نهایی تلگرام
    formatted = f"{category_emoji} <b>{escape_html(title)}</b>\n\n"
    formatted += escape_html(" ".join(sentences))
    formatted += f"\n\n🆔 {CHANNEL_ID}"

    return formatted


# ============================================================
# BOT HANDLERS & ROUTING
# ============================================================

router = Router()


@router.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return await message.answer("شما دسترسی به این ربات را ندارید.")

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="راهنما")]],
        resize_keyboard=True
    )

    await message.answer(
        "سلام! ربات تولید خبر گیمفا فعال است.\n"
        "برای شروع کافیست لینک مقاله گیمفا را ارسال کنید.",
        reply_markup=kb
    )


async def process_news(message: Message, url: str):
    user_id = message.from_user.id
    if user_id in processing_users:
        return await message.answer("یک پردازش در حال انجام است. لطفاً شکیبا باشید.")

    processing_users.add(user_id)
    status_msg = await message.answer("در حال دریافت و استخراج اطلاعات...")

    try:
        source = await fetch_gamefa(url)

        if duplicate(source["body"], source["title"]):
            await status_msg.edit_text("این خبر قبلاً ثبت یا ارسال شده است (تکراری).")
            return

        facts = await extract_facts(source)
        await status_msg.edit_text("اطلاعات استخراج شد. در حال بازنویسی خبر...")

        generated = await generate_news(source, facts)
        valid, title, sentences = validate_generated_output(generated)

        if not valid:
            generated = await generate_news(source, facts, retry_instruction="تذکر: حتما 7 جمله دقیق تولید کن و همه با فارسی شروع شوند.")

        final_caption = format_post(generated, facts)

        if not final_caption:
            await status_msg.edit_text("خطا در قالب‌بندی پست نهایی.")
            return

        # ذخیره در حافظه موقت
        prepared[user_id] = {
            "source": source["body"],
            "title": source["title"],
            "hash": text_hash(source["body"]),
            "caption": final_caption,
            "image": source.get("image", "")
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="تایید و ارسال به کانال", callback_data="publish_post")],
            [InlineKeyboardButton(text="لغو", callback_data="cancel_post")]
        ])

        if source.get("image"):
            await message.answer_photo(
                photo=source["image"],
                caption=final_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
        else:
            await message.answer(
                final_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )

        await status_msg.delete()

    except Exception as e:
        log.error("News processing error: %s", e)
        await status_msg.edit_text(f"خطا در پردازش خبر: {e}")
    finally:
        processing_users.discard(user_id)


@router.message(F.text)
async def handle_message(message: Message):
    if not is_admin(message):
        return

    url = extract_url(message.text)
    if url:
        await process_news(message, url)
    else:
        await message.answer("لطفاً یک لینک معتبر از گیمفا ارسال کنید.")


# ============================================================
# MAIN APPLICATION SETUP
# ============================================================

async def main():
    load_memory()

    if not BOT_TOKEN:
        log.error("BOT_TOKEN ست نشده است.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("========================================")
    log.info("Gamefa Bot started successfully.")
    log.info(f"Admin ID: {ADMIN_ID}")
    log.info(f"Channel: {CHANNEL_ID}")
    log.info(f"Model: {MODEL}")
    log.info(f"Memory: {len(memory)} articles")
    log.info("========================================")

    try:
        await dp.start_polling(bot)
    finally:
        save_memory()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
