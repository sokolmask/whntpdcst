#!/usr/bin/env python3
"""
build_site.py — static site generator for whntpdcst.com.

Builds: landing (EN + RU), podcast page (episodes from feed.xml),
blog (markdown posts with frontmatter from DATA_DIR/blog/).

Run inside podcast-admin or hermes container:
    python /opt/data/skills/podcast/site/build_site.py

Output: DATA_DIR/site/ (served by nginx at /).
Blog posts are managed from the admin panel; format:
    ---
    title: Заголовок
    date: 2026-07-05
    lang: ru          # ru | en
    ---
    markdown body
"""

import html as H
import os
import re
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import markdown
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rss_manager as rm

DATA_DIR = Path(os.environ.get("PODCAST_DATA_DIR", "/opt/data/podcast"))
BLOG_DIR = DATA_DIR / "blog"
OUT = Path(os.environ.get("SITE_OUT", str(DATA_DIR / "site")))
BASE_URL = "https://whntpdcst.com"

LINKS = {
    "github": "https://github.com/sokolmask",
    "email": "podcast@whntpdcst.com",
    "rss": f"{BASE_URL}/feed.xml",
    # Apple Podcasts show URL — вставить, когда будет под рукой
    "apple": "",
}

CSS = """
:root {
  --bg: #fcfcfa; --fg: #22242e; --muted: #7a7d8a; --line: #e7e7e1;
  --accent: #0b7a5e; --accent2: #2d5fa8; --card: #ffffff;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.65; font-size: 1.02rem;
}
.wrap { max-width: 46rem; margin: 0 auto; padding: 1.2rem 1.2rem 4rem; }
.mono, nav, .prompt, code, pre, h1, h2, h3 {
  font-family: ui-monospace, SFMono-Regular, Menlo, "Cascadia Mono", monospace;
}
nav { display: flex; gap: 1.1rem; padding: 1rem 0 2.2rem; font-size: .92rem; flex-wrap: wrap; }
nav a { color: var(--muted); text-decoration: none; }
nav a:hover, nav a.active { color: var(--accent); }
nav .lang { margin-left: auto; }
h1 { font-size: 1.45rem; margin: 0 0 .4rem; font-weight: 600; }
h2 { font-size: 1.05rem; margin-top: 2.4rem; font-weight: 600; }
h3 { font-size: 1rem; }
a { color: var(--accent2); }
.prompt { color: var(--muted); font-size: .92rem; margin: 2.2rem 0 .6rem; }
.prompt::before { content: "$ "; color: var(--accent); font-weight: 700; }
.tagline { color: var(--muted); margin: 0 0 1rem; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.2rem; margin: .8rem 0;
}
.card h3 { margin: 0 0 .3rem; }
.card p { margin: .2rem 0; }
.meta { color: var(--muted); font-size: .88rem; }
ul.plain { list-style: none; padding: 0; }
ul.plain li { margin: .5rem 0; }
audio { width: 100%; margin-top: .6rem; }
pre {
  background: #f4f4ef; border: 1px solid var(--line); border-radius: 8px;
  padding: .8rem 1rem; overflow-x: auto; font-size: .88rem;
}
code { background: #f4f4ef; padding: .1rem .35rem; border-radius: 4px; font-size: .9em; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid var(--accent); margin: 1rem 0; padding: .1rem 1rem; color: #4a4d59; }
img { max-width: 100%; border-radius: 8px; }
hr { border: none; border-top: 1px solid var(--line); margin: 2rem 0; }
footer { margin-top: 4rem; padding-top: 1rem; border-top: 1px solid var(--line);
  color: var(--muted); font-size: .85rem; }
.btn {
  display: inline-block; border: 1px solid var(--line); border-radius: 8px;
  padding: .35rem .8rem; margin: .2rem .3rem .2rem 0; text-decoration: none;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88rem;
  color: var(--fg); background: var(--card);
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
"""

PAGE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{description}">
<title>{title}</title>
<link rel="stylesheet" href="/style.css">
<link rel="alternate" type="application/rss+xml" title="Что нового в AI" href="/feed.xml">
</head>
<body>
<div class="wrap">
<nav>
{nav}
</nav>
{body}
<footer>© {year} Sergei Sokolov · <span class="mono">built by a script, like everything here</span></footer>
</div>
</body>
</html>
"""


def nav_html(lang: str, active: str) -> str:
    if lang == "ru":
        items = [("/ru/", "~/", "home"), ("/podcast/", "~/подкаст", "podcast"),
                 ("/blog/", "~/блог", "blog")]
        toggle = '<a class="lang" href="/">[EN]</a>'
    else:
        items = [("/", "~/", "home"), ("/podcast/", "~/podcast", "podcast"),
                 ("/blog/", "~/blog", "blog")]
        toggle = '<a class="lang" href="/ru/">[RU]</a>'
    links = []
    for href, label, key in items:
        cls = ' class="active"' if key == active else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    return "\n".join(links) + "\n" + toggle


def render(out_path: Path, *, lang: str, active: str, title: str, description: str, body: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(PAGE.format(
        lang=lang, title=H.escape(title), description=H.escape(description),
        nav=nav_html(lang, active), body=body, year=datetime.now().year,
    ), encoding="utf-8")
    print(f"[site] {out_path.relative_to(OUT)}")


# ── Landing ───────────────────────────────────────────────────────────────────

LANDING_EN = f"""
<p class="prompt">whoami</p>
<h1>Sergei Sokolov</h1>
<p class="tagline">AI Adopter — I wire LLMs into everyday life and work.</p>

