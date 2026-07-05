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

# YouTube channels to track: (handle, category)
CHANNELS = [
    # AI/ML
    ("@NateBJones",         "AI Strategy"),
    ("@aiexplained",        "AI News"),
    ("@aiDotEngineer",      "AI Engineering"),
    ("@AndrejKarpathy",     "ML/AI Deep Dives"),
    ("@YannicKilcher",      "ML Research"),
    ("@googledeepmind",     "AI Research"),
    ("@anthropicai",        "AI Safety"),
    ("@OpenAI",             "AI"),
    ("@HuggingFace",        "Open Source ML"),
    ("@mlst",               "ML Research"),
    ("@TwoMinutePapers",    "ML Research"),
    ("@SamWitherspoon",     "AI Tools"),
    # Dev/Tools
    ("@GosuCoder",          "AI Agents"),
    ("@AIJasonZ",           "AI Products"),
    ("@NetworkChuck",       "IT/DevOps"),
    ("@ThePragmaticEngineer", "Tech Industry"),
    ("@ycombinator",        "Startups"),
]

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
- Английские названия продуктов, моделей и компаний оставляй латиницей как есть

Материалы этой недели:
{context}"""

SCRIPT_PROMPT = """Ты пишешь сценарий для подкаста «Что нового в AI» с двумя ведущими.

Ведущий АЛЕКС — мужчина, аналитичный, лаконичный, любит конкретные факты и цифры, задаёт острые вопросы.
Ведущий САША — женщина, тёплая, связывает темы, делает практические выводы, говорит живо и понятно.

Стиль: как NotebookLM Audio Overview — живой, естественный разговор, не лекция. Ведущие перебивают друг друга, \
уточняют, иногда удивляются. Без формальных переходов типа «теперь поговорим о...».

На основе дайджеста ниже напиши эпизод подкаста (~8-12 минут, примерно 1500-2000 слов диалога) на русском языке.

Правила:
- Охвати 5-8 самых интересных тем дайджеста
- По каждой теме: что произошло → почему важно → что это значит для людей/разработчиков
- Только живой разговор — никаких списков, никаких буллетов в репликах
- Начинай сразу с темы — без «добро пожаловать», без «сегодня мы поговорим»
- Заканчивай одним коротким выводом недели
- Формат строго (каждая реплика на новой строке, без пустых строк между репликами одного блока):

АЛЕКС: текст реплики
САША: текст реплики
АЛЕКС: текст реплики

