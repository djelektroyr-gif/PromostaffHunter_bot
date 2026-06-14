# -*- coding: utf-8 -*-
"""Контакт заказчика в превью карточки."""

from services.vacancy_card import VacancyCardInput, build_vacancy_preview_html


def test_preview_shows_username_contact():
    body = (
        "Завтра 9:30\n"
        "💰 450 ₽/ч\n"
        "1 человек\n"
        "👉 @DispIvan"
    )
    inp = VacancyCardInput(
        category_code="loader",
        category_name="Грузчик",
        category_emoji="📦",
        body=body,
        freshness="🟢 Свежая",
        author_contact="@DispIvan",
        rate_hourly=450,
    )
    html = build_vacancy_preview_html(inp)
    assert "DispIvan" in html
    assert "t.me/DispIvan" in html


def test_preview_shows_arrow_display_name():
    body = (
        "Красногорск, нужны: 1 человек\n"
        "Раздавать листовки, 15.06 в 11:45\n"
        "👉 Glavgruz Admin Екатерина"
    )
    inp = VacancyCardInput(
        category_code="promoter",
        category_name="Промоутер",
        category_emoji="📣",
        body=body,
        freshness="🟢 Свежая",
    )
    html = build_vacancy_preview_html(inp)
    assert "Glavgruz" in html
