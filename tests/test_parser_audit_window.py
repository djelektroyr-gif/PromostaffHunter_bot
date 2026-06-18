# -*- coding: utf-8 -*-
"""Аудит: окно по времени, не фиксированные N последних постов."""

from datetime import datetime, timedelta, timezone

from parser import PARSER_AUDIT_HOURS, message_in_audit_window


class _Msg:
    def __init__(self, dt: datetime):
        self.date = dt


def test_message_in_audit_window_last_hour():
    now = datetime.now(timezone.utc)
    assert message_in_audit_window(_Msg(now - timedelta(hours=1))) is True


def test_message_in_audit_window_outside():
    now = datetime.now(timezone.utc)
    assert message_in_audit_window(_Msg(now - timedelta(hours=PARSER_AUDIT_HOURS + 2))) is False
