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

from openai import AsyncOpenAI


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    "@Gamefa_official"
).strip()

MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.4-mini"
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
# GAMEFA FETCH
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
        "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY تنظیم نشده است."
        )

    return AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


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

    client = get_ai_client()

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

    response = await client.responses.create(
        model=MODEL,
        instructions=FACT_PROMPT,
        input=prompt_input,
        max_output_tokens=3000
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

    client = get_ai_client()

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

    response = await client.responses.create(
        model=MODEL,
        instructions=NEWS_PROMPT,
        input=input_text,
        max_output_tokens=1800
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

    title = clean_sentence(lines[0]) if 'clean_sentence' in globals() else lines[0]
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

    # Telegram photo captions have a 1024-character limit. Keep a margin for safety.
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
# FORMAT POST
# ============================================================

def format_post(generated, facts=None):
    generated = clean_ai_text(generated)
    title, sentences = split_sentences(generated)

    sentences = [clean_sentence(x) for x in sentences if clean_sentence(x)]
    if len(sentences) != 7:
        return ""

    title = ensure_persian_start(clean_sentence(title), is_title=True)
    sentences = [ensure_persian_start(x, is_title=False) for x in sentences]

    if not starts_with_persian(title) or any(not starts_with_persian(x) for x in sentences):
        return ""

    category = detect_category(title + " " + " ".join(sentences))
    title = category + " " + title
    body = " ".join(sentences)

    result = (
        "<b>" + escape_html(title) + "</b>\n\n"
        + "🟣 " + escape_html(body) + "\n\n"
        + "<b>🆔 @Gamefa_official</b>"
    )
    return result


# ============================================================
# IMAGE DOWNLOAD
# ============================================================

async def download_image(
    url
):

    if not url:
        return None

    try:

        headers = {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151 Safari/537.36",

            "Accept":
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
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

                content_type = (
                    response.headers.get(
                        "Content-Type",
                        ""
                    ).lower()
                )

                data = await response.read()

        if not data:
            return None

        if len(data) < 1000:
            return None

        if len(data) > 15 * 1024 * 1024:
            return None

        parsed = urlparse(
            url
        )

        extension = Path(
            parsed.path
        ).suffix.lower()

        allowed = [
            ".jpg",
            ".jpeg",
            ".png",
            ".webp"
        ]

        if extension not in allowed:

            if "jpeg" in content_type:
                extension = ".jpg"

            elif "png" in content_type:
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


# ============================================================
# IMAGE SEARCH
# ============================================================

async def find_best_image(
    source
):

    primary = source.get(
        "image",
        ""
    )

    if primary:

        path = await download_image(
            primary
        )

        if path:
            return path

    return None


# ============================================================
# REPLY KEYBOARDS
# ============================================================

def main_reply_keyboard():

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
        resize_keyboard=True,
        is_persistent=True
    )


def news_reply_keyboard():

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
        resize_keyboard=True,
        is_persistent=True
    )


def archive_reply_keyboard():

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
        resize_keyboard=True,
        is_persistent=True
    )


def settings_reply_keyboard():

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
        resize_keyboard=True,
        is_persistent=True
    )


