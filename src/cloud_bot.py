"""Polling bot for one-use invitations to the Telegram cloud service."""

import asyncio
import logging
import os
import re
import secrets
import time

import httpx

from src.cloud_secure import digest
from src.cloud_slots import Slots, slot_count
from src.oauth_proxy import _secret


async def telegram_api(client, credential, method, payload):
    reply = await client.post(f"https://api.telegram.org/bot{credential}/{method}", json=payload)
    reply.raise_for_status()
    data = reply.json()
    if not data.get("ok"):
        raise ValueError("Telegram Bot API request failed")
    return data["result"]


async def message(bot, slots, owner, username, issuer, update):
    msg = update.get("message", {})
    chat, user = msg.get("chat", {}), msg.get("from", {}).get("id")
    if chat.get("type") != "private" or chat.get("id") != user:
        return
    command, _, invite = msg.get("text", "").strip().partition(" ")
    command = command.split("@", 1)[0]
    buttons = None
    if command == "/invite" and user == owner:
        code = secrets.token_urlsafe(24)
        with slots.db(write=True) as db:
            db.execute("DELETE FROM cloud_invites WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO cloud_invites VALUES (?,?)", (digest(code), time.time() + 7 * 86400))
        text = f"Одноразовое приглашение на 7 дней.\nhttps://t.me/{username}?start={code}"
    elif command == "/start":
        text = f"Нет приглашения. Ваш ID: {user}"
        slot = slots.lookup(user)
        if invite and re.fullmatch(r"[A-Za-z0-9_-]{32}", invite):
            with slots.db(write=True) as db:
                valid = db.execute("SELECT 1 FROM cloud_invites WHERE digest=? AND expires>?", (digest(invite), time.time())).fetchone()
                if valid and slot is None:
                    position = slots.claim(db, user)
                    if position:
                        db.execute("DELETE FROM cloud_invites WHERE digest=?", (digest(invite),))
                        slot = True
                    else:
                        text = "Свободных мест пока нет. Попробуйте позже."
        if slot:
            text = "Откройте приложение, чтобы настроить доступ к Telegram."
            buttons = {"inline_keyboard": [[{"text": "Подключить Telegram", "web_app": {"url": issuer + "/app"}}]]}
    else:
        return
    await bot("sendMessage", {"chat_id": user, "text": text, **({"reply_markup": buttons} if buttons else {})})


async def healthy(client):
    try:
        response = await client.get("http://cloud-oauth:8001/.well-known/oauth-authorization-server", timeout=2)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def main():
    credential = _secret("CLOUD_BOT_TOKEN")
    owner = int(os.environ["OWNER_TELEGRAM_ID"])
    username, issuer = os.environ["CLOUD_BOT_USERNAME"], os.environ["PUBLIC_BASE_URL"].rstrip("/")
    slots = Slots(os.environ["OAUTH_DB_PATH"], slot_count())
    offset, failed = 0, False
    async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
        async def bot(method, payload):
            return await telegram_api(client, credential, method, payload)

        while True:
            try:
                updates = await bot("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
                down = not await healthy(client)
                notify, failed = down and not failed, down
                if notify:
                    await bot("sendMessage", {"chat_id": owner, "text": "Облачный сервис Telegram недоступен. Проверьте фасад."})
                for update in updates:
                    offset = update["update_id"] + 1
                    await message(bot, slots, owner, username, issuer, update)
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(3)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
