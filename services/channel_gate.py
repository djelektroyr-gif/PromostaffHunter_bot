"""Канал: env (Bothost) + настройки в БД — одна точка для автопоста вакансий."""

from __future__ import annotations

from config import CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID


def is_channel_env_configured() -> bool:
    """Канал задан в env и включён флаг CHANNEL_CROSSPOST_ENABLED."""
    return bool(CHANNEL_CROSSPOST_ENABLED and HUNTER_CHANNEL_ID)


def is_vacancy_channel_autopost_enabled() -> bool:
    """Автопост вакансий: env + переключатель в админке (crosspost_enabled)."""
    if not is_channel_env_configured():
        return False
    from db import is_channel_crosspost_enabled

    return is_channel_crosspost_enabled()
