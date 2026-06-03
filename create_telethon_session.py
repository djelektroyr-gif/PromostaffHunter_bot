"""
Однократная авторизация Telethon (user-аккаунт для парсера групп).

Запуск только локально в терминале (не на Bothost):
  python create_telethon_session.py

Нужны API_ID и API_HASH в .env — те же, что на сервере.
Создаст user_session.session → загрузите на Bothost в /app/shared (общее хранилище).
"""
import asyncio

from telethon import TelegramClient
from config import API_ID, API_HASH, get_default_session_name

SESSION_NAME = get_default_session_name()


async def main():
    if not API_ID or not API_HASH:
        print("Задайте API_ID и API_HASH в .env (https://my.telegram.org → API development tools)")
        return

    print("Вход в Telegram для парсера групп...")
    print("(Это user-аккаунт, не бот @PromostaffHunter_bot)\n")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"\nГотово: {me.first_name} (@{me.username or '—'})")
    print(f"Файл: {SESSION_NAME}.session")
    print("Загрузите его на Bothost в «Общие файлы» (/app/shared) и перезапустите бота.\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
