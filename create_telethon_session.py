"""
Однократная авторизация Telethon (user-аккаунт для парсера групп).

Запуск только локально в терминале (не на Bothost):
  python create_telethon_session.py
  python create_telethon_session.py --qr          # если код в SMS/чат не приходит
  python create_telethon_session.py --name discovery_session

Нужны API_ID и API_HASH в .env — те же, что на сервере.
Создаст user_session.session → загрузите на Bothost в /app/shared (общее хранилище).
"""
import argparse
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import API_ID, API_HASH, get_default_session_name

QR_IMAGE = "_telethon_qr_login.png"


def _show_qr_png(url: str) -> None:
    """PNG надёжнее ASCII в PowerShell — Telegram часто не читает QR из терминала."""
    try:
        import qrcode  # type: ignore

        img = qrcode.make(url, box_size=8, border=2)
        img.save(QR_IMAGE)
        print(f"QR сохранён: {os.path.abspath(QR_IMAGE)}")
        if sys.platform == "win32":
            os.startfile(QR_IMAGE)
            print("(Файл открыт в просмотрщике — сканируйте с экрана, не из терминала.)\n")
        else:
            print("(Откройте файл и сканируйте с экрана.)\n")
    except ImportError:
        print("Установите: pip install qrcode pillow\n")
        print(f"Ссылка tg:// (вставьте на https://www.qr-code-generator.com/):\n{url}\n")


async def _qr_login(client: TelegramClient) -> None:
    await client.connect()
    if await client.is_user_authorized():
        return

    print("Вход по QR — SMS не нужен.\n")
    print("На телефоне аккаунт +7 968 … → только так:")
    print("  Настройки → Устройства → Подключить устройство")
    print("  (НЕ обычная камера и НЕ «Сканировать QR» в поиске)\n")

    qr = await client.qr_login()
    _show_qr_png(qr.url)
    print(f"Срок действия QR: {qr.expires} UTC")
    print("Жду сканирования… (Ctrl+C — отмена)\n")

    try:
        await qr.wait()
    except asyncio.TimeoutError:
        print("QR истёк. Обновляю…")
        await qr.recreate()
        _show_qr_png(qr.url)
        await qr.wait()
    except SessionPasswordNeededError:
        pwd = input("Облачный пароль (2FA): ")
        await client.sign_in(password=pwd)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Создать .session для Telethon")
    parser.add_argument(
        "--qr",
        action="store_true",
        help="Вход по QR (Telegram → Устройства → Подключить устройство)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Имя файла сессии без .session (по умолчанию user_session)",
    )
    return parser.parse_args()


async def main():
    if not API_ID or not API_HASH:
        print("Задайте API_ID и API_HASH в .env (https://my.telegram.org → API development tools)")
        return

    args = _parse_args()
    session_name = args.name.strip() or os.getenv("TELEGRAM_SESSION_NAME", "").strip() or get_default_session_name()

    print("Вход в Telegram для парсера групп...")
    print("(Это user-аккаунт, не бот @PromostaffHunter_bot)\n")

    client = TelegramClient(session_name, API_ID, API_HASH)
    if args.qr:
        await _qr_login(client)
    else:
        await client.start()
    me = await client.get_me()
    print(f"\nГотово: {me.first_name} (@{me.username or '—'})")
    print(f"Файл: {session_name}.session")
    if session_name.endswith("discovery_session") or "discovery" in session_name:
        print("Discovery: файл остаётся локально, на Bothost не загружать.\n")
    else:
        print("Загрузите его на Bothost в «Общие файлы» (/app/shared) и перезапустите бота.\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
