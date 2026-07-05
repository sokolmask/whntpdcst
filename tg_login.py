#!/usr/bin/env python3
"""
tg_login.py — one-time Telegram user login for the podcast Telegram source.

Creates a StringSession so podcast_skill.py can read your subscribed channels
(including private ones) via MTProto as your user account.

Steps:
    1. Get api_id + api_hash at https://my.telegram.org → "API development tools"
       (app title/short_name can be anything, e.g. whntpdcst)
    2. pip install telethon && python tg_login.py
       (asks phone number, login code from Telegram, 2FA password if set)
    3. Copy the printed TELEGRAM_* lines into /home/sokolmask/hermes-agent/.env
       on carbon, then force-recreate the containers:
       cd ~/hermes-data/skills/podcast/docker && docker compose up -d --force-recreate
    4. Paste the printed channel list (edit to taste) into "Дополнительные
       источники" in the admin panel (sources.extra.yaml).

The session string grants full access to your Telegram account — treat it
like a password, keep it only in .env on carbon.
"""

import sys

try:
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Сначала: pip install telethon")


def main():
    api_id = int(input("api_id (с my.telegram.org): ").strip())
    api_hash = input("api_hash: ").strip()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = client.get_me()
        print(f"\nЗалогинен как: {me.first_name} (@{me.username})")

        print("\n# ── Добавь в /home/sokolmask/hermes-agent/.env ──")
        print(f"TELEGRAM_API_ID={api_id}")
        print(f"TELEGRAM_API_HASH={api_hash}")
        print(f"TELEGRAM_SESSION={client.session.save()}")

        print("\n# ── Твои каналы — для «Дополнительных источников» в админке ──")
        print("# (оставь нужные, впиши category; приватные каналы идут по numeric id)")
        print("telegram:")
        print("  channels:")
        for d in client.iter_dialogs():
            if d.is_channel and not d.is_group:
                ent = d.entity
                name = f"@{ent.username}" if getattr(ent, "username", None) else str(d.id)
                print(f'    - {{name: "{name}", category: ""}}  # {d.title}')


if __name__ == "__main__":
    main()
