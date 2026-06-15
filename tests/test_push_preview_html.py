# -*- coding: utf-8 -*-
"""Регрессия: push preview HTML собирается до цикла рассылки."""

from services.vacancy_card import VacancyCardInput, build_vacancy_preview_html


def test_push_preview_html_from_card_input():
    inp = VacancyCardInput(
        category_code="loader",
        category_name="Грузчик",
        category_emoji="📦",
        body="Завтра 9:30\n💰 450 ₽/ч\n👉 @boss",
        freshness="🟢 Свежая",
        author_contact="@boss",
        rate_hourly=450,
    )
    html = build_vacancy_preview_html(inp, show_published_at=True)
    assert "Грузчик" in html
    assert "boss" in html.lower()
