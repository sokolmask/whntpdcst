#!/usr/bin/env python3
"""news_digest.py — утренний новостной дайджест из Telegram-каналов.

Собирает посты за последние N часов из каналов, перечисленных в
sources.news.yaml (лежит в NEWS_DATA_DIR — личный список, не в репо),
генерирует компактный дайджест (Израиль / Россия / Мир) через OpenRouter
и шлёт в Telegram Saved Messages той же user-сессией.

Usage:
    python news_digest.py                  # last 24h → Saved Messages
    python news_digest.py --hours 12
    python news_digest.py --dry-run        # print digest, don't send / mark seen

Environment variables:
    TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION — user session (tg_login.py)
    OPENROUTER_API_KEY   — OpenRouter API key
    NEWS_DATA_DIR        — data dir override (default /opt/data/news)
"""

import os
import sys
import re
import json
import time
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = "google/gemini-2.5-flash"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

DATA_DIR = Path(os.environ.get("NEWS_DATA_DIR", "/opt/data/news"))
SOURCES_PATH = DATA_DIR / "sources.news.yaml"
SEEN_PATH = DATA_DIR / "seen.json"
DIGESTS_DIR = DATA_DIR / "digests"

SEEN_KEEP_DAYS = 7
MAX_CONTEXT_CHARS = 120000
TG_MESSAGE_LIMIT = 3800  # under Telegram's 4096, leaves room for entities

SECTION_ORDER = ["израиль", "россия", "мир"]
SECTION_TITLES = {
    "израиль": "🇮🇱 Израиль и Ближний Восток",
    "россия": "🇷🇺 Россия",
    "мир": "🌍 Мир",
}

NEWS_PROMPT = """Ты — редактор утреннего новостного дайджеста для русскоязычного читателя, живущего в Израиле.

Ниже — посты из Telegram-каналов за последние {hours} часов, сгруппированные по секциям.
Составь компактный утренний дайджест на русском языке.

Правила:
- Три секции строго в этом порядке: **🇮🇱 Израиль и Ближний Восток**, **🇷🇺 Россия**, **🌍 Мир**
- В каждой секции 3-6 главных тем. Каждая тема — один пункт: строка «— **суть в 3-5 словах:** 1-2 предложения конкретных фактов (кто, что, где, цифры)»
- Одна и та же новость из разных каналов — ОДИН пункт, объедини детали
- В конце пункта ссылка на самый информативный пост: [название канала](https://t.me/...)
- Посты на иврите и английском переводи на русский
- Аналитику и прогнозы помечай «(мнение канала)» — не выдавай за факты
- Начни с заголовка «**Утренний дайджест — {date}**», без вступлений; закончи последним пунктом, без выводов
- Если в секции нет заметных новостей — одна строка «Тихо.»
- Формат — Telegram markdown: **жирный**, [текст](url). Никаких #, ##, таблиц и HTML.

Посты:
{context}"""


def load_sources() -> list[dict]:
    import yaml
    if not SOURCES_PATH.exists():
        sys.exit(f"ОШИБКА: конфиг источников не найден: {SOURCES_PATH}")
    cfg = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    channels = [c for c in cfg.get("channels", []) if c.get("enabled", True)]
    if not channels:
        sys.exit(f"ОШИБКА: нет активных каналов в {SOURCES_PATH}")
    return channels


def load_seen() -> dict:
    """{post_key: iso_date} of posts already digested."""
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[seen] seen.json не читается, начинаю с пустого: {e}")
    return {}


def save_seen(seen: dict, new_keys: list[str]) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for k in new_keys:
        seen[k] = today
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[seen] +{len(new_keys)} постов → {SEEN_PATH} (всего {len(seen)})")


def israel_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Jerusalem"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=3)))


