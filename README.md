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

**GitHub Actions деплой НЕ работает и работать не будет**: carbon за NAT
домашнего провайдера без port forwarding, runner GitHub не достучится до SSH
(все прогоны workflow падают с `dial tcp :22: i/o timeout`). Workflow остался
декоративным. **Каждый пуш в main деплоится руками.**

### Стандартный деплой

```bash
# локально
git push origin main

# на carbon
ssh sokolmask@192.168.1.124
cd ~/hermes-data/skills/podcast
git pull origin main
```

Дальше — по тому, что менялось (репо смонтирован в контейнеры как
`/opt/data/skills/podcast`, поэтому многое подхватывается без пересборки):

| Что менялось | Что сделать после `git pull` |
|---|---|
| `podcast_skill.py`, `rss_manager.py`, `site/*`, `sources.yaml` | ничего — файлы монтируются, каждый запуск читает свежие |
| `admin/app.py` | `docker compose -f docker/docker-compose.yml restart podcast-admin` (uvicorn держит код в памяти) |
| `docker/nginx.conf` | `docker compose -f docker/docker-compose.yml restart podcast-static` |
| `docker/admin.Dockerfile` (зависимости админки) | `cd docker && docker compose up -d --build` |
| `docker/docker-compose.yml` | `cd docker && docker compose up -d` |
| `.env` (hermes-agent/.env) | `docker compose up -d --force-recreate podcast-admin` — простой restart env НЕ подтягивает |
| `requirements.txt` (для hermes) | `docker exec hermes uv pip install -r /opt/data/skills/podcast/requirements.txt` — pip-пакеты hermes НЕ переживают recreate контейнера |
| `cover.jpg` | `cp cover.jpg ~/hermes-data/podcast/cover.jpg` |
| контент сайта (лендинг/страницы) | пересобрать сайт (ниже) |

### Пересборка сайта

Сайт статический, собирается в `/opt/data/podcast/site/`:

```bash
docker exec podcast-admin python /opt/data/skills/podcast/site/build_site.py
```

или кнопка «Пересобрать сайт» в админке. Автоматически пересобирается при
publish/unpublish/правке эпизода и сохранении/удалении/переводе поста.

### Проверка после деплоя

```bash
docker ps --format '{{.Names}}: {{.Status}}' | grep podcast   # контейнеры живы
docker logs podcast-admin --tail 5                            # админка поднялась
curl -s -o /dev/null -w '%{http_code}' https://whntpdcst.com/feed.xml   # 200
```

Публичный трафик: Cloudflare Tunnel → nginx `podcast-static` (:8085), админка
через тот же туннель на `admin.whntpdcst.com` (+ `:8086` в LAN). Cloudflare
кэширует mp3 24ч (при замене эпизода в тот же день — менять URL на `?v=N`)
и страницы сайта 5 мин.

## Структура

```
podcast_skill.py   # основной pipeline
sources.yaml       # источники (YouTube каналы, HN, HF) — правь и пушь
rss_manager.py     # Apple Podcasts-совместимый RSS
admin/app.py       # админка (FastAPI)
docker/            # nginx static server (8085) + админка (8086)
setup-carbon.sh    # one-time setup на сервере
```

## Админка

`https://admin.whntpdcst.com` (через Cloudflare Tunnel) или `http://carbon:8086` в LAN.
HTTP Basic: `ADMIN_USER`/`ADMIN_PASSWORD` из env Hermes.

Умеет: снять эпизод с публикации / вернуть, править название и описание,
загрузить свою запись (любой аудиоформат → CBR 64k MP3) и опубликовать,
удалить файл, ссылки на дайджесты.