# ============================================================
# INLINE PUBLISH BUTTON
# ============================================================

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
            "⏳ یک خبر در حال پردازش است. لطفاً صبر کن."
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

        # ====================================================
        # SOURCE
        # ====================================================

        if url:

            status = await message.answer(
                "⏳ در حال دریافت کامل مقاله از Gamefa..."
            )

            article = await fetch_gamefa(
                url
            )

            source = article

            if status:

                try:

                    await status.edit_text(
                        "🧠 مقاله دریافت شد.\n"
                        "در حال استخراج واقعیت‌های مهم..."
                    )

                except Exception:
                    pass

        else:

            source = {
                "url": "",
                "title": "",
                "description": "",
                "body": text,
                "image": ""
            }

        # ====================================================
        # DUPLICATE
        # ====================================================

        source_for_duplicate = (
            source.get("title", "")
            + "\n"
            + source.get("body", "")
        )

        if duplicate(
            source_for_duplicate,
            source.get(
                "title",
                ""
            )
        ):

            await message.answer(
                "⚠️ این خبر یا یک خبر بسیار مشابه قبلاً در آرشیو وجود دارد.",
                reply_markup=main_reply_keyboard()
            )

            return

        # ====================================================
        # FACT EXTRACTION
        # ====================================================

        facts = await extract_facts(
            source
        )

        if status:

            try:

                await status.edit_text(
                    "🧠 اطلاعات اصلی مقاله استخراج شد.\n"
                    "در حال ساخت خبر..."
                )

            except Exception:
                pass

        # ====================================================
        # AI GENERATION
        # ====================================================

        generated = await generate_news(
            source,
            facts
        )

        valid, title, sentences = (
            validate_generated_output(
                generated
            )
        )

        # ====================================================
        # RETRY 1
        # ====================================================

        if not valid:

            log.warning(
                "AI output failed validation. Regenerating..."
            )

            generated = await generate_news(
                source,
                facts,
                retry_instruction=(
                    "\n\n"
                    "خروجی قبلی رد شده است.\n"
                    "این بار دقیقاً این ساختار را رعایت کن:\n\n"
                    "خط اول = یک تیتر فارسی\n"
                    "خط دوم = جمله اول\n"
                    "خط سوم = جمله دوم\n"
                    "خط چهارم = جمله سوم\n"
                    "خط پنجم = جمله چهارم\n"
                    "خط ششم = جمله پنجم\n"
                    "خط هفتم = جمله ششم\n"
                    "خط هشتم = جمله هفتم\n\n"
                    "هیچ جمله‌ای نباید با کلمه انگلیسی شروع شود.\n"
                    "اگر نام انگلیسی ابتدای جمله است، "
                    "ابتدا یک عبارت فارسی قرار بده.\n"
                    "هیچ Reviewer، AI، Fact یا توضیحی درباره فرآیند ننویس."
                )
            )

        # ====================================================
        # FACT COVERAGE
        # ====================================================

        if not check_important_fact_coverage(
            generated,
            facts
        ):

            log.warning(
                "Important facts may be missing. Regenerating..."
            )

            generated = await generate_news(
                source,
                facts,
                retry_instruction=(
                    "\n\n"
                    "نسخه قبلی بعضی اطلاعات مهم را از دست داده است.\n"
                    "تمام Factهای مهم استخراج‌شده را دوباره بررسی کن.\n"
                    "به‌خصوص تاریخ‌ها، اعداد، پلتفرم‌ها، حجم، قیمت، "
                    "بازیگران و وضعیت عرضه را در صورت وجود وارد کن.\n"
                    "خروجی فقط تیتر + 7 جمله باشد.\n"
                    "تیتر و هر 7 جمله حتماً با فارسی شروع شوند."
                )
            )

        # ====================================================
        # FINAL VALIDATION
        # ====================================================

        valid, title, sentences = (
            validate_generated_output(
                generated
            )
        )

        if not valid:

            # یک بار آخر تلاش برای اصلاح ساختار
            log.warning(
                "Final validation failed. Running final repair..."
            )

            generated = await generate_news(
                source,
                facts,
                retry_instruction=(
                    "\n\n"
                    "این آخرین تلاش برای اصلاح خروجی است.\n"
                    "خروجی باید دقیقاً شامل یک تیتر و 7 جمله باشد.\n"
                    "تیتر با فارسی شروع شود.\n"
                    "هر 7 جمله نیز با فارسی شروع شوند.\n"
                    "هیچ خط اضافه‌ای ننویس.\n"
                    "هیچ Markdown، Emoji، لینک، Reviewer یا AI Score ننویس.\n"
                    "نام‌های انگلیسی را فقط بعد از شروع فارسی استفاده کن."
                )
            )

            valid, title, sentences = (
                validate_generated_output(
                    generated
                )
            )

        if not valid:

            title, sentences = split_sentences(generated)

            if len(sentences) >= 5:

                sentences = sentences[:7]

                while len(sentences) < 7:
                    sentences.append(
                        "جزئیات بیشتری درباره این خبر منتشر نشده است."
                    )

                generated = (
                    title
                    + "\n"
                    + "\n".join(sentences)
                )

            else:

                raise RuntimeError(
                    "خروجی AI قابل اصلاح نیست."
                )

        # ====================================================
        # FORMAT
        # ====================================================

        post = format_post(
            generated,
            facts
        )

        if not post:

            raise RuntimeError(
                "متن نهایی قابل تولید نیست."
            )

        # ====================================================
        # IMAGE
        # ====================================================

        image_path = await find_best_image(
            source
        )

        # ====================================================
        # MEMORY
        # ====================================================

        memory.append(
            {
                "hash": text_hash(
                    source_for_duplicate
                ),
                "title": source.get(
                    "title",
                    ""
                ),
                "source": source_for_duplicate[:25000],
                "post": post,
                "url": url or ""
            }
        )

        memory[:] = memory[
            -MAX_MEMORY:
        ]

        save_memory()

        # ====================================================
        # PREPARE
        # ====================================================

        prepared[user_id] = {
            "text": post,
            "image": (
                str(image_path)
                if image_path
                else ""
            )
        }

        # ====================================================
        # REMOVE STATUS
        # ====================================================

        if status:

            try:
                await status.delete()

            except Exception:
                pass

        # ====================================================
        # PREVIEW
        # ====================================================

        if image_path:
            # متن و تصویر عمداً در یک پیام ارسال می‌شوند.
            # post قبل از این مرحله برای محدودیت 1024 کاراکتر کپشن اعتبارسنجی شده است.
            try:
                await message.answer_photo(
                    FSInputFile(image_path),
                    caption=post,
                    parse_mode=ParseMode.HTML,
                    reply_markup=publish_keyboard()
                )
            except Exception as error:
                log.warning("Photo preview failed: %s", error)
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
            "✅ خبر آماده انتشار است.\n"
            "اگر متن و تصویر مناسب هستند، روی «📢 انتشار در کانال» بزن.",
            reply_markup=main_reply_keyboard()
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
            reply_markup=main_reply_keyboard()
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
            "❌ هنوز خبری برای انتشار آماده نیست.",
            reply_markup=main_reply_keyboard()
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

        # ====================================================
        # WITH IMAGE
        # ====================================================

        if (
            image
            and Path(image).exists()
        ):

            try:
                await message.bot.send_photo(
                    CHANNEL_ID,
                    FSInputFile(image),
                    caption=text,
                    parse_mode=ParseMode.HTML
                )
            except Exception as error:
                log.warning("Photo publish failed: %s", error)
                await message.bot.send_message(
                    CHANNEL_ID,
                    text,
                    parse_mode=ParseMode.HTML
                )

        # ====================================================
        # WITHOUT IMAGE
        # ====================================================

        else:

            await message.bot.send_message(
                CHANNEL_ID,
                text,
                parse_mode=ParseMode.HTML
            )

        await message.answer(
            "✅ خبر با موفقیت در کانال منتشر شد.",
            reply_markup=main_reply_keyboard()
        )

        prepared.pop(
            user_id,
            None
        )

    except Exception as error:

        log.exception(
            "Publish error"
        )

        await message.answer(
            "❌ خطا هنگام انتشار:\n\n"
            + str(error)[:1500],
            reply_markup=main_reply_keyboard()
        )


