# -*- coding: utf-8 -*-
"""Алерт админу при отклике без контакта заказчика."""

from main import build_admin_response_notify_html


def test_admin_response_notify_html_escapes_special_chars():
    html = build_admin_response_notify_html(
        {
            "user_id": 12345,
            "full_name": "Иван <тест> & Петров",
            "age": 25,
            "phone": "+7 (999) 111-22-33",
            "username": "boss_user",
        },
        source_chat="Чат «Грузчики»",
        vacancy_link="https://t.me/c/1/42?start=foo&bar",
    )
    assert "MarkdownV2" not in html
    assert "<b>НОВЫЙ ОТКЛИК" in html
    assert "Иван &lt;тест&gt; &amp; Петров" in html
    assert "tg://user?id=12345" in html
    assert "https://t.me/c/1/42?start=foo&amp;bar" in html
    assert "@boss_user" in html
