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

# دریافت API Key و Base URL مربوط به DeepSeek
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    "@Gamefa_official"
).strip()

MODEL = os.getenv(
    "DEEPSEEK_MODEL",
    "deepseek-chat"
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


def ensure_persian_start(
    text,
    is_title=False
):

    if not text:
        return text

    text = text.strip()

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
        "بازی", "گیم", "game", "gaming", "playstation", "xbox",
        "nintendo", "steam", "doom", "gta", "resident evil", "halo",
        "final fantasy", "devil may cry", "assassin", "elden ring",
        "sony", "microsoft", "ps5", "ps4", "xbox series", "switch"
    ]

    movie_words = [
        "فیلم", "سریال", "بازیگر", "movie", "film", "series",
        "season", "actor", "actress", "netflix", "hbo", "disney",
        "marvel", "dc", "cinema"
    ]

    if any(word in text_lower for word in game_words):
        return "🎮"

    if any(word in text_lower for word in movie_words):
        return "🎬"

    return "📢"


# ============================================================
# AI CLEANER
# ============================================================

def clean_ai_text(text):

    text = text or ""

    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"\*(.*?)\*", r"\1", text, flags=re.S)
    text = re.sub(r"`(.*?)`", r"\1", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

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
        text = re.sub(pattern, "", text)

    text = re.sub(r"(?im)^\s*(?:🆔\s*)?@Gamefa_official\s*$", "", text)
    text = re.sub(r"(?m)^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]\s*", "", text)

    return text.strip()


# ============================================================
# ARTICLE CLEANING
# ============================================================

REMOVE_SELECTORS = [
    "script", "style", "noscript", "svg", "nav", "footer", "form",
    "aside", "header", "iframe", "video", "audio", "canvas",
    ".related-posts", ".related-post", ".related", ".recommended",
    ".recommendations", ".recommended-posts", ".more-posts",
    ".latest-posts", ".popular-posts", ".author-box", ".author-info",
    ".author-card", ".comments", ".comment", ".comment-list",
    ".advertisement", ".ads", ".ad", ".banner", ".newsletter",
    ".social-share", ".share-buttons", ".breadcrumb", ".breadcrumbs",
    ".sidebar", ".widget", ".wp-block-latest-posts", ".read-more",
    ".post-navigation", ".navigation"
]


def remove_unwanted_elements(soup):
    for selector in REMOVE_SELECTORS:
        try:
            for element in soup.select(selector):
                element.decompose()
        except Exception:
            pass


def is_probably_noise(text):
    if not text:
        return True

    low = text.lower()
    noise_words = [
        "مطالب مرتبط", "مطالب پیشنهادی", "اخبار مرتبط", "بیشتر بخوانید",
        "related posts", "related articles", "recommended", "subscribe",
        "newsletter", "تبلیغات", "advertisement", "نویسنده", "author",
        "دیدگاه", "comments", "comment", "share"
    ]

    return any(word in low for word in noise_words)


# ============================================================
# GAMEFA FETCH
# ============================================================

async def fetch_gamefa(url):
    parsed = urlparse(url)

    if "gamefa.com" not in (parsed.netloc.lower()):
        raise ValueError("فقط لینک Gamefa پشتیبانی می‌شود.")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36",
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"
    }

    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            final_url = str(response.url)
            raw = await response.text(errors="ignore")

    soup = BeautifulSoup(raw, "html.parser")
    remove_unwanted_elements(soup)

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
    elif soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    description = ""
    meta_options = [
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"}
    ]

    for attrs in meta_options:
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            description = clean_text(meta["content"])
            break

    image_candidates = []
    for attrs in [
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"}
    ]:
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            image_candidates.append(urljoin(final_url, meta["content"].strip()))

    article = None
    article_selectors = [
        "article", "[itemprop='articleBody']", ".entry-content",
        ".post-content", ".article-content", ".single-post-content",
        ".td-post-content", ".post-body", ".content-area"
    ]

    for selector in article_selectors:
        candidate = soup.select_one(selector)
        if candidate:
            article = candidate
            break

    if article is None:
        article = soup

    paragraphs = article.find_all(["p", "h2", "h3", "h4"])
    body_parts = []
    seen_paragraphs = set()

    for paragraph in paragraphs:
        text = clean_text(paragraph.get_text(" ", strip=True))
        if len(text) < 35 or is_probably_noise(text):
            continue

        paragraph_key = norm(text)
        if paragraph_key in seen_paragraphs:
            continue

        seen_paragraphs.add(paragraph_key)
        body_parts.append(text)

    if len(body_parts) < 3:
        body_parts = []
        for paragraph in soup.find_all("p"):
            text = clean_text(paragraph.get_text(" ", strip=True))
            if len(text) >= 35 and not is_probably_noise(text):
                body_parts.append(text)

    body = "\n".join(body_parts)[:70000]

    if not image_candidates:
        for img in article.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                image_candidates.append(urljoin(final_url, src))

    image = image_candidates[0] if image_candidates else ""

    return {
        "url": final_url,
        "title": title,
        "description": description,
        "body": body,
        "image": image
    }