def fetch_posts(
    hours_back: int, channels: list[dict], seen: dict
) -> tuple[dict[str, list[str]], list[str]]:
    """Fetch recent posts via Telethon user session.

    Returns ({section: [channel context blocks]}, [new post keys]).
    """
    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION", "")
    if not (api_id and api_hash and session):
        sys.exit("ОШИБКА: TELEGRAM_API_ID/API_HASH/SESSION не заданы (одноразовый логин: tg_login.py)")
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    sections: dict[str, list[str]] = {s: [] for s in SECTION_ORDER}
    new_keys: list[str] = []

    with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
        for i, ch in enumerate(channels, 1):
            name = str(ch.get("name", "")).strip()
            section = str(ch.get("section", "мир")).strip().lower()
            if section not in sections:
                section = "мир"
            max_posts = int(ch.get("max_posts", 8))
            min_chars = int(ch.get("min_post_chars", 60))
            if not name:
                continue
            print(f"[TG {i}/{len(channels)}] {name} ({section})", end="", flush=True)
            try:
                ident = int(name) if re.fullmatch(r"-?\d+", name) else name
                entity = client.get_entity(ident)
                username = getattr(entity, "username", None)
                chan_key = username or str(getattr(entity, "id", name))
                title = getattr(entity, "title", name)

                posts = []
                for msg in client.iter_messages(entity, limit=60):
                    if msg.date < cutoff:
                        break
                    text = (msg.message or "").strip()
                    if len(text) < min_chars:
                        continue
                    key = f"tg:{chan_key}/{msg.id}"
                    if key in seen:
                        continue
                    link = (f"https://t.me/{username}/{msg.id}" if username
                            else f"(приватный канал «{title}», пост {msg.id})")
                    posts.append((key, text, link, msg.date))
                    if len(posts) >= max_posts:
                        break

                if not posts:
                    print(" — нет новых постов")
                    continue

                block = [f"### Канал: {title}"]
                for key, text, link, date in posts:
                    new_keys.append(key)
                    block.append(f"Пост ({date.strftime('%Y-%m-%d %H:%M')} UTC) {link}\n{text[:1200]}")
                sections[section].append("\n\n".join(block))
                print(f" — {len(posts)} постов")

            except Exception as e:
                print(f" — ошибка: {e}")
            time.sleep(0.5)  # gentle: user session, don't hammer

    return sections, new_keys


def build_context(sections: dict[str, list[str]]) -> str:
    parts = []
    for s in SECTION_ORDER:
        if sections[s]:
            parts.append(f"# Секция: {SECTION_TITLES[s]}\n\n" + "\n\n".join(sections[s]))
    context = "\n\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n[... посты обрезаны ...]"
    return context


def generate_digest(context: str, hours_back: int) -> str:
    if not OPENROUTER_API_KEY:
        sys.exit("ОШИБКА: OPENROUTER_API_KEY не задан")
    date = israel_now().strftime("%d.%m.%Y")
    print(f"[LLM] Генерирую дайджест ({len(context)} символов контекста)...")
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": NEWS_PROMPT.format(
            hours=hours_back, date=date, context=context)}],
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "Morning News Digest",
    }
    with httpx.Client(timeout=180) as client:
        resp = client.post(OPENROUTER_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        digest = resp.json()["choices"][0]["message"]["content"].strip()
    print(f"[LLM] Дайджест готов: {len(digest)} символов")
    return digest


def split_for_telegram(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Split at paragraph boundaries into chunks under the message limit."""
    chunks, cur, cur_len = [], [], 0
    for para in text.split("\n\n"):
        para_len = len(para) + 2
        if cur and cur_len + para_len > limit:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(para)
        cur_len += para_len
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def send_to_saved_messages(digest: str) -> None:
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION", "")
    chunks = split_for_telegram(digest)
    with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
        for i, chunk in enumerate(chunks, 1):
            client.send_message("me", chunk, parse_mode="md", link_preview=False)
            print(f"[send] Сообщение {i}/{len(chunks)} → Saved Messages")
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Утренний новостной дайджест из Telegram")
    parser.add_argument("--hours", type=int, default=24, help="За сколько часов брать посты (default 24)")
    parser.add_argument("--dry-run", action="store_true", help="Напечатать дайджест, не отправлять и не помечать seen")
    args = parser.parse_args()

    now = israel_now()
    print(f"=== Утренний дайджест — {now.strftime('%Y-%m-%d %H:%M')} (за {args.hours}ч) ===")

    channels = load_sources()
    seen = load_seen()
    if seen:
        print(f"[seen] В базе: {len(seen)} постов")

    sections, new_keys = fetch_posts(args.hours, channels, seen)
    context = build_context(sections)
    if not context:
        print("Нет новых постов — дайджест не нужен")
        return
    print(f"[context] Всего символов: {len(context)}, новых постов: {len(new_keys)}")

    digest = generate_digest(context, args.hours)

    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGESTS_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    if digest_path.exists():
        digest_path = DIGESTS_DIR / f"{now.strftime('%Y-%m-%d-%H%M')}.md"
    digest_path.write_text(digest, encoding="utf-8")
    print(f"[digest] Сохранён → {digest_path}")

    if args.dry_run:
        print(f"\n{'=' * 60}\n{digest}\n{'=' * 60}")
        print("[dry-run] Не отправляю и не помечаю seen. Готово.")
        return

    send_to_saved_messages(digest)
    save_seen(seen, new_keys)
    print("=== Готово ===")


if __name__ == "__main__":
    main()
