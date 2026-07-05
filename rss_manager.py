#!/usr/bin/env python3
"""
rss_manager.py — Manages Apple Podcasts-compatible RSS feed for the AI podcast.

Feed location: /opt/data/podcast/feed.xml
"""

import os
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom import minidom

FEED_PATH = Path("/opt/data/podcast/feed.xml")
BASE_URL = "https://whntpdcst.com"
MAX_EPISODES = 20

FEED_META = {
    "title": "Что нового в AI",
    "description": (
        "Еженедельный подкаст об искусственном интеллекте, ML и технологиях. "
        "Идея — голосовой и текстовый дайджест для тех, кто хочет оставаться в курсе "
        "AI-новостей без чтения бесконечных лент и просмотра видео. Источники я выбрал "
        "для личного пользования, и дайджест собирается так, чтобы быть интересным "
        "в первую очередь мне самому — но среди друзей и коллег оказался запрос на такое, "
        "поэтому выпускаю в виде подкаста. Каждый выпуск полностью генерируется AI: "
        "дайджест, сценарий и голоса ведущих Алекса и Саши."
    ),
    "subtitle": "Еженедельный AI-дайджест голосом и текстом — без бесконечных лент и видео",
    "author": "Sergei Sokolov",
    "email": "podcast@whntpdcst.com",
    "language": "ru",
    "cover": f"{BASE_URL}/cover.jpg",
    "link": BASE_URL,
    "category": "Technology",
    "subcategory": "Artificial Intelligence",
}