# ============================================================
# AI CLIENT (DEEPSEEK SUPPORT)
# ============================================================

def get_ai_client():

    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY تنظیم نشده است.")

    # استفاده از SDK استاندارد OpenAI با آدرس Base URL مربوط به DeepSeek
    return AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )


# ============================================================
# PROMPTS
# ============================================================

FACT_PROMPT = r"""
تو یک سیستم استخراج اطلاعات برای تحریریه Gamefa هستی.
وظیفه تو تولید خبر نیست.
وظیفه تو این است که مقاله را کامل بخوانی و فقط واقعیت‌های مهم و مستقیم مربوط به موضوع اصلی مقاله را استخراج کنی.

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
"""

NEWS_PROMPT = r"""
تو سردبیر ارشد اخبار فارسی Gamefa هستی.
از اطلاعات استخراج‌شده از مقاله، یک خبر حرفه‌ای فارسی تولید کن.

خروجی نهایی باید فقط شامل این موارد باشد:
خط اول: تیتر
خطوط بعدی: دقیقاً 7 جمله خبری در قالب یک پاراگراف واحد.

خروجی فقط:
تیتر
یک پاراگراف شامل دقیقاً 7 جمله خبری
"""


# ============================================================
# EXTRACT FACTS
# ============================================================

async def extract_facts(source):

    client = get_ai_client()

    prompt_input = (
        "عنوان مقاله:\n" + source.get("title", "") + "\n\n"
        "توضیحات:\n" + source.get("description", "") + "\n\n"
        "متن کامل مقاله:\n" + source.get("body", "")
    )

    # اصلاح درخواست به ChatCompletion استاندارد
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": FACT_PROMPT},
            {"role": "user", "content": prompt_input}
        ],
        response_format={"type": "json_object"},
        max_tokens=3000
    )

    raw = (response.choices[0].message.content or "").strip()

    try:
        data = json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            raise RuntimeError("AI نتوانست Factهای مقاله را استخراج کند.")
        try:
            data = json.loads(raw[start:end + 1])
        except Exception as error:
            raise RuntimeError("JSON استخراج Fact نامعتبر است.") from error

    return data


# ============================================================
# NEWS GENERATION
# ============================================================

