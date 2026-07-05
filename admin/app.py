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
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from mutagen.mp3 import MP3

sys.path.insert(0, "/opt/data/skills/podcast")
import rss_manager as rm

EPISODES_DIR = Path("/opt/data/podcast/episodes")
DIGESTS_DIR = Path("/opt/data/podcast/digests")
ITUNES = "{" + rm.ITUNES_NS + "}"

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


# ── Views ─────────────────────────────────────────────────────────────────────

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e8e8e8; background: #16161e; }}
    h2 {{ border-color: #333; }}
    td, th {{ border-color: #2a2a35; }}
    input[type=text], textarea, button {{ background: #22222e; color: #e8e8e8; border-color: #444; }}
  }}
</style>
</head>
<body>
<h1>Что нового в AI — админка</h1>
<p class="muted">Фид: <a href="{base_url}/feed.xml">{base_url}/feed.xml</a> ·
Artist: {author} · метаданные канала правятся в rss_manager.py</p>
{flash}
<h2>Опубликованные эпизоды</h2>
{episodes}
<h2>Файлы вне фида</h2>
{files}
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

    rows = []
    for e in eps:
        t, d, u = html.escape(e["title"]), html.escape(e["description"]), html.escape(e["url"])
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
<td><form class="inline" method="post" action="/unpublish"
     onsubmit="return confirm('Снять «{t}» с публикации? Файл останется.')">
<input type="hidden" name="url" value="{u}">
<button class="danger" type="submit">Снять</button></form></td>
</tr>""")
    episodes_html = (
        "<table><tr><th>№</th><th>Эпизод</th><th>Дата</th><th>Длит.</th><th></th></tr>"
        + "".join(rows) + "</table>" if rows else "<p class='muted'>Фид пуст.</p>"
    )

    file_rows = []
    for p in sorted(EPISODES_DIR.glob("*.mp3"), reverse=True):
        if p.name in published_files:
            continue
        n = html.escape(p.name)
        file_rows.append(f"""<tr>
<td>{n}</td><td>{_fmt_size(p.stat().st_size)}</td><td>{_fmt_dur(_mp3_duration(p))}</td>
<td>
<details><summary>Опубликовать</summary>
<form method="post" action="/publish">
<input type="hidden" name="filename" value="{n}">
<input type="text" name="title" placeholder="Название" required>
<textarea name="description" rows="3" placeholder="Описание" required></textarea>
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

    digest_links = [
        f'<li>{p.stem}: <a href="{rm.BASE_URL}/digests/{p.stem}.html">HTML</a> · '
        f'<a href="{rm.BASE_URL}/digests/{p.stem}.md">MD</a></li>'
        for p in sorted(DIGESTS_DIR.glob("*.md"), reverse=True)
    ] if DIGESTS_DIR.exists() else []
    digests_html = f"<ul>{''.join(digest_links)}</ul>" if digest_links else "<p class='muted'>Пока нет.</p>"

    next_num = rm.get_next_episode_number()
    today = datetime.now().strftime("%Y-%m-%d")
    return PAGE.format(
        base_url=rm.BASE_URL,
        author=html.escape(rm.FEED_META["author"]),
        flash=f"<p><b>{html.escape(flash)}</b></p>" if flash else "",
        episodes=episodes_html,
        files=files_html,
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
    return _redirect(f"Снят с публикации: {title}")


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
    return _redirect(f"Обновлено: {title}")


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
    return _redirect(f"Опубликован: {title}")


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
    return _redirect(f"Удалён: {filename}")
