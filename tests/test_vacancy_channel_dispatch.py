# -*- coding: utf-8 -*-

from services.vacancy_channel_dispatch import schedule_vacancy_channel_crosspost


def test_channel_crosspost_scheduled_without_push(monkeypatch):
    scheduled = []

    def _spawn(coro):
        scheduled.append(coro)

    monkeypatch.setattr(
        "services.vacancy_channel_dispatch.is_vacancy_channel_autopost_enabled",
        lambda: True,
    )
    ok = schedule_vacancy_channel_crosspost(
        _spawn,
        bot=object(),
        order={"chat_title": "Test chat"},
        vacancy_id="vac_ch_1",
        category_code="loader",
        category_name="Грузчик",
        category_emoji="📦",
        body="Нужны грузчики 500/4",
        freshness="🟢 Свежая",
    )
    assert ok is True
    assert len(scheduled) == 1


def test_channel_crosspost_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "services.vacancy_channel_dispatch.is_vacancy_channel_autopost_enabled",
        lambda: False,
    )
    ok = schedule_vacancy_channel_crosspost(
        lambda coro: None,
        bot=object(),
        order={},
        vacancy_id="vac_ch_2",
        category_code="loader",
        category_name="Грузчик",
        category_emoji="📦",
        body="text",
        freshness="🟢",
    )
    assert ok is False