def _make_empty_feed() -> ET.Element:
    """Create a fresh RSS root element with channel metadata."""
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:content": "http://purl.org/rss/1.0/modules/content/",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
    })
    channel = ET.SubElement(rss, "channel")

    def sub(parent, tag, _text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if _text is not None:
            el.text = _text
        return el

    sub(channel, "title", FEED_META["title"])
    sub(channel, "description", FEED_META["description"])
    sub(channel, "link", FEED_META["link"])
    sub(channel, "language", FEED_META["language"])
    sub(channel, "copyright", f"© {datetime.now().year} {FEED_META['author']}")
    sub(channel, "lastBuildDate", formatdate())
    sub(channel, "atom:link", href=f"{BASE_URL}/feed.xml",
        rel="self", type="application/rss+xml")

    # iTunes tags
    sub(channel, "itunes:author", FEED_META["author"])
    sub(channel, "itunes:subtitle", FEED_META["subtitle"])
    sub(channel, "itunes:summary", FEED_META["description"])
    sub(channel, "itunes:explicit", "no")
    sub(channel, "itunes:type", "episodic")

    owner = sub(channel, "itunes:owner")
    sub(owner, "itunes:name", FEED_META["author"])
    sub(owner, "itunes:email", FEED_META["email"])

    image = sub(channel, "itunes:image", href=FEED_META["cover"])

    cat = ET.SubElement(channel, "itunes:category", {"text": FEED_META["category"]})
    ET.SubElement(cat, "itunes:category", {"text": FEED_META["subcategory"]})

    # Standard image block
    img = sub(channel, "image")
    sub(img, "url", FEED_META["cover"])
    sub(img, "title", FEED_META["title"])
    sub(img, "link", FEED_META["link"])

    return rss


def _prettify(rss: ET.Element) -> str:
    """Return a pretty-printed XML string."""
    raw = ET.tostring(rss, encoding="unicode", xml_declaration=False)
    dom = minidom.parseString(raw)
    pretty = dom.toprettyxml(indent="  ", encoding=None)
    # minidom adds its own xml declaration; prepend the correct one
    lines = pretty.splitlines()
    # remove the minidom-generated declaration if present
    if lines and lines[0].startswith("<?xml"):
        lines = lines[1:]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(lines) + "\n"


def _parse_feed() -> tuple[ET.Element, ET.Element]:
    """Return (rss_root, channel_element). Creates feed if missing."""
    if FEED_PATH.exists():
        try:
            # Register namespaces so they're preserved on re-serialisation
            ET.register_namespace("", "")
            ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
            ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
            ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
            rss = ET.parse(str(FEED_PATH)).getroot()
            channel = rss.find("channel")
            if channel is not None:
                return rss, channel
        except ET.ParseError:
            pass
    rss = _make_empty_feed()
    return rss, rss.find("channel")


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def _sync_channel_meta(channel: ET.Element) -> None:
    """Refresh channel-level metadata from FEED_META (feed may predate config changes)."""
    def set_text(parent, tag, value):
        # accept both Clark ({ns}tag) and prefixed (itunes:tag) forms
        alt = tag.replace(f"{{{ITUNES_NS}}}", "itunes:")
        for child in parent:
            if child.tag in (tag, alt):
                child.text = value
                return
        ET.SubElement(parent, tag).text = value

    it = f"{{{ITUNES_NS}}}"
    set_text(channel, "title", FEED_META["title"])
    set_text(channel, "description", FEED_META["description"])
    set_text(channel, "copyright", f"© {datetime.now().year} {FEED_META['author']}")
    set_text(channel, f"{it}author", FEED_META["author"])
    set_text(channel, f"{it}subtitle", FEED_META["subtitle"])
    set_text(channel, f"{it}summary", FEED_META["description"])
    owner = next(
        (c for c in channel if c.tag in (f"{it}owner", "itunes:owner")), None
    )
    if owner is not None:
        set_text(owner, f"{it}name", FEED_META["author"])


def _get_existing_items(channel: ET.Element) -> list[ET.Element]:
    return channel.findall("item")


def _remove_items(channel: ET.Element):
    for item in channel.findall("item"):
        channel.remove(item)


def add_episode(
    title: str,
    description: str,
    mp3_path: str | Path,
    duration_seconds: int,
    episode_number: int,
) -> None:
    """
    Add a new episode to the RSS feed.

    Args:
        title:            Episode title (e.g. "Выпуск 12 — 04 июля 2026")
        description:      Short episode description / show notes
        mp3_path:         Absolute path to the MP3 file on the server
        duration_seconds: Audio duration in whole seconds
        episode_number:   Sequential episode number (used in guid/url)
    """
    mp3_path = Path(mp3_path)
    mp3_filename = mp3_path.name
    mp3_url = f"{BASE_URL}/episodes/{mp3_filename}"

    # File size for <enclosure>
    try:
        file_size = mp3_path.stat().st_size
    except FileNotFoundError:
        file_size = 0

    pub_date = formatdate(usegmt=True)

    rss, channel = _parse_feed()
    _sync_channel_meta(channel)
    existing_items = _get_existing_items(channel)

    # Build the new <item>
    item = ET.Element("item")

    def sub(parent, tag, _text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if _text is not None:
            el.text = _text
        return el

    sub(item, "title", title)
    sub(item, "description", description)
    sub(item, "pubDate", pub_date)
    sub(item, "guid", mp3_url, isPermaLink="true")
    sub(item, "enclosure",
        url=mp3_url,
        length=str(file_size),
        type="audio/mpeg")
    sub(item, "itunes:title", title)
    sub(item, "itunes:summary", description)
    sub(item, "itunes:author", FEED_META["author"])
    sub(item, "itunes:duration", str(duration_seconds))
    sub(item, "itunes:explicit", "no")
    sub(item, "itunes:episode", str(episode_number))
    sub(item, "itunes:episodeType", "full")
    sub(item, "itunes:image", href=FEED_META["cover"])

    # Update lastBuildDate in channel
    lb = channel.find("lastBuildDate")
    if lb is not None:
        lb.text = pub_date
    else:
        ET.SubElement(channel, "lastBuildDate").text = pub_date

    # Remove all existing items, then re-insert: new first, old after (max 20)
    _remove_items(channel)
    channel.append(item)
    for old in existing_items[: MAX_EPISODES - 1]:
        channel.append(old)

    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEED_PATH.write_text(_prettify(rss), encoding="utf-8")
    print(f"[rss] feed updated → {FEED_PATH}  ({len(existing_items[:MAX_EPISODES-1])+1} episodes)")


def get_next_episode_number() -> int:
    """Return the next episode number based on existing feed items."""
    if not FEED_PATH.exists():
        return 1
    try:
        rss = ET.parse(str(FEED_PATH)).getroot()
        channel = rss.find("channel")
        if channel is None:
            return 1
        items = channel.findall("item")
        if not items:
            return 1
        # Try to parse itunes:episode from the most recent item
        ns = "http://www.itunes.com/dtds/podcast-1.0.dtd"
        ep_el = items[0].find(f"{{{ns}}}episode")
        if ep_el is not None and ep_el.text:
            return int(ep_el.text) + 1
        return len(items) + 1
    except Exception:
        return 1


if __name__ == "__main__":
    # Quick smoke test — adds a fake episode
    import sys
    print("RSS Manager smoke test")
    print(f"Feed path: {FEED_PATH}")
    ep_num = get_next_episode_number()
    print(f"Next episode number: {ep_num}")
    add_episode(
        title=f"Выпуск {ep_num} — тест",
        description="Тестовый эпизод для проверки генерации RSS.",
        mp3_path="/opt/data/podcast/episodes/test.mp3",
        duration_seconds=600,
        episode_number=ep_num,
    )
    print("Done. Check feed.xml")