Дайджест недели:
{digest}"""


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


def resolve_channel_id(client: httpx.Client, handle: str) -> str | None:
    """Resolve @handle to channel ID."""
    try:
        data = yt_get(client, "channels", part="id", forHandle=handle.lstrip("@"))
        items = data.get("items", [])
        return items[0]["id"] if items else None
    except Exception as e:
        print(f"  [YT] resolve {handle}: {e}")
        return None


def get_recent_videos(client: httpx.Client, channel_id: str, days_back: int, max_results: int = 3) -> list[dict]:
    """Get recent videos from a channel."""
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=days_back)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = yt_get(
            client, "search",
            part="id,snippet",
            channelId=channel_id,
            publishedAfter=published_after,
            order="date",
            maxResults=max_results,
            type="video",
        )
        return [
            {
                "id": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "published": item["snippet"]["publishedAt"][:10],
                "description": item["snippet"]["description"][:300],
            }
            for item in data.get("items", [])
        ]
    except Exception as e:
        print(f"  [YT] get videos for {channel_id}: {e}")
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


def fetch_youtube_context(days_back: int) -> tuple[str, list[str]]:
    """Fetch transcripts from all tracked channels.

    Returns (formatted context string, list of sources that had videos).
    """
    if not YOUTUBE_API_KEY:
        print("[YT] YOUTUBE_API_KEY not set — skipping YouTube")
        return "", []

    parts = []
    sources = []
    client = httpx.Client()

    for i, (handle, category) in enumerate(CHANNELS, 1):
        print(f"[YT {i}/{len(CHANNELS)}] {handle} ({category})", end="", flush=True)
        try:
            channel_id = resolve_channel_id(client, handle)
            if not channel_id:
                print(" — не найден")
                continue

            videos = get_recent_videos(client, channel_id, days_back, max_results=2)
            if not videos:
                print(" — нет видео")
                continue

            print(f" — {len(videos)} видео", end="", flush=True)

            channel_parts = [f"\n## YouTube: {handle} ({category})\n"]
            for v in videos:
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

def fetch_hn_ai(max_items: int = 10) -> str:
    """Fetch top AI stories from HackerNews RSS."""
    parts = []
    queries = ["AI LLM", "machine learning", "Claude GPT", "AI agent"]
    seen = set()

    try:
        client = httpx.Client()
        import feedparser  # type: ignore
        for query in queries:
            url = f"https://hnrss.org/newest?q={httpx.URL('').copy_with()}&points=30&count=8"
            url = f"https://hnrss.org/newest?q={query.replace(' ', '+')}&points=30&count=8"
            feed = feedparser.parse(url)
            for entry in feed.entries:
                link = entry.get("link", "")
                if link in seen:
                    continue
                seen.add(link)
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                score_m = re.search(r"Points:\s*(\d+)", summary)
                score = score_m.group(1) if score_m else "?"
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


def fetch_hf_papers(days_back: int = 7, max_items: int = 8) -> str:
    """Fetch recent papers from HuggingFace daily papers."""
    parts = []
    seen = set()

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
                abstract_tag = article.find("p")
                abstract = abstract_tag.get_text(strip=True)[:300] if abstract_tag else ""
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


def fetch_web_context(days_back: int) -> tuple[str, list[str]]:
    """Fetch HN + HF papers. Returns (combined context string, list of sources)."""
    print("[Web] HackerNews...", end="", flush=True)
    hn = fetch_hn_ai()
    print(f" {hn.count(chr(10))} строк")

    print("[Web] HuggingFace Papers...", end="", flush=True)
    hf = fetch_hf_papers(days_back)
    print(f" {hf.count(chr(10))} строк")

    sources = []
    if hn:
        sources.append("HackerNews")
    if hf:
        sources.append("HuggingFace Papers")
    return "\n\n".join(filter(None, [hn, hf])), sources


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


def generate_digest(context: str, date: str) -> str:
    """Stage 1: raw materials → structured Markdown digest."""
    # Truncate context to ~50000 chars
    if len(context) > 50000:
        context = context[:50000] + "\n\n[... материалы обрезаны ...]"

    print(f"[LLM] Генерирую дайджест ({len(context)} символов контекста)...")
    digest = call_llm(DIGEST_PROMPT.format(date=date, context=context), temperature=0.4)
    print(f"[LLM] Дайджест готов: {len(digest)} символов")
    return digest


def generate_script(digest: str) -> str:
    """Stage 2: digest → dialogue script."""
    print("[LLM] Генерирую сценарий из дайджеста...")
    script = call_llm(SCRIPT_PROMPT.format(digest=digest), temperature=0.8)
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
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== Подкаст 'Что нового в AI' — {today} (за {args.days} дней) ===")

    # 1. Fetch YouTube transcripts
    yt_context, yt_sources = fetch_youtube_context(args.days)

    # 2. Fetch web sources
    web_context, web_sources = fetch_web_context(args.days)

    # 3. Build combined context
    context_parts = list(filter(None, [yt_context, web_context]))
    if not context_parts:
        print("ОШИБКА: нет материалов для подкаста")
        sys.exit(1)

    context = "\n\n".join(context_parts)
    sources = yt_sources + web_sources
    print(f"[context] Всего символов: {len(context)}, источников: {len(sources)}")

    # 4. Stage 1: digest
    digest = generate_digest(context, today)
    if sources:
        digest += "\n\n## Источники выпуска\n" + "\n".join(f"- {s}" for s in sources)

    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGESTS_DIR / f"{today}.md"
    digest_path.write_text(digest, encoding="utf-8")
    print(f"[digest] Сохранён → {digest_path}")

    # 5. Stage 2: script from digest
    script = generate_script(digest)

    # Save script for debugging
    script_path = DATA_DIR / f"script_{today}.txt"
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
    mp3_path = EPISODES_DIR / f"{today}.mp3"
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

    # 8. Update RSS feed
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import rss_manager
        ep_num = rss_manager.get_next_episode_number()
        title = f"Выпуск {ep_num} — {today}"
        description = (
            f"Еженедельный обзор AI новостей за неделю от {today}. "
            f"Алекс и Саша обсуждают ключевые события в мире искусственного интеллекта. "
            f"Выпуск полностью сгенерирован AI: дайджест, сценарий и голоса. "
            f"Текстовый дайджест выпуска: {BASE_URL}/digests/{today}.md. "
            f"Источники: {', '.join(sources) if sources else 'YouTube, HackerNews, HuggingFace Papers'}."
        )
        rss_manager.add_episode(
            title=title,
            description=description,
            mp3_path=mp3_path,
            duration_seconds=duration_sec,
            episode_number=ep_num,
        )
    except Exception as e:
        print(f"[RSS] ошибка обновления: {e}")

    print(f"\n=== Готово ===")
    print(f"MP3:     {mp3_path}")
    print(f"URL:     {BASE_URL}/episodes/{today}.mp3")
    print(f"Дайджест: {BASE_URL}/digests/{today}.md")
    print(f"RSS:     {BASE_URL}/feed.xml")
    duration_min = duration_sec // 60
    print(f"Длительность: ~{duration_min} мин")


if __name__ == "__main__":
    main()