<p class="prompt">cat about.txt</p>
<p>I believe the fastest way to understand AI is to hand it real jobs.
Everything on this domain is an experiment in exactly that: an AI-generated
podcast, automated news digests, agents running on a small home server.
Built with LLM pipelines, TTS, MTProto and duct tape — reviewed by a human.</p>

<p class="prompt">ls projects/</p>
<ul class="plain">
<li class="card"><h3><a href="/podcast/">whntpdcst — «Что нового в AI»</a></h3>
<p>Weekly Russian-language podcast about AI, fully generated by AI:
sources → digest → two-host script → multi-voice TTS. Human in the loop only
for listening and hitting «publish».</p>
<p class="meta">python · LLM pipeline · Gemini TTS · RSS</p></li>
<li class="card"><h3>morning digest</h3>
<p>Personal automated news brief compiled from Telegram channels every morning
— read as a user, summarized by an LLM, delivered before coffee.</p>
<p class="meta">telethon · LLM · cron</p></li>
<li class="card"><h3>homelab «carbon»</h3>
<p>An old laptop under the TV running the whole thing: Docker, tunnels,
pipelines, this site. No cloud bills, no uptime guarantees.</p>
<p class="meta">docker · cloudflare tunnel · nginx</p></li>
</ul>

<p class="prompt">cat contact</p>
<p><a href="{LINKS['github']}">github.com/sokolmask</a> ·
<a href="mailto:{LINKS['email']}">{LINKS['email']}</a></p>
"""

LANDING_RU = f"""
<p class="prompt">whoami</p>
<h1>Сергей Соколов</h1>
<p class="tagline">AI Adopter — встраиваю LLM в повседневную жизнь и работу.</p>

<p class="prompt">cat about.txt</p>
<p>Быстрее всего понять AI можно, поручив ему настоящую работу. Всё на этом
домене — эксперимент ровно об этом: подкаст, который генерирует AI,
автоматические новостные дайджесты, агенты на маленьком домашнем сервере.
Собрано из LLM-пайплайнов, TTS, MTProto и изоленты — проверено человеком.</p>

<p class="prompt">ls projects/</p>
<ul class="plain">
<li class="card"><h3><a href="/podcast/">whntpdcst — «Что нового в AI»</a></h3>
<p>Еженедельный подкаст об AI, полностью сгенерированный AI: источники →
дайджест → сценарий двух ведущих → многоголосый TTS. Человек в процессе
один раз — послушать и нажать «опубликовать».</p>
<p class="meta">python · LLM pipeline · Gemini TTS · RSS</p></li>
<li class="card"><h3>утренний дайджест</h3>
<p>Личная автоматическая новостная сводка из Telegram-каналов каждое утро —
читается от имени пользователя, суммаризируется LLM, приходит до кофе.</p>
<p class="meta">telethon · LLM · cron</p></li>
<li class="card"><h3>homelab «carbon»</h3>
<p>Старый ноутбук под телевизором, на котором крутится всё это: Docker,
туннели, пайплайны, этот сайт. Без счетов за облако и гарантий аптайма.</p>
<p class="meta">docker · cloudflare tunnel · nginx</p></li>
</ul>

<p class="prompt">cat contact</p>
<p><a href="{LINKS['github']}">github.com/sokolmask</a> ·
<a href="mailto:{LINKS['email']}">{LINKS['email']}</a></p>
"""


# ── Podcast page ──────────────────────────────────────────────────────────────

def episodes_from_feed() -> list[dict]:
    _, channel = rm._parse_feed()
    it_ns = "{" + rm.ITUNES_NS + "}"
    eps = []
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else ""
        desc = item.findtext("description") or ""
        m = re.search(r"https://\S+/digests/(\S+\.html)", desc)
        try:
            date = parsedate_to_datetime(item.findtext("pubDate") or "").strftime("%Y-%m-%d")
        except Exception:
            date = ""
        dur = int(float(item.findtext(it_ns + "duration") or 0))
        eps.append({
            "title": item.findtext("title") or "",
            "number": item.findtext(it_ns + "episode") or "",
            "url": url,
            "date": date,
            "duration": f"{dur // 60} мин" if dur else "",
            "digest": f"/digests/{m.group(1)}" if m else "",
        })
    return eps


def podcast_body() -> str:
    apple = (f'<a class="btn" href="{LINKS["apple"]}">Apple Podcasts</a>' if LINKS["apple"] else "")
    cards = []
    for e in episodes_from_feed():
        digest = f' · <a href="{e["digest"]}">текстовый дайджест</a>' if e["digest"] else ""
        cards.append(f"""<li class="card">
