#!/usr/bin/env python3
"""
podcast_skill.py — Hermes skill: generate a Russian AI podcast episode.

Called by Hermes when user says "сделай подкаст" or similar.

Usage:
    python podcast_skill.py                  # last 7 days
    python podcast_skill.py --days 14        # last 14 days
    python podcast_skill.py --dry-run        # script only, no TTS/MP3

Environment variables:
    YOUTUBE_API_KEY      — YouTube Data API v3
    OPENROUTER_API_KEY   — OpenRouter API key
    GEMINI_API_KEY       — Google AI Studio key (multi-speaker TTS; fallback: edge-tts)
    PODCAST_DATA_DIR     — data dir override (default /opt/data/podcast)
"""

import os
import sys
import re
import json
import time
import argparse
import tempfile
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# ── Config ────────────────────────────────────────────────────────────────────

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

OPENROUTER_MODEL = "google/gemini-2.5-flash"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_API_URL = "https://generativelanguage.googleapis.com"
GEMINI_TTS_SAMPLE_RATE = 24000       # PCM s16le mono
GEMINI_TTS_BLOCK_CHARS = 3500        # max script chars per TTS request

EDGE_TTS = "/opt/hermes/.venv/bin/edge-tts"
DATA_DIR = Path(os.environ.get("PODCAST_DATA_DIR", "/opt/data/podcast"))
EPISODES_DIR = DATA_DIR / "episodes"
RSS_DATA_DIR = DATA_DIR
DIGESTS_DIR = DATA_DIR / "digests"
BASE_URL = "https://whntpdcst.com"

VOICE_ALEX = "ru-RU-DmitryNeural"   # Алекс — male
VOICE_SASHA = "ru-RU-SvetlanaNeural"  # Саша — female
GEMINI_VOICE_ALEX = "Charon"   # male
GEMINI_VOICE_SASHA = "Leda"    # female

SILENCE_BETWEEN_SPEAKERS_SEC = 0.3

SOURCES_PATH = Path(os.environ.get("PODCAST_SOURCES", Path(__file__).parent / "sources.yaml"))
EXTRA_SOURCES_PATH = DATA_DIR / "sources.extra.yaml"   # user additions from admin panel
COVERED_PATH = DATA_DIR / "covered.json"               # items already covered in published episodes


