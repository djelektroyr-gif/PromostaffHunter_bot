"""Тесты отложенных уведомлений «вакансия закрыта»."""

from __future__ import annotations

import os
import tempfile
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from db import (
    add_closed_notice_pending,
    add_subscriber,
    get_subscriber_filter_prefs_effective,
    init_db,
    list_closed_notice_pending,
    remove_closed_notice_pending,
    set_subscriber_filter_prefs,
    set_user_plan,
)
from db_backend import IS_POSTGRES
from services.filter_prefs import normalize_prefs
from services.push_notify import is_push_blocked
from services.vacancy_closed_notify import should_defer_closed_notice


@pytest.fixture
def tmp_db(monkeypatch):
    if IS_POSTGRES:
        pytest.skip("SQLite-only fixture")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr("db_backend.DATABASE_URL", "")
    monkeypatch.setattr("db_backend.IS_POSTGRES", False)
    monkeypatch.setattr("db_backend.SQLITE_PATH", path)
    init_db()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _quiet_prefs():
    return normalize_prefs({
        "notify": {
            "quiet_start": "00:00",
            "quiet_end": "09:00",
            "quiet_configured": True,
        },
    })


def _add_premium_user(uid: int) -> None:
    add_subscriber(uid, "u", "U", None)
    set_user_plan(uid, plan="premium", days=30, extend=True)


def test_push_blocked_during_user_quiet_hours(tmp_db):
    uid = 9001
    _add_premium_user(uid)
    set_subscriber_filter_prefs(uid, _quiet_prefs())
    prefs = get_subscriber_filter_prefs_effective(uid)
    at_730_msk = datetime(2026, 6, 3, 4, 30, tzinfo=timezone.utc)
    at_10_msk = datetime(2026, 6, 3, 7, 0, tzinfo=timezone.utc)
    assert is_push_blocked(prefs, now=at_730_msk) is True
    assert is_push_blocked(prefs, now=at_10_msk) is False


def test_should_defer_uses_push_blocked(tmp_db, monkeypatch):
    uid = 9002
    _add_premium_user(uid)
    set_subscriber_filter_prefs(uid, _quiet_prefs())
    monkeypatch.setattr(
        "services.vacancy_closed_notify.is_push_blocked",
        lambda prefs: True,
    )
    assert should_defer_closed_notice(uid) is True


def test_should_not_defer_without_prefs(tmp_db):
    uid = 9003
    add_subscriber(uid, "u", "U", None)
    assert should_defer_closed_notice(uid) is False


def test_closed_notice_pending_queue(tmp_db):
    uid = 9004
    add_subscriber(uid, "u", "U", None)
    assert add_closed_notice_pending(uid, "vac1") is True
    assert add_closed_notice_pending(uid, "vac1") is False
    assert add_closed_notice_pending(uid, "vac2") is True
    assert list_closed_notice_pending(uid) == ["vac1", "vac2"]
    assert remove_closed_notice_pending(uid, "vac1") is True
    assert list_closed_notice_pending(uid) == ["vac2"]


def test_deliver_defers_instead_of_sending(tmp_db, monkeypatch):
    from services import vacancy_closed_notify as vcn

    uid = 9010
    _add_premium_user(uid)
    set_subscriber_filter_prefs(uid, _quiet_prefs())

    bot = MagicMock()
    bot.send_message = AsyncMock()
    monkeypatch.setattr(vcn, "should_defer_closed_notice", lambda user_id: user_id == uid)

    asyncio.run(vcn.deliver_closed_vacancy_notices(bot, [("vac_x", [uid])]))

    bot.send_message.assert_not_called()
    assert list_closed_notice_pending(uid) == ["vac_x"]


def test_closed_notice_sent_only_once(tmp_db, monkeypatch):
    from services import vacancy_closed_notify as vcn

    uid = 9020
    add_subscriber(uid, "u", "U", None)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(
        vcn.deliver_closed_vacancy_notices(
            bot, [("vac_dup", [uid]), ("vac_dup", [uid])],
        )
    )
    assert bot.send_message.await_count == 1

    asyncio.run(vcn.deliver_closed_vacancy_notices(bot, [("vac_dup", [uid])]))
    assert bot.send_message.await_count == 1