<h3>{H.escape(e['title'])}</h3>
<p class="meta">{e['date']}{' · ' + e['duration'] if e['duration'] else ''}{digest}</p>
<audio controls preload="none" src="{H.escape(e['url'])}"></audio>
</li>""")
    eps_html = "\n".join(cards) if cards else "<p class='meta'>Пока нет выпусков.</p>"
    return f"""
<p class="prompt">cat podcast/README.md</p>
<h1>«Что нового в AI»</h1>
<p class="tagline">Еженедельный AI-дайджест голосом и текстом — без бесконечных лент и видео.</p>
<p>Каждый выпуск полностью генерируется AI: сбор источников (YouTube, Hacker News,
статьи, Telegram-каналы), текстовый дайджест, сценарий диалога и голоса ведущих
Алекса и Саши. Подкаст на русском языке.</p>
<p>{apple}
<a class="btn" href="{LINKS['rss']}">RSS</a>
<a class="btn" href="https://antennapod.org/">Android: AntennaPod</a></p>
<p class="meta">В любом подкаст-приложении: «добавить по URL» → <code>{LINKS['rss']}</code></p>

<p class="prompt">ls episodes/ --sort=date</p>
<ul class="plain">
{eps_html}
</ul>
"""


# ── Blog ──────────────────────────────────────────────────────────────────────

def load_posts() -> list[dict]:
    posts = []
    if not BLOG_DIR.exists():
        return posts
    for p in sorted(BLOG_DIR.glob("*.md")):
        raw = p.read_text(encoding="utf-8")
        meta, body = {}, raw
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception as e:
                print(f"[blog] {p.name}: frontmatter не распарсился ({e}) — пропускаю")
                continue
            body = m.group(2)
        posts.append({
            "slug": p.stem,
            "title": str(meta.get("title", p.stem)),
            "date": str(meta.get("date", "")),
            "lang": str(meta.get("lang", "ru")),
            "draft": bool(meta.get("draft", False)),
            "body_md": body,
        })
    posts.sort(key=lambda x: x["date"], reverse=True)
    return [p for p in posts if not p["draft"]]


def blog_pages() -> None:
    posts = load_posts()
    items = []
    for post in posts:
        body_html = markdown.markdown(post["body_md"], extensions=["extra", "fenced_code"])
        body = f"""
<p class="prompt">cat blog/{H.escape(post['slug'])}.md</p>
<h1>{H.escape(post['title'])}</h1>
<p class="meta">{post['date']} · {post['lang']}</p>
{body_html}
<p><a href="/blog/">← blog</a></p>
"""
        render(OUT / "blog" / f"{post['slug']}.html",
               lang=post["lang"], active="blog", title=post["title"],
               description=post["title"], body=body)
        items.append(
            f'<li><span class="meta mono">{post["date"]}</span> — '
            f'<a href="/blog/{post["slug"]}.html">{H.escape(post["title"])}</a>'
            f' <span class="meta">[{post["lang"]}]</span></li>'
        )
    index_body = f"""
<p class="prompt">ls blog/ --sort=date</p>
<h1>Блог</h1>
<p class="tagline">Нерегулярные заметки: AI, автоматизация, инженерное. RU/EN вперемешку.</p>
<ul class="plain">
{chr(10).join(items) if items else "<p class='meta'>Пока пусто. Скоро будет.</p>"}
</ul>
"""
    render(OUT / "blog" / "index.html", lang="ru", active="blog",
           title="Блог — Sergei Sokolov", description="Заметки про AI и автоматизацию",
           body=index_body)


# ── Main ──────────────────────────────────────────────────────────────────────

def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "style.css").write_text(CSS, encoding="utf-8")
    render(OUT / "index.html", lang="en", active="home",
           title="Sergei Sokolov — AI Adopter",
           description="AI Adopter: LLM pipelines, automation, an AI-generated podcast",
           body=LANDING_EN)
    render(OUT / "ru" / "index.html", lang="ru", active="home",
           title="Сергей Соколов — AI Adopter",
           description="AI Adopter: LLM-пайплайны, автоматизация, AI-подкаст",
           body=LANDING_RU)
    render(OUT / "podcast" / "index.html", lang="ru", active="podcast",
           title="Подкаст «Что нового в AI»",
           description="Еженедельный AI-дайджест голосом и текстом",
           body=podcast_body())
    blog_pages()
    print(f"[site] Готово → {OUT}")


if __name__ == "__main__":
    build()