# ============================================================
# ROUTER
# ============================================================

router = Router()


# ============================================================
# START
# ============================================================

@router.message(Command("start"))
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
        "به پنل مدیریت اخبار خوش آمدید.\n"
        "از منوی زیر عملیات موردنظر را انتخاب کن.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard()
    )


# ============================================================
# PUBLISH BUTTON
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


# ============================================================
# HOME CALLBACK
# ============================================================

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
        reply_markup=main_reply_keyboard()
    )


# ============================================================
# TEXT MENU HANDLER
# ============================================================

@router.message(
    F.text == "🔎 بررسی خبر جدید"
)
async def news_menu_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🔎 <b>بررسی خبر جدید</b>\n\n"
        "یکی از گزینه‌های زیر را انتخاب کن.",
        parse_mode=ParseMode.HTML,
        reply_markup=news_reply_keyboard()
    )


@router.message(
    F.text == "📝 ارسال خبر"
)
async def news_text_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📝 متن خبر را ارسال کن.\n\n"
        "AI کل متن را تحلیل می‌کند و یک خبر ۷ جمله‌ای می‌سازد."
    )


@router.message(
    F.text == "🔗 ارسال لینک Gamefa"
)
async def news_link_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🔗 لینک مقاله Gamefa را ارسال کن.\n\n"
        "ربات کل مقاله را دریافت می‌کند، "
        "اطلاعات اصلی را استخراج می‌کند و خبر را تولید می‌کند."
    )


# ============================================================
# ARCHIVE
# ============================================================

@router.message(
    F.text == "📁 آرشیو"
)
async def archive_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📁 <b>آرشیو اخبار</b>\n\n"
        "یک گزینه را انتخاب کن.",
        parse_mode=ParseMode.HTML,
        reply_markup=archive_reply_keyboard()
    )


@router.message(
    F.text == "📚 آخرین اخبار"
)
async def archive_latest_handler(
    message: Message
):

    if not is_admin(message):
        return

    if not memory:

        await message.answer(
            "📚 آرشیو خالی است.",
            reply_markup=archive_reply_keyboard()
        )

        return

    latest = memory[-10:]

    lines = [
        "📚 <b>آخرین اخبار</b>",
        ""
    ]

    for index, item in enumerate(
        reversed(latest),
        1
    ):

        post = item.get(
            "post",
            ""
        )

        clean = re.sub(
            r"<[^>]+>",
            "",
            post
        )

        first_line = (
            clean.splitlines()[0]
            if clean
            else "خبر بدون عنوان"
        )

        lines.append(
            f"{index}. {first_line[:100]}"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=archive_reply_keyboard()
    )