async def generate_news(source, facts, retry_instruction=""):

    client = get_ai_client()

    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)

    input_text = (
        "FACTS استخراج‌شده از مقاله:\n\n" + facts_json + "\n\n"
        "عنوان اصلی مقاله:\n" + source.get("title", "") + "\n\n"
        "متن اصلی مقاله برای بررسی نهایی:\n" + source.get("body", "") + "\n\n"
        + retry_instruction
    )

    # اصلاح درخواست به ChatCompletion استاندارد
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": NEWS_PROMPT},
            {"role": "user", "content": input_text}
        ],
        max_tokens=1800
    )

    result = (response.choices[0].message.content or "").strip()

    if not result:
        raise RuntimeError("AI خروجی خالی تولید کرد.")

    return result


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def split_sentences(text):
    text = clean_ai_text(text)
    text = text.replace("\r", "\n")

    text = re.sub(r"(?im)^\s*(?:تیتر|عنوان)\s*[:：]\s*", "", text)
    text = re.sub(r"(?im)^\s*(?:خبر|متن خبر)\s*[:：]\s*", "", text)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", []

    title = clean_sentence(lines[0])
    body = " ".join(lines[1:])
    body = re.sub(r"\s+", " ", body).strip()

    parts = re.split(r"(?<=[.!؟])\s+", body)
    parts = [x.strip() for x in parts if x.strip()]

    if len(parts) < 7 and len(lines) == 8:
        parts = [x.strip() for x in lines[1:] if x.strip()]

    if len(parts) < 7:
        line_parts = [x.strip() for x in lines[1:] if x.strip()]
        if len(line_parts) == 7:
            parts = line_parts

    return title, parts


def clean_sentence(sentence):
    sentence = sentence.strip()
    sentence = re.sub(r"^[•\-–—\d.)]+\s*", "", sentence)
    sentence = re.sub(r"^\s*[🎮🎬📱📢🟣📰🔵🟢🟡🟠⚪⚫]+\s*", "", sentence)
    sentence = re.sub(r"(?i)\b(?:reviewer|ai score|accuracy score)\b.*$", "", sentence)
    return sentence.strip()


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


def fact_text_list(facts):
    result = []
    for item in facts.get("facts", []):
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact", "")).strip()
        try:
            importance = int(item.get("importance", 0))
        except Exception:
            importance = 0

        if fact and importance >= 4:
            result.append(fact)
    return result


def check_important_fact_coverage(generated, facts):
    important_facts = fact_text_list(facts)
    if not important_facts:
        return True

    generated_norm = norm(generated)
    generated_words = set(generated_norm.split())

    missed = 0
    for fact in important_facts:
        fact_norm = norm(fact)
        fact_words = set(fact_norm.split())
        if not fact_words:
            continue

        overlap = len(fact_words & generated_words) / len(fact_words)
        numbers = re.findall(r"\d+(?:[.,]\d+)?", fact)

        if numbers and not any(number in generated for number in numbers):
            missed += 1
            continue

        if overlap < 0.25:
            missed += 1

    return missed <= max(1, len(important_facts) // 3)


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


async def download_image(url):
    if not url:
        return None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        }
        timeout = aiohttp.ClientTimeout(total=35)

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return None
                content_type = response.headers.get("Content-Type", "").lower()
                data = await response.read()

        if not data or len(data) < 1000 or len(data) > 15 * 1024 * 1024:
            return None

        parsed = urlparse(url)
        extension = Path(parsed.path).suffix.lower()
        if extension not in [".jpg", ".jpeg", ".png", ".webp"]:
            if "png" in content_type:
                extension = ".png"
            elif "webp" in content_type:
                extension = ".webp"
            else:
                extension = ".jpg"

        filename = "gamefa_" + hashlib.md5(url.encode("utf-8")).hexdigest() + extension
        path = IMAGE_DIR / filename
        path.write_bytes(data)
        return path

    except Exception as error:
        log.warning("Image download error: %s", error)
        return None


async def find_best_image(source):
    primary = source.get("image", "")
    if primary:
        path = await download_image(primary)
        if path:
            return path
    return None


# ============================================================
# KEYBOARDS & PROCESS
# ============================================================

