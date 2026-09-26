# whntpdcst — AI Podcast Generator

Автоматический русскоязычный подкаст «Что нового в AI».

Каждый эпизод: YouTube, HackerNews, HuggingFace Papers, RSS, Telegram → аналитический дайджест с памятью о прошлых выпусках → диалог Алекса и Саши → MP3 → Apple Podcasts.

**Слушать:** [whntpdcst.com/feed.xml](https://whntpdcst.com/feed.xml)

## Как работает

```
YouTube, HN, HF Papers, ──┐                       прошлые выпуски
RSS, Telegram             │                       (блокнот сюжетов + 3 дайджеста)
                          ▼                                │
              аналитический дайджест ◄─────────────────────┘
                          │  humanizer
                          ▼
              план редактора → диалог → humanizer → Gemini TTS → MP3 → RSS
```

1. Собирает транскрипты YouTube и топ материалы недели (источники — в `sources.yaml`)
2. Подгружает контекст прошлых **опубликованных** выпусков: блокнот сквозных сюжетов
   (`memory/<stem>.md` — что отслеживаем, какие прогнозы давали) и три последних дайджеста
3. LLM пишет аналитический дайджест: отделяет прорывы от инкремента и маркетинга, связывает
   новости с прошлыми выпусками, разбирает последствия для отрасли и для обычных людей →
   [whntpdcst.com/digests/](https://whntpdcst.com/digests/) (MD + HTML, RU/EN)
4. Блокнот сюжетов обновляется по новому дайджесту
5. План редактора: тезис выпуска, порядок тем, где ведущие расходятся во мнениях
6. Диалог двух ведущих: Алекс — инженер-скептик, Саша — аналитик технологий и общества.
   По умолчанию ~18 минут (`--minutes` / `PODCAST_MINUTES`): каждая тема дайджеста пишется
   отдельным вызовом, и в него подаются выдержки из первоисточников темы (транскрипты,
   статьи, посты по ссылкам из дайджеста), чтобы в разговоре были детали, а не пересказ
   дайджеста. Сырые материалы сохраняются в `context_<stem>.txt` для `--digest-file`
7. Дайджест и сценарий проходят через [humanizer](https://github.com/blader/humanizer)
   (`prompts/humanizer/SKILL.md`, MIT) + русское дополнение для диалога
   (`prompts/humanizer/ru.md`) — вычищаются генеративные обороты. Блок, который после
   правки сломал формат или потерял текст, остаётся исходным
8. Gemini multi-speaker TTS озвучивает: Алекс (`Charon`) + Саша (`Leda`); fallback — edge-tts
9. ffmpeg кодирует в CBR 64k MP3, RSS обновляется → Apple Podcasts подхватывает

Модель для текста — `PODCAST_LLM_MODEL` (по умолчанию `anthropic/claude-sonnet-5` через
OpenRouter). Если OpenRouter не знает id или модель недоступна — автоматический откат на
`anthropic/claude-sonnet-4.5`, затем `google/gemini-2.5-flash`.

Отладочные файлы в `$PODCAST_DATA_DIR`: `plan_<stem>.md` (план), `script_<stem>.raw.txt`
(сценарий до humanizer), `script_<stem>.txt` (финальный), `context_<stem>.txt` (сырые материалы). `--no-humanize` отключает проход.

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
| `podcast_skill.py`, `prompts/*`, `rss_manager.py`, `site/*`, `sources.yaml` | ничего — файлы монтируются, каждый запуск читает свежие |
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
podcast_skill.py   # основной pipeline (промпты — в начале файла)
prompts/humanizer/ # humanizer-скилл (blader/humanizer, MIT) + русское дополнение
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