@router.message(
    F.text == "🗑 پاکسازی آرشیو"
)
async def clear_archive_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "⚠️ برای پاک کردن آرشیو دستور /clear را ارسال کن."
    )


# ============================================================
# STATS
# ============================================================

@router.message(
    F.text == "📊 آمار"
)
async def stats_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📊 <b>آمار ربات</b>\n\n"
        f"📰 اخبار آرشیو: <b>{len(memory)}</b>\n"
        f"💾 ظرفیت حافظه: <b>{MAX_MEMORY}</b>\n"
        f"👤 مدیر: <code>{ADMIN_ID}</code>\n"
        f"🧠 مدل: <code>{escape_html(MODEL)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard()
    )


# ============================================================
# SETTINGS
# ============================================================

@router.message(
    F.text == "⚙️ تنظیمات"
)
async def settings_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "⚙️ <b>تنظیمات</b>\n\n"
        "یک بخش را انتخاب کن.",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_reply_keyboard()
    )


@router.message(
    F.text == "📢 کانال انتشار"
)
async def channel_setting_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📢 کانال انتشار:\n\n"
        f"<code>{escape_html(CHANNEL_ID)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_reply_keyboard()
    )


@router.message(
    F.text == "🧠 مدل AI"
)
async def model_setting_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🧠 مدل AI:\n\n"
        f"<code>{escape_html(MODEL)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_reply_keyboard()
    )


@router.message(
    F.text == "🖼 سیستم تصویر"
)
async def image_setting_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "🖼 سیستم تصویر\n\n"
        "ربات ابتدا تصویر اصلی og:image مقاله را پیدا می‌کند.\n\n"
        "اگر تصویر مناسب پیدا نشود، خبر بدون تصویر منتشر می‌شود.",
        reply_markup=settings_reply_keyboard()
    )


@router.message(
    F.text == "✍️ قالب خبر"
)
async def format_setting_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "✍️ <b>قالب خبر</b>\n\n"
        "• تیتر فارسی\n"
        "• دقیقاً ۷ جمله\n"
        "• یک پاراگراف واحد\n"
        "• اطلاعات مهم مقاله\n"
        "• تاریخ و اعداد در صورت وجود\n"
        "• حذف اطلاعات Reviewer\n"
        "• حذف اطلاعات AI\n"
        "• امضای Gamefa",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_reply_keyboard()
    )


# ============================================================
# BACK
# ============================================================

@router.message(
    F.text == "🔙 بازگشت"
)
async def back_handler(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "✨ <b>پنل مدیریت Gamefa</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard()
    )


# ============================================================
# COMMAND: PUBLISH
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


# ============================================================
# COMMAND: STATS
# ============================================================

@router.message(
    Command("stats")
)
async def stats_command(
    message: Message
):

    if not is_admin(message):
        return

    await message.answer(
        "📊 تعداد اخبار آرشیو: "
        + str(len(memory)),
        reply_markup=main_reply_keyboard()
    )


# ============================================================
# COMMAND: CLEAR
# ============================================================

@router.message(
    Command("clear")
)
async def clear_command(
    message: Message
):

    if not is_admin(message):
        return

    memory.clear()

    save_memory()

    prepared.clear()

    await message.answer(
        "✅ آرشیو با موفقیت پاک شد.",
        reply_markup=main_reply_keyboard()
    )


# ============================================================
# TEXT MESSAGE
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
        message.text or ""
    ).strip()

    if not text:
        return

    if text.startswith("/"):
        return

    # ========================================================
    # MENU WORDS
    # ========================================================

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

    if not OPENAI_API_KEY:

        raise RuntimeError(
            "OPENAI_API_KEY تنظیم نشده است."
        )

    if not ADMIN_ID:

        raise RuntimeError(
            "ADMIN_ID تنظیم نشده است."
        )

    load_memory()

    bot = Bot(
        token=BOT_TOKEN
    )

    dispatcher = Dispatcher()

    dispatcher.include_router(
        router
    )

    log.info(
        "========================================"
    )

    log.info(
        "Gamefa Bot started successfully."
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
        "Memory: %s articles",
        len(memory)
    )

    log.info(
        "========================================"
    )

    await dispatcher.start_polling(
        bot,
        allowed_updates=dispatcher.resolve_used_update_types()
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())