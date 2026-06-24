"""
Диагностика доставки кода входа Telethon (без ввода кода).
Запуск: python scripts/diagnose_telethon_code.py +79685337332
"""
import asyncio
import sys

from telethon import TelegramClient
from telethon.errors import FloodWaitError, PhoneNumberBannedError, PhoneNumberInvalidError

from config import API_ID, API_HASH

PHONE = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
SESSION = "_diag_code_session"


def _type_name(sent) -> str:
    t = getattr(sent, "type", None)
    return type(t).__name__ if t is not None else "?"


async def try_send(client: TelegramClient, phone: str, *, force_sms: bool) -> None:
    label = "force_sms=True" if force_sms else "обычный запрос"
    print(f"\n--- {label} ---")
    try:
        sent = await client.send_code_request(phone, force_sms=force_sms)
        print(f"  тип доставки: {_type_name(sent)}")
        print(f"  phone_code_hash: {sent.phone_code_hash[:12]}…")
        nxt = getattr(sent, "next_type", None)
        if nxt:
            print(f"  next_type (если не дождётесь): {type(nxt).__name__}")
        timeout = getattr(sent, "timeout", None)
        if timeout:
            print(f"  timeout до смены способа: {timeout} с")
    except FloodWaitError as e:
        print(f"  FloodWait: подождите {e.seconds} с (~{e.seconds // 60} мин)")
    except PhoneNumberBannedError:
        print("  Номер заблокирован в Telegram.")
    except PhoneNumberInvalidError:
        print("  Номер неверный для Telegram.")
    except Exception as e:
        print(f"  Ошибка: {type(e).__name__}: {e}")


async def main() -> None:
    if not API_ID or not API_HASH:
        print("Задайте API_ID и API_HASH в .env")
        return
    if not PHONE:
        print("Укажите номер: python scripts/diagnose_telethon_code.py +79685337332")
        return

    print(f"API_ID: задан | номер: {PHONE}")
    print(f"Временная сессия: {SESSION}.session (не трогает user_session)")

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await try_send(client, PHONE, force_sms=False)
        await try_send(client, PHONE, force_sms=True)
    else:
        me = await client.get_me()
        print(f"Сессия {SESSION} уже авторизована: {me.first_name}")
    await client.disconnect()
    print("\nГотово. Файл _diag_code_session.session можно удалить.")


if __name__ == "__main__":
    asyncio.run(main())
