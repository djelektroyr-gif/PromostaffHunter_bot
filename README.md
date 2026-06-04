# Promostaff Hunter Bot

Telegram-бот для парсинга вакансий (хелперы, грузчики, промо и др.) из групп и рассылки подписчикам.

## Документация

**[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** — живой конспект для разработки: архитектура, админка, парсер, подписка (trial + ручная оплата), бэклог, деплой.

**[docs/SUBSCRIPTION.md](docs/SUBSCRIPTION.md)** — полная логика подписки: тарифы, статусы `premium_requests`, сценарий оплаты с чеком, инструменты админа.

## Быстрый старт

1. Скопировать `.env` (см. `config.example.py` / переменные в DEVELOPMENT.md).
2. Авторизовать Telethon: первый запуск создаст `user_session.session`.
3. `pip install -r requirements.txt`
4. `python main.py`
5. Тесты: `python -m pytest tests/ -q`

## Стек

- **aiogram** — бот (polling)
- **Telethon** — парсинг групп (`user_session`)
- **SQLite** — `bot_database.db`
