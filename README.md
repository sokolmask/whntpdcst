# whntpdcst — AI Podcast Generator

Автоматический русскоязычный подкаст «Что нового в AI».

Каждый эпизод: YouTube каналы + HackerNews + HuggingFace Papers → диалог Алекса и Саши → MP3 → Apple Podcasts.

**Слушать:** [whntpdcst.com/feed.xml](https://whntpdcst.com/feed.xml)

## Как работает

```
YouTube (17 каналов) ──┐
HackerNews AI ─────────┤──► Gemini 2.5 Flash ──► edge-tts ──► CBR MP3 ──► RSS
HuggingFace Papers ────┘       (диалог ~10 мин)    Алекс + Саша     Apple Podcasts
```

1. Собирает транскрипты YouTube и топ материалы недели (источники — в `sources.yaml`)
2. Gemini пишет структурированный дайджест → [whntpdcst.com/digests/](https://whntpdcst.com/digests/) (MD + HTML)
3. Из дайджеста генерируется живой диалог двух ведущих (~10 мин)
4. Gemini multi-speaker TTS озвучивает: Алекс (`Charon`) + Саша (`Leda`); fallback — edge-tts
5. ffmpeg кодирует в CBR 64k MP3
6. RSS обновляется → Apple Podcasts подхватывает автоматически

## Запуск

```bash
# Установить зависимости
pip install -r requirements.txt

# Сгенерировать эпизод (нужны YOUTUBE_API_KEY и OPENROUTER_API_KEY)
python podcast_skill.py --days 7

# Только сценарий, без TTS
python podcast_skill.py --days 7 --dry-run
```

## Переменные окружения

```
YOUTUBE_API_KEY=...
OPENROUTER_API_KEY=...
```

## Деплой (carbon homelab)

Первый раз:
```bash
./setup-carbon.sh
```

После этого — `git push` деплоит автоматически через GitHub Actions.

Подкаст раздаётся через nginx + Cloudflare Tunnel (без port forwarding на роутере).

## Структура

```
podcast_skill.py   # основной pipeline
sources.yaml       # источники (YouTube каналы, HN, HF) — правь и пушь
rss_manager.py     # Apple Podcasts-совместимый RSS
docker/            # nginx static server (порт 8085)
setup-carbon.sh    # one-time setup на сервере
```