def main_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 بررسی خبر جدید"), KeyboardButton(text="📁 آرشیو")],
            [KeyboardButton(text="📊 آمار"), KeyboardButton(text="⚙️ تنظیمات")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def news_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 ارسال خبر"), KeyboardButton(text="🔗 ارسال لینک Gamefa")],
            [KeyboardButton(text="🔙 بازگشت")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def archive_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 آخرین اخبار"), KeyboardButton(text="🗑 پاکسازی آرشیو")],
            [KeyboardButton(text="🔙 بازگشت")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def settings_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 کانال انتشار"), KeyboardButton(text="🧠 مدل AI")],
            [KeyboardButton(text="🖼 سیستم تصویر"), KeyboardButton(text="✍️ قالب خبر")],
            [KeyboardButton(text="🔙 بازگشت")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def publish_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 انتشار در کانال", callback_data="publish_current")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]
        ]
    )


async def process_news(message, text):
    user_id = message.from_user.id
    if user_id in processing_users:
        await message.answer("⏳ یک خبر در حال پردازش است. لطفاً صبر کن.")
        return

    processing_users.add(user_id)
    status = None

    try:
        url = extract_url(text)
        if url:
            status = await message.answer("⏳ در حال دریافت کامل مقاله از Gamefa...")
            source = await fetch_gamefa(url)
            if status:
                try:
                    await status.edit_text("🧠 مقاله دریافت شد.\nدر حال استخراج واقعیت‌های مهم...")
                except Exception:
                    pass
        else:
            source = {"url": "", "title": "", "description": "", "body": text, "image": ""}

        source_for_duplicate = source.get("title", "") + "\n" + source.get("body", "")
        if duplicate(source_for_duplicate, source.get("title", "")):
            await message.answer("⚠️ این خبر یا یک خبر بسیار مشابه قبلاً در آرشیو وجود دارد.", reply_markup=main_reply_keyboard())
            return

        facts = await extract_facts(source)

        if status:
            try:
                await status.edit_text("🧠 اطلاعات اصلی مقاله استخراج شد.\nدر حال ساخت خبر...")
            except Exception:
                pass

        generated = await generate_news(source, facts)
        valid, title, sentences = validate_generated_output(generated)

        if not valid:
            log.warning("AI output failed validation. Regenerating...")
            generated = await generate_news(
                source, facts,
                retry_instruction="\n\nخروجی قبلی رد شده است. لطفاً تیتر و ۷ جمله کاملاً فارسی تولید کن."
            )

        if not check_important_fact_coverage(generated, facts):
            log.warning("Important facts missing. Regenerating...")
            generated = await generate_news(
                source, facts,
                retry_instruction="\n\nاطلاعات مهم مانند تاریخ‌ها یا اعداد حذف شده‌اند. دوباره بررسی کن."
            )

        valid, title, sentences = validate_generated_output(generated)
        if not valid:
            title, sentences = split_sentences(generated)
            if len(sentences) >= 5:
                sentences = sentences[:7]
                while len(sentences) < 7:
                    sentences.append("جزئیات بیشتری درباره این خبر منتشر نشده است.")
                generated = title + "\n" + "\n".join(sentences)
            else:
                raise RuntimeError("خروجی AI قابل اصلاح نیست.")

        post = format_post(generated, facts)
        if not post:
            raise RuntimeError("متن نهایی قابل تولید نیست.")

        image_path = await find_best_image(source)

        memory.append({
            "hash": text_hash(source_for_duplicate),
            "title": source.get("title", ""),
            "source": source_for_duplicate[:25000],
            "post": post,
            "url": url or ""
        })
        memory[:] = memory[-MAX_MEMORY:]
        save_memory()

        prepared[user_id] = {
            "text": post,
            "image": str(image_path) if image_path else ""
        }

        if status:
            try:
                await status.delete()
            except Exception:
                pass

        if image_path:
            try:
                await message.answer_photo(
                    FSInputFile(image_path), caption=post,
                    parse_mode=ParseMode.HTML, reply_markup=publish_keyboard()
                )
            except Exception as error:
                log.warning("Photo preview failed: %s", error)
                await message.answer(post, parse_mode=ParseMode.HTML, reply_markup=publish_keyboard())
        else:
            await message.answer(post, parse_mode=ParseMode.HTML, reply_markup=publish_keyboard())

        await message.answer("✅ خبر آماده انتشار است.", reply_markup=main_reply_keyboard())

    except Exception as error:
        log.exception("News processing error")
        if status:
            try:
                await status.delete()
            except Exception:
                pass
        await message.answer(f"❌ خطا هنگام پردازش خبر:\n\n{str(error)[:1500]}", reply_markup=main_reply_keyboard())

    finally:
        processing_users.discard(user_id)


async def publish_news(message, user_id):
    item = prepared.get(user_id)
    if not item:
        await message.answer("❌ هنوز خبری برای انتشار آماده نیست.", reply_markup=main_reply_keyboard())
        return

    text = item.get("text", "")
    image = item.get("image", "")

    try:
        if image and Path(image).exists():
            try:
                await message.bot.send_photo(CHANNEL_ID, FSInputFile(image), caption=text, parse_mode=ParseMode.HTML)
            except Exception as error:
                log.warning("Photo publish failed: %s", error)
                await message.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        else:
            await message.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)

        await message.answer("✅ خبر با موفقیت در کانال منتشر شد.", reply_markup=main_reply_keyboard())
        prepared.pop(user_id, None)

    except Exception as error:
        log.exception("Publish error")
        await message.answer(f"❌ خطا هنگام انتشار:\n\n{str(error)[:1500]}", reply_markup=main_reply_keyboard())


# ============================================================
# ROUTER & HANDLERS
# ============================================================

router = Router()

@router.message(Command("start"))
async def start_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ این ربات خصوصی است.")
        return
    await message.answer("✨ <b>پنل مدیریت Gamefa</b>", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@router.callback_query(F.data == "publish_current")
async def publish_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("در حال انتشار...")
    await publish_news(callback.message, callback.from_user.id)


@router.callback_query(F.data == "home")
async def home_callback(callback):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("✨ <b>پنل مدیریت Gamefa</b>", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@router.message(F.text == "🔎 بررسی خبر جدید")
async def news_menu_handler(message: Message):
    if is_admin(message):
        await message.answer("🔎 <b>بررسی خبر جدید</b>", parse_mode=ParseMode.HTML, reply_markup=news_reply_keyboard())


@router.message(F.text == "📝 ارسال خبر")
async def news_text_handler(message: Message):
    if is_admin(message):
        await message.answer("📝 متن خبر را ارسال کن.")


@router.message(F.text == "🔗 ارسال لینک Gamefa")
async def news_link_handler(message: Message):
    if is_admin(message):
        await message.answer("🔗 لینک مقاله Gamefa را ارسال کن.")


@router.message(F.text == "📁 آرشیو")
async def archive_handler(message: Message):
    if is_admin(message):
        await message.answer("📁 <b>آرشیو اخبار</b>", parse_mode=ParseMode.HTML, reply_markup=archive_reply_keyboard())


@router.message(F.text == "📚 آخرین اخبار")
async def archive_latest_handler(message: Message):
    if not is_admin(message):
        return
    if not memory:
        await message.answer("📚 آرشیو خالی است.", reply_markup=archive_reply_keyboard())
        return

    latest = memory[-10:]
    lines = ["📚 <b>آخرین اخبار</b>\n"]
    for index, item in enumerate(reversed(latest), 1):
        clean = re.sub(r"<[^>]+>", "", item.get("post", ""))
        first_line = clean.splitlines()[0] if clean else "خبر بدون عنوان"
        lines.append(f"{index}. {first_line[:100]}")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=archive_reply_keyboard())


@router.message(F.text == "🗑 پاکسازی آرشیو")
async def clear_archive_handler(message: Message):
    if is_admin(message):
        await message.answer("⚠️ برای پاک کردن آرشیو دستور /clear را ارسال کن.")


@router.message(F.text == "📊 آمار")
async def stats_handler(message: Message):
    if is_admin(message):
        await message.answer(
            f"📊 <b>آمار ربات</b>\n\n📰 اخبار آرشیو: <b>{len(memory)}</b>\n💾 ظرفیت: <b>{MAX_MEMORY}</b>\n👤 مدیر: <code>{ADMIN_ID}</code>\n🧠 مدل: <code>{escape_html(MODEL)}</code>",
            parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard()
        )


@router.message(F.text == "⚙️ تنظیمات")
async def settings_handler(message: Message):
    if is_admin(message):
        await message.answer("⚙️ <b>تنظیمات</b>", parse_mode=ParseMode.HTML, reply_markup=settings_reply_keyboard())


@router.message(F.text == "📢 کانال انتشار")
async def channel_setting_handler(message: Message):
    if is_admin(message):
        await message.answer(f"📢 کانال انتشار:\n\n<code>{escape_html(CHANNEL_ID)}</code>", parse_mode=ParseMode.HTML, reply_markup=settings_reply_keyboard())


@router.message(F.text == "🧠 مدل AI")
async def model_setting_handler(message: Message):
    if is_admin(message):
        await message.answer(f"🧠 مدل AI:\n\n<code>{escape_html(MODEL)}</code>", parse_mode=ParseMode.HTML, reply_markup=settings_reply_keyboard())


@router.message(F.text == "🖼 سیستم تصویر")
async def image_setting_handler(message: Message):
    if is_admin(message):
        await message.answer("🖼 سیستم تصویر فعال است.", reply_markup=settings_reply_keyboard())


@router.message(F.text == "✍️ قالب خبر")
async def format_setting_handler(message: Message):
    if is_admin(message):
        await message.answer("✍️ <b>قالب خبر ۷ جمله‌ای</b>", parse_mode=ParseMode.HTML, reply_markup=settings_reply_keyboard())


@router.message(F.text == "🔙 بازگشت")
async def back_handler(message: Message):
    if is_admin(message):
        await message.answer("✨ <b>پنل مدیریت Gamefa</b>", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@router.message(Command("publish"))
async def publish_command(message: Message):
    if is_admin(message):
        await publish_news(message, message.from_user.id)


@router.message(Command("stats"))
async def stats_command(message: Message):
    if is_admin(message):
        await message.answer(f"📊 تعداد اخبار آرشیو: {len(memory)}", reply_markup=main_reply_keyboard())


@router.message(Command("clear"))
async def clear_command(message: Message):
    if is_admin(message):
        memory.clear()
        save_memory()
        prepared.clear()
        await message.answer("✅ آرشیو با موفقیت پاک شد.", reply_markup=main_reply_keyboard())


@router.message(F.text)
async def text_handler(message: Message):
    if not is_admin(message):
        return

    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    menu_words = {
        "🔎 بررسی خبر جدید", "📁 آرشیو", "📊 آمار", "⚙️ تنظیمات",
        "📝 ارسال خبر", "🔗 ارسال لینک Gamefa", "📚 آخرین اخبار",
        "🗑 پاکسازی آرشیو", "📢 کانال انتشار", "🧠 مدل AI",
        "🖼 سیستم تصویر", "✍️ قالب خبر", "🔙 بازگشت"
    }

    if text in menu_words:
        return

    await process_news(message, text)


# ============================================================
# MAIN
# ============================================================

async def main():

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY تنظیم نشده است.")

    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID تنظیم نشده است.")

    load_memory()

    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    log.info("========================================")
    log.info("Gamefa Bot (DeepSeek) started successfully.")
    log.info("Admin ID: %s", ADMIN_ID)
    log.info("Channel: %s", CHANNEL_ID)
    log.info("Model: %s", MODEL)
    log.info("Memory: %s articles", len(memory))
    log.info("========================================")

    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
