#!/usr/bin/env python3
"""
app.py — Minimal admin panel for the whntpdcst podcast.

Runs in the podcast-admin container (docker/docker-compose.yml), LAN-only
on port 8086. HTTP Basic auth: ADMIN_USER (default "admin") / ADMIN_PASSWORD.

Features:
    - list published episodes, unpublish, edit title/description
    - upload a recording (any audio format → CBR 64k MP3) and publish
    - publish/delete existing files in episodes/
    - digest links
"""

import html
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from mutagen.mp3 import MP3

sys.path.insert(0, "/opt/data/skills/podcast")
import rss_manager as rm

SKILL_DIR = Path("/opt/data/skills/podcast")
EPISODES_DIR = Path("/opt/data/podcast/episodes")
DIGESTS_DIR = Path("/opt/data/podcast/digests")
ACCESS_LOG = Path("/opt/data/podcast/logs/episodes.log")
SOURCES_PATH = SKILL_DIR / "sources.yaml"
EXTRA_SOURCES_PATH = Path("/opt/data/podcast/sources.extra.yaml")
COVERED_PATH = Path("/opt/data/podcast/covered.json")
GEN_LOG = Path("/opt/data/podcast/logs/generate.log")
BLOG_DIR = Path("/opt/data/podcast/blog")
SITE_DIR = Path("/opt/data/podcast/site")
SITE_LOG = Path("/opt/data/podcast/logs/site.log")
ITUNES = "{" + rm.ITUNES_NS + "}"

# Single uvicorn worker → a module global is enough to track the one allowed run
_gen_proc: subprocess.Popen | None = None

# nginx "main" log format:
# $remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent
# "$http_referer" "$http_user_agent" "$http_x_forwarded_for"
LOG_LINE_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[[^\]]+\] '
    r'"(?:GET|HEAD) (?P<path>/episodes/[^" ?]+)[^"]*" '
    r'(?P<status>\d+) \d+ "[^"]*" "[^"]*" "(?P<xff>[^"]*)"'
)