def load_sources() -> dict:
    """Load sources.yaml, merged with user additions from sources.extra.yaml."""
    import yaml
    if not SOURCES_PATH.exists():
        sys.exit(f"ОШИБКА: конфиг источников не найден: {SOURCES_PATH}")
    cfg = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    if EXTRA_SOURCES_PATH.exists():
        try:
            extra = yaml.safe_load(EXTRA_SOURCES_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[sources] sources.extra.yaml не распарсился, игнорирую: {e}")
            extra = {}
        channels = cfg.setdefault("youtube", {}).setdefault("channels", [])
        known = {c.get("handle", "").lower() for c in channels}
        added = 0
        for c in extra.get("youtube", {}).get("channels", []):
            if c.get("handle") and c["handle"].lower() not in known:
                channels.append(c)
                added += 1
        queries = cfg.setdefault("hackernews", {}).setdefault("queries", [])
        for q in extra.get("hackernews", {}).get("queries", []):
            if q not in queries:
                queries.append(q)
                added += 1
        tg_channels = cfg.setdefault("telegram", {}).setdefault("channels", [])
        known_tg = {str(c.get("name", "")).lower() for c in tg_channels}
        for c in extra.get("telegram", {}).get("channels", []):
            if c.get("name") and str(c["name"]).lower() not in known_tg:
                tg_channels.append(c)
                added += 1
        if added:
            print(f"[sources] +{added} доп. источников из sources.extra.yaml")
    return cfg


def load_covered() -> dict:
    """Load {item_id: {"episode": date, "title": ...}} of already-covered items."""
    if COVERED_PATH.exists():
        try:
            return json.loads(COVERED_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[covered] covered.json не читается, начинаю с пустого: {e}")
    return {}


def mark_covered(items: dict[str, str], episode_date: str) -> None:
    """Record items as covered by the episode published on episode_date."""
    covered = load_covered()
    for item_id, title in items.items():
        covered[item_id] = {"episode": episode_date, "title": title}
    COVERED_PATH.write_text(
        json.dumps(covered, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[covered] +{len(items)} материалов → {COVERED_PATH} (всего {len(covered)})")

DIGEST_PROMPT = """Ты — редактор еженедельного дайджеста «Что нового в AI».

На основе сырых материалов ниже (транскрипты YouTube, HackerNews, свежие статьи) составь \
структурированный дайджест недели на русском языке в формате Markdown.

Правила:
- Начни с заголовка первого уровня: # Что нового в AI — {date}
- Отбери 6-10 самых значимых тем недели, каждая — раздел с заголовком ##
- В каждой теме: что произошло (конкретные факты, цифры, названия), почему это важно, \
что это значит на практике для людей и разработчиков
- В конце темы строка «Источники:» со ссылками из материалов, если они там есть
- Раздел «## Коротко» в конце — остальные заметные новости, по одной строке
- Пиши плотно и фактурно, без воды, вступлений и выводов «в целом неделя показала»
- Приоритет тем: (1) большие международные игроки — OpenAI, Anthropic, Google, Meta, xAI, \
Mistral, DeepSeek и т.п. — и что их шаги значат на самом деле; (2) инновации и новые подходы; \
(3) израильская и китайская стартап-сцена — их идеи и продукты разбирай как ранний сигнал: \
то, что они делают сейчас, остальной мир будет делать через год-полтора
- Новости российского рынка (Сбер, Яндекс, GigaChat, MTS AI и т.п.) отдельными темами НЕ делай — \
максимум строка в «Коротко», и только если новость заметна на мировом уровне
- Английские названия продуктов, моделей и компаний оставляй латиницей как есть
- Темы из раздела «Уже освещалось в прошлых выпусках» заново НЕ разбирай — максимум \
короткая отсылка «об этом говорили в выпуске от ...» внутри связанной новой темы
{focus_block}
Материалы этой недели:
{context}"""

DIGEST_FOCUS_NOTE = """
Особый фокус этого выпуска: {focus}
- Темы, относящиеся к фокусу, отбирай в первую очередь и разбирай заметно глубже \
(конкретика, детали, разбор «что это значит на самом деле»)
- Прочие значимые новости недели — короче обычного или строкой в «Коротко»
"""

SCRIPT_PROMPT = """Ты пишешь сценарий для подкаста «Что нового в AI» с двумя ведущими.

Ведущий АЛЕКС — мужчина, аналитичный, лаконичный, любит конкретные факты и цифры, задаёт острые вопросы.
Ведущий САША — женщина, тёплая, связывает темы, делает практические выводы, говорит живо и понятно.

Стиль: как NotebookLM Audio Overview — живой, естественный разговор, не лекция. Ведущие перебивают друг друга, \
уточняют, иногда удивляются. Без формальных переходов типа «теперь поговорим о...».

На основе дайджеста ниже напиши эпизод подкаста ({length_hint}) на русском языке.

Правила:
- Охвати {topics_hint}
- Если ведущие ссылаются на тему прошлого выпуска — одной фразой, не пересказывая её
- По каждой теме: что произошло → почему важно → что это значит для людей/разработчиков
- Только живой разговор — никаких списков, никаких буллетов в репликах
- Начинай сразу с темы — без «добро пожаловать», без «сегодня мы поговорим»
- Заканчивай одним коротким выводом недели
- Формат строго (каждая реплика на новой строке, без пустых строк между репликами одного блока):

АЛЕКС: текст реплики
САША: текст реплики
АЛЕКС: текст реплики
{focus_block}
Дайджест недели:
{digest}"""

SCRIPT_FOCUS_NOTE = """
Особый фокус выпуска: {focus} — этим темам удели основное время и глубину, \
остальные обсуждайте короче.
"""

SEGMENT_PROMPT = """Ты пишешь фрагмент сценария подкаста «Что нового в AI» с двумя ведущими.

Ведущий АЛЕКС — мужчина, аналитичный, лаконичный, любит конкретные факты и цифры, задаёт острые вопросы.
Ведущий САША — женщина, тёплая, связывает темы, делает практические выводы, говорит живо и понятно.

Стиль: как NotebookLM Audio Overview — живой, естественный разговор, не лекция. Ведущие перебивают друг друга, \
уточняют, иногда удивляются. Без формальных переходов типа «теперь поговорим о...».

Это ОДИН ФРАГМЕНТ длинного выпуска — разговор по одной теме, примерно {words} слов диалога. \
Не растекайся: это один фрагмент из многих, уложись в объём. \
Разбирайте тему глубоко: что произошло (конкретные факты, цифры, названия) → почему это важно → \
что это значит на практике для отрасли и разработчиков → где подводные камни.

{position}
{continuity}
Правила:
- Только живой разговор — никаких списков и буллетов в репликах
- НЕ завершай выпуск: не прощайся, не подводи итоги недели — после этой темы разговор продолжится
- Последняя реплика фрагмента — естественная точка, после которой можно перейти к новой теме
- Формат строго (каждая реплика на новой строке):

АЛЕКС: текст реплики
САША: текст реплики

Тема фрагмента:
## {title}
{body}"""

FINAL_SEGMENT_PROMPT = """Ты пишешь финальный фрагмент сценария подкаста «Что нового в AI» с двумя ведущими.

Ведущий АЛЕКС — мужчина, аналитичный, лаконичный, любит конкретные факты и цифры, задаёт острые вопросы.
Ведущий САША — женщина, тёплая, связывает темы, делает практические выводы, говорит живо и понятно.

Стиль: как NotebookLM Audio Overview — живой, естественный разговор, не лекция.

Это КОНЕЦ выпуска. Сначала живой блиц по коротким новостям ниже (одна-две реплики на новость), \
затем один короткий вывод недели — и всё, без долгих прощаний. Весь финал — примерно 250-300 слов.
{continuity}
Правила:
- Только живой разговор — никаких списков и буллетов в репликах
- Формат строго (каждая реплика на новой строке):

АЛЕКС: текст реплики
САША: текст реплики

Блиц-новости:
{korotko}"""

CONDENSE_PROMPT = """Сократи фрагмент диалога подкаста примерно до {words} слов.
Сохрани формат реплик АЛЕКС:/САША: (каждая на новой строке), самые важные факты, цифры и живость разговора.
Убирай второстепенные ответвления и повторы, ничего нового не добавляй.
Первая и последняя реплики должны остаться связующими — фрагмент стоит в середине выпуска.

Фрагмент:
{segment}"""


TRANSLATE_DIGEST_PROMPT = """Translate this Russian Markdown digest of AI news into English.
Keep the Markdown structure, headings, links and facts exactly as they are.
Write natural tech-journalism English. Leave product/company names as is.
Output only the translated Markdown, nothing else.

{digest}"""

DIGEST_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 42rem; margin: 0 auto; padding: 1.5rem; line-height: 1.6;
         color: #1a1a2e; background: #fdfdfb; }}
  h1 {{ font-size: 1.6rem; line-height: 1.3; }}
  h2 {{ font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: .3rem; }}
  a {{ color: #3563a8; word-break: break-all; }}
  li {{ margin-bottom: .4rem; }}
  .footer {{ margin-top: 3rem; font-size: .85rem; color: #888; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e8e8e8; background: #16161e; }}
    h2 {{ border-color: #333; }}
    a {{ color: #7fa7e0; }}
  }}
</style>
</head>
<body>
{body}
<p class="footer">{footer}</p>
</body>
</html>
"""

DIGEST_FOOTERS = {
    "ru": 'Дайджест сгенерирован AI · <a href="{base_url}/feed.xml">Подкаст «Что нового в AI»</a>',
    "en": 'AI-generated digest · <a href="{base_url}/feed.xml">«Что нового в AI» podcast</a>',
}


def render_digest_html(digest_md: str, out_path: Path, title: str, lang: str = "ru") -> None:
    """Render the MD digest to a standalone mobile-friendly HTML page."""
    import markdown

    # Wrap bare URLs into <...> so python-markdown turns them into links
    linked = re.sub(r'(?<![\(<"])(https?://[^\s<>\)\]",]+)', r"<\1>", digest_md)
    body = markdown.markdown(linked, extensions=["extra"])
    footer = DIGEST_FOOTERS.get(lang, DIGEST_FOOTERS["ru"]).format(base_url=BASE_URL)
    out_path.write_text(
        DIGEST_HTML_TEMPLATE.format(title=title, body=body, lang=lang, footer=footer),
        encoding="utf-8",
    )


# ── YouTube helpers ───────────────────────────────────────────────────────────

def yt_get(client: httpx.Client, endpoint: str, **params) -> dict:
    """Call YouTube Data API v3."""
    resp = client.get(
        f"https://www.googleapis.com/youtube/v3/{endpoint}",
        params={"key": YOUTUBE_API_KEY, **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_uploads_playlist(client: httpx.Client, handle: str) -> str | None:
    """Resolve @handle to the channel's uploads playlist ID (1 quota unit)."""
    try:
        data = yt_get(client, "channels", part="contentDetails", forHandle=handle.lstrip("@"))
        items = data.get("items", [])
        if not items:
            return None
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as e:
        print(f"  [YT] resolve {handle}: {e}")
        return None


def get_recent_videos(client: httpx.Client, playlist_id: str, days_back: int, max_results: int = 3) -> list[dict]:
    """Get recent videos from the uploads playlist (1 quota unit vs 100 for search)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days_back)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = yt_get(
            client, "playlistItems",
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=10,
        )
        videos = []
        for item in data.get("items", []):
            snippet = item["snippet"]
            published = item["contentDetails"].get("videoPublishedAt") or snippet["publishedAt"]
            if published < cutoff:
                continue
            videos.append({
                "id": item["contentDetails"]["videoId"],
                "title": snippet["title"],
                "published": published[:10],
                "description": snippet["description"][:300],
            })
            if len(videos) >= max_results:
                break
        return videos
    except Exception as e:
        print(f"  [YT] get videos for playlist {playlist_id}: {e}")
        return []


def get_transcript(video_id: str, max_chars: int = 5000) -> str | None:
    """Fetch transcript preferring Russian then English."""
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)
        for fetcher in [
            lambda: tlist.find_transcript(["ru"]),
            lambda: tlist.find_transcript(["en"]),
            lambda: tlist.find_generated_transcript(["ru"]),
            lambda: tlist.find_generated_transcript(["en"]),
            lambda: next(iter(tlist)),
        ]:
            try:
                t = fetcher()
                text = " ".join(e["text"] for e in t.fetch())
                return text[:max_chars]
            except Exception:
                continue
    except (TranscriptsDisabled, NoTranscriptFound):
        pass
    except Exception:
        pass
    return None


def fetch_youtube_context(
    days_back: int,
    channels: list[tuple[str, str]],
    covered: dict,
    new_items: dict[str, str],
    old_mentions: list[str],
) -> tuple[str, list[str]]:
    """Fetch transcripts from configured channels.

    Skips videos already covered in published episodes (adds them to
    old_mentions instead); records fresh video ids into new_items.
    Returns (formatted context string, list of sources that had videos).
    """
    if not YOUTUBE_API_KEY:
        print("[YT] YOUTUBE_API_KEY not set — skipping YouTube")
        return "", []
    if not channels:
        print("[YT] нет активных каналов в sources.yaml")
        return "", []

    parts = []
    sources = []
    client = httpx.Client()

    for i, (handle, category) in enumerate(channels, 1):
        print(f"[YT {i}/{len(channels)}] {handle} ({category})", end="", flush=True)
        try:
            playlist_id = resolve_uploads_playlist(client, handle)
            if not playlist_id:
                print(" — не найден")
                continue

            videos = get_recent_videos(client, playlist_id, days_back, max_results=2)
            fresh = []
            for v in videos:
                key = f"yt:{v['id']}"
                if key in covered:
                    old_mentions.append(
                        f"{v['title']} ({handle}) — выпуск от {covered[key].get('episode', '?')}"
                    )
                else:
                    fresh.append(v)
            if not videos:
                print(" — нет видео")
                continue
            if not fresh:
                print(" — всё уже освещалось")
                continue

            print(f" — {len(fresh)} видео", end="", flush=True)

            channel_parts = [f"\n## YouTube: {handle} ({category})\n"]
            for v in fresh:
                new_items[f"yt:{v['id']}"] = v["title"]
                channel_parts.append(
                    f"Видео: {v['title']} ({v['published']}) https://youtube.com/watch?v={v['id']}"
                )
                transcript = get_transcript(v["id"])
                if transcript:
                    channel_parts.append(f"Транскрипт: {transcript}")
                    print(".", end="", flush=True)
                else:
                    channel_parts.append(f"Описание: {v['description']}")
                    print("d", end="", flush=True)

            parts.append("\n".join(channel_parts))
            sources.append(f"YouTube {handle} ({category})")
            print()

        except Exception as e:
            print(f" — ошибка: {e}")

        time.sleep(0.2)  # gentle rate limiting

    client.close()
    return "\n\n".join(parts), sources


# ── Web sources ───────────────────────────────────────────────────────────────

def fetch_hn_ai(
    queries: list[str],
    min_points: int = 30,
    max_items: int = 10,
    covered: dict | None = None,
    new_items: dict[str, str] | None = None,
    old_mentions: list[str] | None = None,
) -> str:
    """Fetch top AI stories from HackerNews RSS."""
    parts = []
    seen = set()
    covered = covered or {}

    try:
        client = httpx.Client()
        import feedparser  # type: ignore
        for query in queries:
            url = f"https://hnrss.org/newest?q={query.replace(' ', '+')}&points={min_points}&count=8"
            feed = feedparser.parse(url)
            for entry in feed.entries:
                link = entry.get("link", "")
                if link in seen:
                    continue
                seen.add(link)
                title = entry.get("title", "")
                if f"hn:{link}" in covered:
                    if old_mentions is not None:
                        old_mentions.append(
                            f"{title} (HN) — выпуск от {covered[f'hn:{link}'].get('episode', '?')}"
                        )
                    continue
                summary = entry.get("summary", "")
                score_m = re.search(r"Points:\s*(\d+)", summary)
                score = score_m.group(1) if score_m else "?"
                if new_items is not None:
                    new_items[f"hn:{link}"] = title
                parts.append(f"- {title} (HN score: {score})\n  {link}")
                if len(parts) >= max_items:
                    break
            if len(parts) >= max_items:
                break
        client.close()
    except Exception as e:
        print(f"  [HN] ошибка: {e}")

    if not parts:
        return ""
    return "## HackerNews — топ AI материалы\n" + "\n".join(parts)


def fetch_hf_papers(
    days_back: int = 7,
    max_items: int = 8,
    covered: dict | None = None,
    new_items: dict[str, str] | None = None,
    old_mentions: list[str] | None = None,
) -> str:
    """Fetch recent papers from HuggingFace daily papers."""
    parts = []
    seen = set()
    covered = covered or {}

    try:
        client = httpx.Client()
        from bs4 import BeautifulSoup  # type: ignore

        for offset in range(min(days_back, 5)):
            date = (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")
            resp = client.get(
                f"https://huggingface.co/papers?date={date}",
                headers={"User-Agent": "Mozilla/5.0 (podcast-bot/1.0)"},
                timeout=15,
                follow_redirects=True,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            for article in soup.select("article"):
                h3 = article.find("h3")
                if not h3:
                    continue
                title = h3.get_text(strip=True)
                a_tag = article.find("a", href=re.compile(r"^/papers/\d"))
                if not a_tag:
                    continue
                href = a_tag["href"]
                if href in seen:
                    continue
                seen.add(href)
                if f"hf:{href}" in covered:
                    if old_mentions is not None:
                        old_mentions.append(
                            f"{title} (HF paper) — выпуск от {covered[f'hf:{href}'].get('episode', '?')}"
                        )
                    continue
                abstract_tag = article.find("p")
                abstract = abstract_tag.get_text(strip=True)[:300] if abstract_tag else ""
                if new_items is not None:
                    new_items[f"hf:{href}"] = title
                parts.append(f"- {title} ({date})\n  https://huggingface.co{href}\n  {abstract}")
                if len(parts) >= max_items:
                    break
            if len(parts) >= max_items:
                break
            time.sleep(0.5)
        client.close()
    except Exception as e:
        print(f"  [HF] ошибка: {e}")

    if not parts:
        return ""
    return "## HuggingFace Papers — свежие статьи\n" + "\n".join(parts)


def fetch_telegram_context(
    days_back: int,
    tg_cfg: dict,
    covered: dict,
    new_items: dict[str, str],
    old_mentions: list[str],
) -> tuple[str, list[str]]:
    """Fetch recent posts from subscribed Telegram channels via a user session.

    Requires TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION env vars
    (one-time login: tg_login.py). Works for private channels the account
    is subscribed to. Covered posts are skipped like other sources.
    """
    channels = [c for c in tg_cfg.get("channels", []) if c.get("enabled", True)]
    if not tg_cfg.get("enabled", True) or not channels:
        return "", []

    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session = os.environ.get("TELEGRAM_SESSION", "")
    if not (api_id and api_hash and session):
        print("[TG] TELEGRAM_API_ID/API_HASH/SESSION не заданы — пропускаю Telegram (одноразовый логин: tg_login.py)")
        return "", []
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("[TG] telethon не установлен — пропускаю Telegram")
        return "", []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    max_posts = int(tg_cfg.get("max_posts_per_channel", 5))
    min_chars = int(tg_cfg.get("min_post_chars", 200))
    parts: list[str] = []
    sources: list[str] = []

    with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
        for i, ch in enumerate(channels, 1):
            name = str(ch.get("name", "")).strip()
            category = ch.get("category", "")
            if not name:
                continue
            print(f"[TG {i}/{len(channels)}] {name} ({category})", end="", flush=True)
            try:
                # name: @username or numeric id (private channels have no username)
                ident = int(name) if re.fullmatch(r"-?\d+", name) else name
                entity = client.get_entity(ident)
                username = getattr(entity, "username", None)
                chan_key = username or str(getattr(entity, "id", name))
                title = getattr(entity, "title", name)

                posts = []
                for msg in client.iter_messages(entity, limit=50):
                    if msg.date < cutoff:
                        break
                    text = (msg.message or "").strip()
                    if len(text) < min_chars:
                        continue
                    key = f"tg:{chan_key}/{msg.id}"
                    if key in covered:
                        old_mentions.append(
                            f"{text[:80]}… (TG {title}) — выпуск от {covered[key].get('episode', '?')}"
                        )
                        continue
                    link = (f"https://t.me/{username}/{msg.id}" if username
                            else f"(приватный канал «{title}», пост {msg.id})")
                    posts.append((key, text, link, msg.date))
                    if len(posts) >= max_posts:
                        break

                if not posts:
                    print(" — нет новых постов")
                    continue

                chan_parts = [f"\n## Telegram: {title} ({category})\n"]
                for key, text, link, date in posts:
                    new_items[key] = f"{title}: {text[:100]}"
                    chan_parts.append(f"Пост ({date.strftime('%Y-%m-%d')}) {link}\n{text[:1500]}")
                parts.append("\n\n".join(chan_parts))
                sources.append(f"Telegram {title}")
                print(f" — {len(posts)} постов")

            except Exception as e:
                print(f" — ошибка: {e}")
            time.sleep(0.5)  # gentle: user session, don't hammer

    return "\n\n".join(parts), sources


def fetch_web_context(
    days_back: int,
    hn_cfg: dict,
    hf_cfg: dict,
    covered: dict,
    new_items: dict[str, str],
    old_mentions: list[str],
) -> tuple[str, list[str]]:
    """Fetch HN + HF papers per config. Returns (combined context string, list of sources)."""
    hn = ""
    if hn_cfg.get("enabled", True):
        print("[Web] HackerNews...", end="", flush=True)
        hn = fetch_hn_ai(
            queries=hn_cfg.get("queries", ["AI LLM", "AI agent"]),
            min_points=hn_cfg.get("min_points", 30),
            max_items=hn_cfg.get("max_items", 10),
            covered=covered, new_items=new_items, old_mentions=old_mentions,
        )
        print(f" {hn.count(chr(10))} строк")

    hf = ""
    if hf_cfg.get("enabled", True):
        print("[Web] HuggingFace Papers...", end="", flush=True)
        hf = fetch_hf_papers(
            days_back, max_items=hf_cfg.get("max_items", 8),
            covered=covered, new_items=new_items, old_mentions=old_mentions,
        )
        print(f" {hf.count(chr(10))} строк")

    sources = []
    if hn:
        sources.append("HackerNews")
    if hf:
        sources.append("HuggingFace Papers")
    return "\n\n".join(filter(None, [hn, hf])), sources


def fetch_rss_context(
    days_back: int,
    rss_cfg: dict,
    covered: dict,
    new_items: dict[str, str],
    old_mentions: list[str],
) -> tuple[str, list[str]]:
    """Fetch recent entries from configured RSS feeds (tech press, newsletters)."""
    feeds = [f for f in rss_cfg.get("feeds", []) if f.get("enabled", True)]
    if not rss_cfg.get("enabled", True) or not feeds:
        return "", []

    import feedparser  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    max_items = int(rss_cfg.get("max_items_per_feed", 5))
    parts: list[str] = []
    sources: list[str] = []

    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (podcast-bot/1.0)"}) as client:
        for i, f in enumerate(feeds, 1):
            name, url, category = f.get("name", "?"), f.get("url", ""), f.get("category", "")
            print(f"[RSS {i}/{len(feeds)}] {name}", end="", flush=True)
            try:
                feed = feedparser.parse(client.get(url).content)
                items = []
                for entry in feed.entries:
                    ts = entry.get("published_parsed") or entry.get("updated_parsed")
                    if ts and datetime(*ts[:6], tzinfo=timezone.utc) < cutoff:
                        continue
                    link = entry.get("link", "")
                    title = entry.get("title", "").strip()
                    if not link or not title:
                        continue
                    if f"rss:{link}" in covered:
                        old_mentions.append(
                            f"{title} ({name}) — выпуск от {covered[f'rss:{link}'].get('episode', '?')}"
                        )
                        continue
                    summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
                    new_items[f"rss:{link}"] = title
                    items.append(f"- {title}\n  {link}\n  {summary[:1200]}")
                    if len(items) >= max_items:
                        break
                if items:
                    parts.append(f"## RSS: {name} ({category})\n" + "\n".join(items))
                    sources.append(f"{name} ({category})")
                print(f" — {len(items)} материалов")
            except Exception as e:
                print(f" — ошибка: {e}")

    return "\n\n".join(parts), sources


# ── Digest & script generation ────────────────────────────────────────────────

def call_llm(prompt: str, temperature: float, max_tokens: int = 4096) -> str:
    """Call OpenRouter chat completions, return message content."""
    if not OPENROUTER_API_KEY:
        sys.exit("ERROR: OPENROUTER_API_KEY not set")

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": BASE_URL,
        "X-Title": "AI Podcast Generator",
    }

    with httpx.Client(timeout=180) as client:
        resp = client.post(OPENROUTER_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return data["choices"][0]["message"]["content"]


def generate_digest(context: str, date: str, focus: str | None = None) -> str:
    """Stage 1: raw materials → structured Markdown digest."""
    # Truncate context (~40k tokens for Gemini Flash; TG sources come last,
    # so a low cap would silently drop them)
    if len(context) > 150000:
        context = context[:150000] + "\n\n[... материалы обрезаны ...]"

    focus_block = DIGEST_FOCUS_NOTE.format(focus=focus) if focus else ""
    print(f"[LLM] Генерирую дайджест ({len(context)} символов контекста)...")
    digest = call_llm(
        DIGEST_PROMPT.format(date=date, context=context, focus_block=focus_block),
        temperature=0.4, max_tokens=8192,
    )
    print(f"[LLM] Дайджест готов: {len(digest)} символов")
    return digest


def split_digest_sections(digest: str) -> tuple[list[tuple[str, str]], str]:
    """Split digest MD into topic sections [(title, body), ...] + «Коротко» body."""
    parts = re.split(r"^##\s+", digest, flags=re.MULTILINE)
    topics, korotko = [], ""
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        title, body = title.strip(), body.strip()
        if title.startswith("Источники"):
            continue
        if title.startswith("Коротко"):
            korotko = body
        else:
            topics.append((title, body))
    return topics, korotko


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def generate_script_chunked(digest: str, minutes: int) -> str:
    """Long episodes: one LLM call per digest topic + a finale.
    A single call reliably undershoots length targets past ~12 minutes."""
    topics, korotko = split_digest_sections(digest)
    # Gemini TTS speaks Russian dialogue at ~145 wpm (measured on episode 3)
    words_per_topic = max(int(minutes * 145 / (len(topics) + 1)), 250)
    print(f"[LLM] Длинный выпуск: {len(topics)} тем по ~{words_per_topic} слов + финал...")
    segments, tail = [], ""
    for i, (title, body) in enumerate(topics):
        position = (
            "Это самое начало выпуска — начинай сразу с сути темы, без приветствий и анонсов."
            if i == 0 else
            "Это середина выпуска — разговор уже идёт. Продолжай естественно, без «а теперь поговорим о»."
        )
        continuity = (
            f"\nПоследние реплики предыдущего фрагмента (не повторять, просто продолжить после них):\n{tail}\n"
            if tail else ""
        )
        seg = call_llm(
            SEGMENT_PROMPT.format(words=words_per_topic, position=position,
                                  continuity=continuity, title=title, body=body),
            temperature=0.8,
        ).strip()
        segments.append(seg)
        seg_lines = [l for l in seg.splitlines() if l.strip()]
        tail = "\n".join(seg_lines[-2:])
        print(f"[LLM]   {i + 1}/{len(topics)} «{title}» — {len(seg)} символов")
    if korotko:
        continuity = f"\nПоследние реплики перед финалом (не повторять):\n{tail}\n" if tail else ""
        final = call_llm(
            FINAL_SEGMENT_PROMPT.format(continuity=continuity, korotko=korotko),
            temperature=0.8,
        ).strip()
        segments.append(final)
        print(f"[LLM]   финал — {len(final)} символов")

    # Gemini tends to overshoot per-segment word targets ~2x; condense
    # proportionally when the total is clearly past the requested duration
    target_words = minutes * 145
    total_words = sum(_word_count(s) for s in segments)
    if total_words > target_words * 1.2:
        ratio = target_words / total_words
        print(f"[LLM] Сценарий {total_words} слов при цели {target_words} — ужимаю сегменты (x{ratio:.2f})...")
        for i, seg in enumerate(segments):
            want = max(int(_word_count(seg) * ratio), 150)
            condensed = call_llm(
                CONDENSE_PROMPT.format(words=want, segment=seg), temperature=0.4
            ).strip()
            # keep the original if condensing broke the dialogue format
            if re.search(r"^(АЛЕКС|САША):", condensed, re.MULTILINE):
                segments[i] = condensed
            print(f"[LLM]   сегмент {i + 1}: {_word_count(seg)} → {_word_count(segments[i])} слов")

    script = "\n".join(segments)
    print(f"[LLM] Сценарий готов: {len(script)} символов, ~{_word_count(script)} слов")
    return script


def generate_script(digest: str, minutes: int | None = None, focus: str | None = None) -> str:
    """Stage 2: digest → dialogue script. Length scales with topic count,
    or is pinned by an explicit target duration."""
    if minutes and minutes >= 15 and split_digest_sections(digest)[0]:
        return generate_script_chunked(digest, minutes)
    n_topics = len(re.findall(r"^##\s+(?!Коротко|Источники)", digest, flags=re.MULTILINE)) or 5
    if minutes:
        words_lo = minutes * 145
        words_hi = minutes * 175
        length_hint = f"~{minutes} минут, примерно {words_lo}-{words_hi} слов диалога"
    else:
        words_lo = min(max(n_topics * 230, 600), 1900)
        words_hi = words_lo + 500
        length_hint = f"~{max(words_lo // 170, 3)}-{words_hi // 150} минут, примерно {words_lo}-{words_hi} слов диалога"
    topics_hint = (
        f"все {n_topics} тем дайджеста" if n_topics <= 8
        else "8 самых интересных тем дайджеста"
    )
    focus_block = SCRIPT_FOCUS_NOTE.format(focus=focus) if focus else ""
    print(f"[LLM] Генерирую сценарий из дайджеста ({n_topics} тем → {length_hint})...")
    script = call_llm(
        SCRIPT_PROMPT.format(digest=digest, length_hint=length_hint,
                             topics_hint=topics_hint, focus_block=focus_block),
        temperature=0.8,
        max_tokens=max(4096, words_hi * 4),
    )
    print(f"[LLM] Сценарий готов: {len(script)} символов")
    return script


# ── Script parsing ────────────────────────────────────────────────────────────

def parse_script(script: str) -> list[tuple[str, str]]:
    """Parse 'АЛЕКС: text' / 'САША: text' lines into (speaker, text) pairs."""
    pattern = re.compile(r"^(АЛЕКС|САША):\s*(.+)$", re.MULTILINE)
    lines = []
    for m in pattern.finditer(script):
        speaker = m.group(1)
        text = m.group(2).strip()
        if text:
            lines.append((speaker, text))
    return lines


# ── TTS & audio ───────────────────────────────────────────────────────────────

def tts_chunk(text: str, speaker: str, out_path: Path) -> bool:
    """Run edge-tts for one chunk. Returns True on success."""
    voice = VOICE_ALEX if speaker == "АЛЕКС" else VOICE_SASHA
    cmd = [
        EDGE_TTS,
        "--voice", voice,
        "--text", text,
        "--write-media", str(out_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            print(f"  [TTS] ошибка: {result.stderr.decode()[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"  [TTS] таймаут для {speaker}")
        return False
    except Exception as e:
        print(f"  [TTS] исключение: {e}")
        return False


def make_silence_file(duration_sec: float, out_path: Path) -> bool:
    """Generate a short silence MP3 with ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=44100:cl=mono",
        "-t", str(duration_sec),
        "-q:a", "9",
        "-acodec", "libmp3lame",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=15, check=True)
        return True
    except Exception as e:
        print(f"  [ffmpeg silence] ошибка: {e}")
        return False


def concatenate_audio(chunk_paths: list[Path], output_path: Path) -> bool:
    """Concatenate MP3 files with ffmpeg concat demuxer."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        list_file = Path(f.name)
        for p in chunk_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        list_file.unlink(missing_ok=True)
        if result.returncode != 0:
            print(f"  [ffmpeg concat] ошибка: {result.stderr.decode()[:300]}")
            return False
        return True
    except Exception as e:
        list_file.unlink(missing_ok=True)
        print(f"  [ffmpeg concat] исключение: {e}")
        return False


def get_audio_duration(mp3_path: Path) -> int:
    """Get duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            capture_output=True, timeout=15, text=True,
        )
        return int(float(result.stdout.strip()))
    except Exception:
        return 0


def build_audio(lines: list[tuple[str, str]], output_path: Path) -> int:
    """
    Generate TTS for each line, add silence between speaker changes,
    concatenate to final MP3. Returns duration in seconds.
    """
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    total = len(lines)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        chunk_paths = []

        # Pre-generate silence file
        silence_path = tmp / "silence.mp3"
        make_silence_file(SILENCE_BETWEEN_SPEAKERS_SEC, silence_path)

        for i, (speaker, text) in enumerate(lines):
            print(f"  [TTS {i+1}/{total}] {speaker}: {text[:60]}...")
            chunk_path = tmp / f"chunk_{i:04d}.mp3"

            if tts_chunk(text, speaker, chunk_path):
                chunk_paths.append(chunk_path)

                # Add silence on speaker change (not at the very end)
                if i < total - 1 and silence_path.exists():
                    next_speaker = lines[i + 1][0]
                    if next_speaker != speaker:
                        chunk_paths.append(silence_path)
            else:
                print(f"  [TTS] пропускаю реплику {i+1}")

        if not chunk_paths:
            print("[audio] Нет аудиочанков — выхожу")
            return 0

        print(f"[audio] Склеиваю {len(chunk_paths)} файлов...")
        concat_path = output_path.with_suffix(".concat.mp3")
        success = concatenate_audio(chunk_paths, concat_path)

    if success and concat_path.exists():
        # Re-encode as CBR 64k so seeking works correctly in all players
        print("[audio] Перекодирую в CBR 64k...")
        cmd = [
            "ffmpeg", "-y", "-i", str(concat_path),
            "-acodec", "libmp3lame", "-b:a", "64k", "-ar", "22050", "-ac", "1",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=300, check=True)
            concat_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"  [CBR] ошибка перекодирования: {e} — оставляю concat файл")
            concat_path.rename(output_path)

    if output_path.exists():
        duration = get_audio_duration(output_path)
        size_mb = output_path.stat().st_size / 1_048_576
        print(f"[audio] Готово → {output_path}  ({duration}с, {size_mb:.1f} МБ)")
        return duration
    return 0


# ── Gemini multi-speaker TTS ──────────────────────────────────────────────────

def gemini_tts_request(text: str) -> bytes | None:
    """One multi-speaker TTS request. Returns raw PCM (s16le, 24kHz, mono)."""
    import base64

    payload = {
        "contents": [{"parts": [{"text": "Озвучь этот разговор двух ведущих подкаста живо и естественно:\n\n" + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                {"speaker": "АЛЕКС", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE_ALEX}}},
                {"speaker": "САША", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE_SASHA}}},
            ]}},
        },
    }
    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{GEMINI_API_URL}/v1beta/models/{GEMINI_TTS_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json=payload,
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
            return base64.b64decode(b64)
        except Exception as e:
            print(f"  [Gemini TTS] попытка {attempt + 1}: {e}")
            time.sleep(5)
    return None


def build_audio_gemini(lines: list[tuple[str, str]], output_path: Path) -> int:
    """
    Multi-speaker TTS via Gemini API: dialogue blocks → PCM → CBR 64k MP3.
    Returns duration in seconds (0 on failure — caller falls back to edge-tts).
    """
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)

    # Split dialogue into blocks that fit one TTS request
    blocks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for speaker, text in lines:
        line = f"{speaker}: {text}"
        if cur and cur_len + len(line) > GEMINI_TTS_BLOCK_CHARS:
            blocks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)
    if cur:
        blocks.append("\n".join(cur))

    print(f"[Gemini TTS] {len(lines)} реплик → {len(blocks)} блоков")
    silence = b"\x00" * (int(SILENCE_BETWEEN_SPEAKERS_SEC * GEMINI_TTS_SAMPLE_RATE) * 2)
    pcm = bytearray()
    for i, block in enumerate(blocks, 1):
        print(f"  [Gemini TTS {i}/{len(blocks)}] {len(block)} символов...")
        audio = gemini_tts_request(block)
        if audio is None:
            print(f"[Gemini TTS] блок {i} не озвучился")
            return 0
        if pcm:
            pcm += silence
        pcm += audio

    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
        pcm_path = Path(f.name)
        f.write(bytes(pcm))

    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(GEMINI_TTS_SAMPLE_RATE), "-ac", "1",
        "-i", str(pcm_path),
        "-acodec", "libmp3lame", "-b:a", "64k", "-ar", "22050", "-ac", "1",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300, check=True)
    except Exception as e:
        print(f"  [Gemini TTS ffmpeg] ошибка: {e}")
        return 0
    finally:
        pcm_path.unlink(missing_ok=True)

    duration = get_audio_duration(output_path)
    size_mb = output_path.stat().st_size / 1_048_576
    print(f"[audio] Готово → {output_path}  ({duration}с, {size_mb:.1f} МБ)")
    return duration


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Генерация эпизода подкаста 'Что нового в AI'"
    )
    parser.add_argument("--days", type=int, default=7, help="За сколько дней брать материалы (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="Только сценарий, без TTS и MP3")
    parser.add_argument("--no-publish", action="store_true",
                        help="Сгенерировать MP3+дайджест, но не публиковать в RSS (публикация из админки)")
    parser.add_argument("--focus", type=str, default=None,
                        help="Тематический фокус выпуска (свободный текст для промптов)")
    parser.add_argument("--minutes", type=int, default=None,
                        help="Целевая длительность эпизода в минутах (иначе — от числа тем)")
    parser.add_argument("--digest-file", type=str, default=None,
                        help="Готовый дайджест (.md): пропустить сбор источников и этап 1, "
                             "items/sources взять из манифеста того же прогона")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== Подкаст 'Что нового в AI' — {today} (за {args.days} дней) ===")

    new_items: dict[str, str] = {}
    sources: list[str] = []
    digest = None
    if args.digest_file:
        digest_src = Path(args.digest_file)
        digest = digest_src.read_text(encoding="utf-8")
        man_path = EPISODES_DIR / f"{digest_src.stem}.items.json"
        if man_path.exists():
            man = json.loads(man_path.read_text(encoding="utf-8"))
            new_items = man.get("items", {})
            sources = man.get("sources", [])
        else:
            print(f"[digest-file] ВНИМАНИЕ: манифест {man_path} не найден — items/sources пустые")
        print(f"[digest-file] Дайджест из {digest_src}: {len(digest)} символов, "
              f"items: {len(new_items)}, sources: {len(sources)}")
    else:
        cfg = load_sources()
        channels = [
            (c["handle"], c.get("category", ""))
            for c in cfg.get("youtube", {}).get("channels", [])
            if c.get("enabled", True)
        ]

        covered = load_covered()
        old_mentions: list[str] = []
        if covered:
            print(f"[covered] В базе освещённого: {len(covered)} материалов")

        # 1. Fetch YouTube transcripts
        yt_context, yt_sources = fetch_youtube_context(
            args.days, channels, covered, new_items, old_mentions
        )

        # 2. Fetch web sources
        web_context, web_sources = fetch_web_context(
            args.days, cfg.get("hackernews", {}), cfg.get("huggingface_papers", {}),
            covered, new_items, old_mentions,
        )

        # 2b. Fetch RSS feeds (Israeli/Chinese tech press, newsletters)
        rss_context, rss_sources = fetch_rss_context(
            args.days, cfg.get("rss", {}), covered, new_items, old_mentions
        )

        # 2c. Fetch Telegram channels (user session; includes private subscriptions)
        tg_context, tg_sources = fetch_telegram_context(
            args.days, cfg.get("telegram", {}), covered, new_items, old_mentions
        )

        # 3. Build combined context
        context_parts = list(filter(None, [yt_context, web_context, rss_context, tg_context]))
        if not context_parts:
            print("ОШИБКА: нет материалов для подкаста")
            sys.exit(1)

        if old_mentions:
            context_parts.append(
                "## Уже освещалось в прошлых выпусках\n"
                "(заново не разбирать; можно кратко сослаться, если связано с новой темой)\n"
                + "\n".join(f"- {m}" for m in old_mentions)
            )
            print(f"[covered] Пропущено как уже освещённое: {len(old_mentions)}")

        context = "\n\n".join(context_parts)
        sources = yt_sources + web_sources + rss_sources + tg_sources
        print(f"[context] Всего символов: {len(context)}, источников: {len(sources)}, новых материалов: {len(new_items)}")

    # Output stem: never overwrite same-day artifacts (published episode/digest
    # already reference them, and Cloudflare caches mp3 for 24h)
    stem = today
    if (EPISODES_DIR / f"{today}.mp3").exists() or (DIGESTS_DIR / f"{today}.md").exists():
        stem = f"{today}-{datetime.now().strftime('%H%M')}"
        print(f"[out] Артефакты за {today} уже есть → пишу как {stem}.*")

    # 4. Stage 1: digest
    if digest is None:
        digest = generate_digest(context, today, focus=args.focus)
    if sources and "## Источники выпуска" not in digest:
        digest += "\n\n## Источники выпуска\n" + "\n".join(f"- {s}" for s in sources)

    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGESTS_DIR / f"{stem}.md"
    digest_path.write_text(digest, encoding="utf-8")
    print(f"[digest] Сохранён → {digest_path}")

    try:
        html_path = DIGESTS_DIR / f"{stem}.html"
        render_digest_html(digest, html_path, f"Что нового в AI — {today}")
        print(f"[digest] HTML → {html_path}")
    except Exception as e:
        print(f"[digest] HTML не сгенерирован: {e}")

    # 4b. English version of the digest
    digest_en_ok = False
    try:
        print("[LLM] Перевожу дайджест на английский...")
        digest_en = call_llm(
            TRANSLATE_DIGEST_PROMPT.format(digest=digest), temperature=0.3, max_tokens=8192
        )
        (DIGESTS_DIR / f"{stem}.en.md").write_text(digest_en, encoding="utf-8")
        render_digest_html(
            digest_en, DIGESTS_DIR / f"{stem}.en.html",
            f"What's new in AI — {today}", lang="en",
        )
        digest_en_ok = True
        print(f"[digest] EN → {DIGESTS_DIR / f'{stem}.en.html'}")
    except Exception as e:
        print(f"[digest] EN не сгенерирован: {e}")

    # 5. Stage 2: script from digest
    script = generate_script(digest, minutes=args.minutes, focus=args.focus)

    # Save script for debugging
    script_path = DATA_DIR / f"script_{stem}.txt"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    print(f"[script] Сохранён → {script_path}")

    if args.dry_run:
        print("[dry-run] Пропускаю TTS и RSS. Готово.")
        print(f"\nДайджест:\n{'='*60}")
        print(digest[:3000])
        print(f"\nСценарий:\n{'='*60}")
        print(script[:3000])
        if len(script) > 3000:
            print(f"\n... (ещё {len(script)-3000} символов, см. {script_path})")
        return

    # 6. Parse script
    lines = parse_script(script)
    if not lines:
        print(f"ОШИБКА: не удалось распарсить сценарий. Проверь {script_path}")
        sys.exit(1)
    print(f"[parse] {len(lines)} реплик")

    # 7. Build audio: Gemini multi-speaker, fallback edge-tts
    mp3_path = EPISODES_DIR / f"{stem}.mp3"
    if GEMINI_API_KEY:
        duration_sec = build_audio_gemini(lines, mp3_path)
        if duration_sec == 0:
            print("[audio] Gemini TTS не сработал — fallback на edge-tts")
            duration_sec = build_audio(lines, mp3_path)
    else:
        duration_sec = build_audio(lines, mp3_path)

    if not mp3_path.exists() or duration_sec == 0:
        print("ОШИБКА: MP3 не создан")
        sys.exit(1)

    # 8. Episode manifest (admin panel uses it for publish prefill + covered merge)
    sys.path.insert(0, str(Path(__file__).parent))
    import rss_manager
    ep_num = rss_manager.get_next_episode_number()
    title = f"Выпуск {ep_num} — {today}"
    digest_links = f"RU: {BASE_URL}/digests/{stem}.html"
    if digest_en_ok:
        digest_links += f"\nEN: {BASE_URL}/digests/{stem}.en.html"
    if len(sources) > 4:
        yt_n = sum(1 for s in sources if s.startswith("YouTube "))
        tg_n = sum(1 for s in sources if s.startswith("Telegram "))
        rest = [s for s in sources if not s.startswith(("YouTube ", "Telegram "))]
        summary = []
        if yt_n:
            summary.append(f"YouTube ({yt_n} каналов)")
        summary += [re.sub(r"\s*\(.*\)$", "", s) for s in rest[:4]]
        if tg_n:
            summary.append(f"Telegram ({tg_n} каналов)")
        sources_line = ", ".join(summary) + " и другие — полный список в текстовом дайджесте"
    else:
        sources_line = ", ".join(sources) if sources else "YouTube, HackerNews, HuggingFace Papers"
    description = (
        f"Еженедельный обзор AI-новостей — неделя от {today}: ключевые события "
        f"и что они значат на практике.\n"
        f"\n"
        f"Текстовый дайджест выпуска:\n"
        f"{digest_links}\n"
        f"\n"
        f"Источники: {sources_line}.\n"
        f"\n"
        f"Выпуск полностью сгенерирован AI: дайджест, сценарий и озвучка."
    )
    manifest = {
        "date": today,
        "title": title,
        "description": description,
        "duration": duration_sec,
        "mp3": mp3_path.name,
        "sources": sources,
        "items": new_items,
    }
    manifest_path = EPISODES_DIR / f"{mp3_path.stem}.items.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[manifest] → {manifest_path}")

    # 9. Publish to RSS (unless deferred to the admin panel)
    if args.no_publish:
        print("[no-publish] В RSS не публикую — послушай и опубликуй из админки")
    else:
        try:
            rss_manager.add_episode(
                title=title,
                description=description,
                mp3_path=mp3_path,
                duration_seconds=duration_sec,
                episode_number=ep_num,
            )
            mark_covered(new_items, today)
        except Exception as e:
            print(f"[RSS] ошибка обновления: {e}")
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).parent / "site" / "build_site.py")],
                capture_output=True, timeout=120, check=True,
            )
            print("[site] сайт пересобран")
        except Exception as e:
            print(f"[site] пересборка не удалась: {e}")

    print(f"\n=== Готово ===")
    print(f"MP3:     {mp3_path}")
    print(f"URL:     {BASE_URL}/episodes/{mp3_path.name}")
    print(f"Дайджест: {BASE_URL}/digests/{stem}.html")
    print(f"RSS:     {BASE_URL}/feed.xml")
    duration_min = duration_sec // 60
    print(f"Длительность: ~{duration_min} мин")


if __name__ == "__main__":
    main()