security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    admin_pass = os.environ.get("ADMIN_PASSWORD", "")
    user_ok = secrets.compare_digest(creds.username, os.environ.get("ADMIN_USER", "admin"))
    pass_ok = admin_pass and secrets.compare_digest(creds.password, admin_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(dependencies=[Depends(auth)])


# ── Feed helpers ──────────────────────────────────────────────────────────────

def _save_feed(rss) -> None:
    rm.FEED_PATH.write_text(rm._prettify(rss), encoding="utf-8")


def _published() -> list[dict]:
    _, channel = rm._parse_feed()
    eps = []
    for it in channel.findall("item"):
        enc = it.find("enclosure")
        url = enc.get("url") if enc is not None else ""
        eps.append({
            "title": it.findtext("title") or "",
            "description": it.findtext("description") or "",
            "url": url,
            "file": url.split("/")[-1].split("?")[0],
            "date": (it.findtext("pubDate") or "")[:16],
            "duration": int(float(it.findtext(ITUNES + "duration") or 0)),
            "number": it.findtext(ITUNES + "episode") or "?",
        })
    return eps


def _episode_analytics() -> dict[str, dict]:
    """Parse the persisted nginx access log into per-file request/listener stats."""
    stats: dict[str, dict] = {}
    if not ACCESS_LOG.exists():
        return stats
    for line in ACCESS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LOG_LINE_RE.match(line)
        if not m or not m.group("status").startswith(("2", "3")):
            continue
        filename = m.group("path").rsplit("/", 1)[-1]
        ip = m.group("xff") or m.group("ip")
        s = stats.setdefault(filename, {"requests": 0, "ips": set()})
        s["requests"] += 1
        s["ips"].add(ip)
    return stats


def _find_item(channel, url: str):
    for it in channel.findall("item"):
        enc = it.find("enclosure")
        if enc is not None and enc.get("url") == url:
            return it
    return None


def _safe_episode_path(filename: str) -> Path:
    if not re.fullmatch(r"[\w][\w.\- ]*\.mp3", filename):
        raise HTTPException(400, f"Недопустимое имя файла: {filename}")
    return EPISODES_DIR / filename


def _mp3_duration(path: Path) -> int:
    try:
        return int(MP3(str(path)).info.length)
    except Exception:
        return 0


def _fmt_dur(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_size(num_bytes: int) -> str:
    return f"{num_bytes / 1_048_576:.1f} МБ"


def _manifest_for(mp3_path: Path) -> dict:
    """Load the generation manifest ({stem}.items.json) for an episode file."""
    mpath = mp3_path.with_name(mp3_path.stem + ".items.json")
    if mpath.exists():
        try:
            return json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _merge_covered(mp3_path: Path) -> int:
    """On publish: merge the episode manifest's items into covered.json."""
    manifest = _manifest_for(mp3_path)
    items = manifest.get("items", {})
    if not items:
        return 0
    covered = {}
    if COVERED_PATH.exists():
        try:
            covered = json.loads(COVERED_PATH.read_text(encoding="utf-8"))
        except Exception:
            covered = {}
    date = manifest.get("date", datetime.now().strftime("%Y-%m-%d"))
    for item_id, title in items.items():
        covered[item_id] = {"episode": date, "title": title}
    COVERED_PATH.write_text(json.dumps(covered, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(items)


def _rebuild_site() -> str:
    """Run the static site generator; return a short status string."""
    SITE_LOG.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(SKILL_DIR / "site" / "build_site.py")],
        capture_output=True, timeout=120, text=True,
    )
    SITE_LOG.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    return "сайт пересобран" if result.returncode == 0 else "ОШИБКА сборки сайта (см. лог)"


def _safe_post_path(slug: str) -> Path:
    if not re.fullmatch(r"[\w-]{1,80}", slug, flags=re.UNICODE):
        raise HTTPException(400, f"Недопустимое имя поста: {slug}")
    return BLOG_DIR / f"{slug}.md"


def _post_parts(raw: str) -> tuple[dict, str]:
    """Split a post into (frontmatter dict, markdown body)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
    if not m:
        return {}, raw
    try:
        return (yaml.safe_load(m.group(1)) or {}), m.group(2)
    except Exception:
        return {}, raw


def _site_episode_urls() -> set[str]:
    """Episode mp3 URLs present on the built podcast pages."""
    urls: set[str] = set()
    for f in [SITE_DIR / "podcast" / "index.html", SITE_DIR / "ru" / "podcast" / "index.html"]:
        if f.exists():
            urls |= set(re.findall(r'<audio[^>]+src="([^"]+)"', f.read_text(encoding="utf-8")))
    return urls


def _llm_translate(title: str, body: str, dst_lang: str) -> tuple[str, str]:
    """Translate a post via OpenRouter. Returns (title, body)."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise HTTPException(500, "OPENROUTER_API_KEY не задан")
    lang_name = {"en": "English", "ru": "Russian"}[dst_lang]
    prompt = (
        f"Translate this blog post into {lang_name}. Preserve the markdown structure exactly: "
        "keep code blocks, inline code and URLs unchanged, translate prose and link texts. "
        "Keep the author's tone (light, technical, first person). "
        "Reply with the translated title on the first line, then a line containing only ---, "
        f"then the translated markdown body. No commentary.\n\nTITLE: {title}\n\nBODY:\n{body}"
    )
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "google/gemini-2.5-flash", "temperature": 0.2,
              "max_tokens": 8192,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    lines = content.splitlines()
    t_title = re.sub(r"^TITLE:\s*", "", lines[0]).strip()
    try:
        sep = next(i for i, ln in enumerate(lines) if ln.strip() == "---")
    except StopIteration:
        raise HTTPException(502, "LLM вернул перевод в неожиданном формате")
    return t_title, "\n".join(lines[sep + 1:]).strip() + "\n"


def _gen_running() -> bool:
    return _gen_proc is not None and _gen_proc.poll() is None


def _gen_log_tail(n: int = 20) -> str:
    if not GEN_LOG.exists():
        return ""
    lines = GEN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


# ── Views ─────────────────────────────────────────────────────────────────────

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>Админка — Что нового в AI</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 52rem; margin: 0 auto; padding: 1.5rem; line-height: 1.5;
         color: #1a1a2e; background: #fdfdfb; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: .3rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
  td, th {{ padding: .4rem .6rem; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }}
  input[type=text], textarea {{ width: 100%; box-sizing: border-box; padding: .3rem;
         border: 1px solid #ccc; border-radius: 4px; font: inherit; }}
  button {{ padding: .3rem .8rem; border: 1px solid #888; border-radius: 4px;
         background: #f5f5f5; cursor: pointer; font: inherit; }}
  button.danger {{ border-color: #c0392b; color: #c0392b; }}
  form.inline {{ display: inline; }}
  details {{ margin: .3rem 0; }}
  .muted {{ color: #888; font-size: .85rem; }}
  pre.log {{ background: #f0f0ec; padding: .6rem; border-radius: 6px; font-size: .78rem;
        overflow-x: auto; white-space: pre-wrap; }}
  audio {{ width: 100%; max-width: 24rem; display: block; margin-top: .3rem; }}
  textarea.yaml {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e8e8e8; background: #16161e; }}
    h2 {{ border-color: #333; }}
    td, th {{ border-color: #2a2a35; }}
    input[type=text], textarea, button {{ background: #22222e; color: #e8e8e8; border-color: #444; }}
    pre.log {{ background: #1e1e28; }}
  }}
</style>
</head>
<body>
<h1>Что нового в AI — админка</h1>
<p class="muted">Фид: <a href="{base_url}/feed.xml">{base_url}/feed.xml</a> ·
Artist: {author} · метаданные канала правятся в rss_manager.py</p>
{flash}
<h2>Опубликованные эпизоды</h2>
<p class="muted">Запросов/IP — из логов nginx, приблизительно (плееры качают эпизод по частям,
одно прослушивание может дать несколько запросов)</p>
{episodes}
<h2>Файлы вне фида</h2>
{files}
<h2>Генерация выпуска</h2>
{generation}
<h2>Источники</h2>
{sources}
<h2>Блог и сайт</h2>
{blog}
<h2>Загрузить запись</h2>
<form method="post" action="/upload" enctype="multipart/form-data">
<table>
<tr><td>Аудиофайл</td><td><input type="file" name="file" accept="audio/*" required>
    <span class="muted">любой формат — будет перекодирован в CBR 64k MP3</span></td></tr>
<tr><td>Название</td><td><input type="text" name="title" value="{next_title}" required></td></tr>
<tr><td>Описание</td><td><textarea name="description" rows="3" required></textarea></td></tr>
<tr><td></td><td><label><input type="checkbox" name="publish" checked> опубликовать сразу</label>
    <button type="submit">Загрузить</button></td></tr>
</table>
</form>
<h2>Дайджесты</h2>
{digests}
</body>
</html>
"""


def _render(flash: str = "") -> str:
    eps = _published()
    published_files = {e["file"] for e in eps}
    analytics = _episode_analytics()
    site_urls = _site_episode_urls()

    rows = []
    for e in eps:
        t, d, u = html.escape(e["title"]), html.escape(e["description"]), html.escape(e["url"])
        a = analytics.get(e["file"], {"requests": 0, "ips": set()})
        on_site = "✅" if e["url"] in site_urls else "⚠️ нет"
        rows.append(f"""<tr>
<td>{e['number']}</td>
<td><a href="{u}">{t}</a><br>
<details><summary class="muted">описание / правка</summary>
<form method="post" action="/edit">
<input type="hidden" name="url" value="{u}">
<input type="text" name="title" value="{t}">
<textarea name="description" rows="4">{d}</textarea>
<button type="submit">Сохранить</button>
</form></details></td>
<td>{e['date']}</td>
<td>{_fmt_dur(e['duration'])}</td>
<td>{a['requests']}</td>
<td>{len(a['ips'])}</td>
<td>{on_site}</td>
<td><form class="inline" method="post" action="/unpublish"
     onsubmit="return confirm('Снять «{t}» с публикации? Файл останется.')">
<input type="hidden" name="url" value="{u}">
<button class="danger" type="submit">Снять</button></form></td>
</tr>""")
    episodes_html = (
        "<table><tr><th>№</th><th>Эпизод</th><th>Дата</th><th>Длит.</th>"
        "<th>Запросов</th><th>IP</th><th>Сайт</th><th></th></tr>"
        + "".join(rows) + "</table>" if rows else "<p class='muted'>Фид пуст.</p>"
    )

    file_rows = []
    for p in sorted(EPISODES_DIR.glob("*.mp3"), reverse=True):
        if p.name in published_files:
            continue
        n = html.escape(p.name)
        manifest = _manifest_for(p)
        title_val = html.escape(manifest.get("title", ""))
        desc_val = html.escape(manifest.get("description", ""))
        file_rows.append(f"""<tr>
<td>{n}
<audio controls preload="none" src="/audio/{n}"></audio></td>
<td>{_fmt_size(p.stat().st_size)}</td><td>{_fmt_dur(_mp3_duration(p))}</td>
<td>
<details><summary>Опубликовать</summary>
<form method="post" action="/publish">
<input type="hidden" name="filename" value="{n}">
<input type="text" name="title" placeholder="Название" value="{title_val}" required>
<textarea name="description" rows="3" placeholder="Описание" required>{desc_val}</textarea>
<button type="submit">В фид</button>
</form></details>
<form class="inline" method="post" action="/delete-file"
      onsubmit="return confirm('Удалить файл {n} безвозвратно?')">
<input type="hidden" name="filename" value="{n}">
<button class="danger" type="submit">Удалить</button></form>
</td></tr>""")
    files_html = (
        "<table><tr><th>Файл</th><th>Размер</th><th>Длит.</th><th></th></tr>"
        + "".join(file_rows) + "</table>"
        if file_rows else "<p class='muted'>Все файлы опубликованы.</p>"
    )

    # Generation section
    running = _gen_running()
    tail = html.escape(_gen_log_tail())
    if running:
        generation_html = (
            "<p>⏳ Идёт генерация — страница обновляется каждые 5 секунд.</p>"
            f"<pre class='log'>{tail}</pre>"
        )
    else:
        status = ""
        if _gen_proc is not None:
            rc = _gen_proc.returncode
            status = ("<p>✅ Последняя генерация завершилась — файл в «Файлы вне фида», "
                      "послушай и опубликуй.</p>" if rc == 0
                      else f"<p>❌ Последняя генерация упала (код {rc}) — смотри лог.</p>")
        log_details = (f"<details><summary class='muted'>лог последней генерации</summary>"
                       f"<pre class='log'>{tail}</pre></details>") if tail else ""
        generation_html = f"""{status}
<form method="post" action="/generate">
Материалы за <input type="text" name="days" value="7" size="3" style="width:3rem"> дней
<button type="submit">Сгенерировать выпуск</button>
<span class="muted">дайджест + сценарий + озвучка, ~5-10 минут; без публикации в фид</span>
</form>
{log_details}"""

    # Sources section
    default_yaml = html.escape(
        SOURCES_PATH.read_text(encoding="utf-8") if SOURCES_PATH.exists()
        else "# sources.yaml не найден"
    )
    extra_yaml = html.escape(
        EXTRA_SOURCES_PATH.read_text(encoding="utf-8") if EXTRA_SOURCES_PATH.exists() else ""
    )
    covered_count = 0
    if COVERED_PATH.exists():
        try:
            covered_count = len(json.loads(COVERED_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    sources_html = f"""<details><summary>Дефолтные (sources.yaml из репо, {default_yaml.count("handle:")} каналов) —
развернуть и скопировать</summary>
<textarea class="yaml" rows="14" readonly onclick="this.select()">{default_yaml}</textarea>
<p class="muted">Правится в репо: sources.yaml → git push (+ ручной деплой)</p></details>
<form method="post" action="/sources-extra">
<p>Дополнительные источники (поверх дефолтных, живут на сервере, репо не трогают):</p>
<textarea class="yaml" name="content" rows="8" placeholder="youtube:
  channels:
    - {{handle: &quot;@SomeChannel&quot;, category: &quot;AI&quot;}}
hackernews:
  queries: [&quot;extra query&quot;]">{extra_yaml}</textarea>
<button type="submit">Сохранить</button>
<span class="muted">пустое поле = убрать дополнительные</span>
</form>
<p class="muted">В базе освещённого: {covered_count} материалов (covered.json) —
уже попавшее в опубликованные выпуски в новых только упоминается одной фразой.</p>"""

    # Blog & site section
    post_blocks = []
    if BLOG_DIR.exists():
        for p in sorted(BLOG_DIR.glob("*.md"), reverse=True):
            slug = html.escape(p.stem)
            raw = p.read_text(encoding="utf-8")
            content = html.escape(raw)
            meta, _ = _post_parts(raw)
            lang = str(meta.get("lang", "ru")).lower()
            pair = str(meta.get("pair", ""))
            dst = "RU" if lang == "en" else "EN"
            translate_html = (
                f'<span class="muted">перевод: {html.escape(pair)}.md</span>' if pair else
                f"""<form class="inline" method="post" action="/blog-translate">
<input type="hidden" name="slug" value="{slug}">
<button type="submit">Перевести на {dst}</button></form>"""
            )
            post_blocks.append(f"""<details><summary class="mono">{slug}.md [{lang}]
<a href="{rm.BASE_URL}/blog/{slug}.html">→ на сайте</a></summary>
<form method="post" action="/blog-save">
<input type="hidden" name="slug" value="{slug}">
<textarea class="yaml" name="content" rows="14">{content}</textarea>
<button type="submit">Сохранить и пересобрать</button>
</form>
{translate_html}
<form class="inline" method="post" action="/blog-delete"
      onsubmit="return confirm('Удалить пост {slug}?')">
<input type="hidden" name="slug" value="{slug}">
<button class="danger" type="submit">Удалить</button></form>
</details>""")
    today_str = datetime.now().strftime("%Y-%m-%d")
    new_post_tpl = html.escape(
        f"---\ntitle: Заголовок\ndate: {today_str}\nlang: ru\ndraft: false\n---\n\nТекст поста в markdown.\n"
    )
    site_tail = html.escape(
        SITE_LOG.read_text(encoding="utf-8", errors="replace")[-800:]
    ) if SITE_LOG.exists() else ""
    blog_html = f"""<p class="muted">Сайт: <a href="{rm.BASE_URL}/">{rm.BASE_URL}</a> ·
посты в /opt/data/podcast/blog/, сайт пересобирается при сохранении и публикации эпизодов</p>
<form class="inline" method="post" action="/site-rebuild"><button type="submit">Пересобрать сайт</button></form>
{"".join(post_blocks) if post_blocks else "<p class='muted'>Постов пока нет.</p>"}
<details><summary>Новый пост</summary>
<form method="post" action="/blog-save">
<p>Имя файла (slug, латиница/цифры/дефис): <input type="text" name="slug" placeholder="my-first-post" required></p>
<textarea class="yaml" name="content" rows="12">{new_post_tpl}</textarea>
<button type="submit">Создать и пересобрать</button>
</form></details>
{f'<details><summary class="muted">лог сборки</summary><pre class="log">{site_tail}</pre></details>' if site_tail else ''}"""

    digest_links = [
        f'<li>{p.stem}: <a href="{rm.BASE_URL}/digests/{p.stem}.html">HTML</a> · '
        f'<a href="{rm.BASE_URL}/digests/{p.stem}.md">MD</a></li>'
        for p in sorted(DIGESTS_DIR.glob("*.md"), reverse=True)
    ] if DIGESTS_DIR.exists() else []
    digests_html = f"<ul>{''.join(digest_links)}</ul>" if digest_links else "<p class='muted'>Пока нет.</p>"

    next_num = rm.get_next_episode_number()
    today = datetime.now().strftime("%Y-%m-%d")
    return PAGE.format(
        refresh='<meta http-equiv="refresh" content="5">' if running else "",
        base_url=rm.BASE_URL,
        author=html.escape(rm.FEED_META["author"]),
        flash=f"<p><b>{html.escape(flash)}</b></p>" if flash else "",
        episodes=episodes_html,
        files=files_html,
        generation=generation_html,
        sources=sources_html,
        blog=blog_html,
        next_title=f"Выпуск {next_num} — {today}",
        digests=digests_html,
    )


@app.get("/", response_class=HTMLResponse)
def index(msg: str = ""):
    return _render(msg)


def _redirect(msg: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)


# ── Actions ───────────────────────────────────────────────────────────────────

@app.post("/unpublish")
def unpublish(url: str = Form(...)):
    rss, channel = rm._parse_feed()
    item = _find_item(channel, url)
    if item is None:
        raise HTTPException(404, "Эпизод не найден в фиде")
    title = item.findtext("title") or url
    channel.remove(item)
    _save_feed(rss)
    return _redirect(f"Снят с публикации: {title}, {_rebuild_site()}")


@app.post("/edit")
def edit(url: str = Form(...), title: str = Form(...), description: str = Form(...)):
    rss, channel = rm._parse_feed()
    item = _find_item(channel, url)
    if item is None:
        raise HTTPException(404, "Эпизод не найден в фиде")
    for tag, value in [
        ("title", title), ("description", description),
        (ITUNES + "title", title), (ITUNES + "summary", description),
    ]:
        el = item.find(tag)
        if el is not None:
            el.text = value
    _save_feed(rss)
    return _redirect(f"Обновлено: {title}, {_rebuild_site()}")


@app.post("/publish")
def publish(filename: str = Form(...), title: str = Form(...), description: str = Form(...)):
    path = _safe_episode_path(filename)
    if not path.exists():
        raise HTTPException(404, f"Файл не найден: {filename}")
    rm.add_episode(
        title=title,
        description=description,
        mp3_path=path,
        duration_seconds=_mp3_duration(path),
        episode_number=rm.get_next_episode_number(),
    )
    n_covered = _merge_covered(path)
    extra = f" (+{n_covered} материалов в базу освещённого)" if n_covered else ""
    return _redirect(f"Опубликован: {title}{extra}, {_rebuild_site()}")


@app.get("/audio/{filename}")
def audio(filename: str):
    path = _safe_episode_path(filename)
    if not path.exists():
        raise HTTPException(404, f"Файл не найден: {filename}")
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/generate")
def generate(days: str = Form("7")):
    global _gen_proc
    if _gen_running():
        return _redirect("Генерация уже идёт")
    try:
        days_n = max(1, min(int(days), 31))
    except ValueError:
        return _redirect(f"Некорректное число дней: {days}")
    GEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with GEN_LOG.open("w", encoding="utf-8") as log_f:
        _gen_proc = subprocess.Popen(
            [sys.executable, "-u", str(SKILL_DIR / "podcast_skill.py"),
             "--days", str(days_n), "--no-publish"],
            stdout=log_f, stderr=subprocess.STDOUT, cwd=str(SKILL_DIR),
        )
    return _redirect(f"Генерация запущена (материалы за {days_n} дней)")


@app.post("/blog-save")
def blog_save(slug: str = Form(...), content: str = Form(...)):
    path = _safe_post_path(slug.strip())
    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")
    return _redirect(f"Пост {path.name} сохранён, {_rebuild_site()}")


@app.post("/blog-delete")
def blog_delete(slug: str = Form(...)):
    path = _safe_post_path(slug.strip())
    if not path.exists():
        raise HTTPException(404, f"Пост не найден: {slug}")
    path.unlink()
    return _redirect(f"Пост {slug} удалён, {_rebuild_site()}")


@app.post("/site-rebuild")
def site_rebuild():
    return _redirect(_rebuild_site().capitalize())


@app.post("/blog-translate")
def blog_translate(slug: str = Form(...)):
    src_path = _safe_post_path(slug.strip())
    if not src_path.exists():
        raise HTTPException(404, f"Пост не найден: {slug}")
    meta, body = _post_parts(src_path.read_text(encoding="utf-8"))
    if not meta:
        return _redirect(f"У поста {slug} нет frontmatter — не перевожу")
    src_lang = str(meta.get("lang", "ru")).lower()
    dst_lang = "en" if src_lang != "en" else "ru"
    pair_slug = str(meta.get("pair", ""))
    if pair_slug and (BLOG_DIR / f"{pair_slug}.md").exists():
        return _redirect(f"Перевод уже есть: {pair_slug}.md — удали его, если нужен свежий")
    dst_slug = re.sub(r"-(ru|en)$", "", slug) + f"-{dst_lang}"

    t_title, t_body = _llm_translate(str(meta.get("title", slug)), body, dst_lang)

    dst_meta = {"title": t_title, "date": meta.get("date", ""), "lang": dst_lang, "pair": slug}
    fm = yaml.safe_dump(dst_meta, allow_unicode=True, sort_keys=False).strip()
    (BLOG_DIR / f"{dst_slug}.md").write_text(f"---\n{fm}\n---\n\n{t_body}", encoding="utf-8")

    meta["pair"] = dst_slug
    src_fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    src_path.write_text(f"---\n{src_fm}\n---\n\n{body.lstrip()}", encoding="utf-8")

    return _redirect(f"Переведено: {dst_slug}.md ({dst_lang}), {_rebuild_site()}")


@app.post("/sources-extra")
def sources_extra(content: str = Form("")):
    import yaml
    if not content.strip():
        EXTRA_SOURCES_PATH.unlink(missing_ok=True)
        return _redirect("Дополнительные источники убраны")
    try:
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError("ожидается YAML-словарь (youtube:/hackernews:)")
    except Exception as e:
        return _redirect(f"НЕ сохранено, ошибка YAML: {e}")
    EXTRA_SOURCES_PATH.write_text(content, encoding="utf-8")
    return _redirect("Дополнительные источники сохранены")


@app.post("/upload")
async def upload(
    file: UploadFile,
    title: str = Form(...),
    description: str = Form(...),
    publish: str = Form(""),
):
    slug = re.sub(r"[^\w\-]+", "-", Path(file.filename or "live").stem, flags=re.ASCII).strip("-") or "live"
    out_path = EPISODES_DIR / f"{datetime.now().strftime('%Y-%m-%d')}-{slug}.mp3"
    if out_path.exists():
        out_path = out_path.with_stem(out_path.stem + datetime.now().strftime("-%H%M%S"))

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "a").suffix or ".bin", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)

    # Re-encode to the same CBR format as generated episodes (correct seeking)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(tmp_path),
         "-acodec", "libmp3lame", "-b:a", "64k", "-ar", "22050", "-ac", "1",
         str(out_path)],
        capture_output=True, timeout=600,
    )
    tmp_path.unlink(missing_ok=True)
    if result.returncode != 0 or not out_path.exists():
        raise HTTPException(400, f"ffmpeg не смог перекодировать: {result.stderr.decode()[-300:]}")

    if publish:
        rm.add_episode(
            title=title,
            description=description,
            mp3_path=out_path,
            duration_seconds=_mp3_duration(out_path),
            episode_number=rm.get_next_episode_number(),
        )
        return _redirect(f"Загружен и опубликован: {title}")
    return _redirect(f"Загружен (не опубликован): {out_path.name}")


@app.post("/delete-file")
def delete_file(filename: str = Form(...)):
    path = _safe_episode_path(filename)
    if not path.exists():
        raise HTTPException(404, f"Файл не найден: {filename}")
    if filename in {e["file"] for e in _published()}:
        raise HTTPException(400, "Файл опубликован — сначала сними с публикации")
    path.unlink()
    path.with_name(path.stem + ".items.json").unlink(missing_ok=True)
    return _redirect(f"Удалён: {filename}")
