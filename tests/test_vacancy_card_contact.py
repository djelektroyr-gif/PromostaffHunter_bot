# -*- coding: utf-8 -*-
"""Контакт заказчика в превью карточки."""

from services.channel_post import build_channel_preview_text
from services.vacancy_card import VacancyCardInput, build_vacancy_preview_html


def test_channel_preview_hides_employer_contact():
    body = (
        "Завтра 9:30\n"
        "💰 450 ₽/ч\n"
        "👉 @DispIvan"
    )
    text = build_channel_preview_text(
        category_name="Грузчик",
        category_emoji="📦",
        category_code="loader",
        body=body,
        source="Secret",
        freshness="🟢 Свежая",
    )
    assert "DispIvan" not in text
    assert "t.me/DispIvan" not in text
    assert "в боте" in text.lower()


def test_preview_hides_username_contact_by_default():
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
    assert "DispIvan" not in html
    assert "t.me/DispIvan" not in html
    assert "Откликнуться" in html


def test_preview_shows_username_contact_when_enabled():
    body = (
        "Завтра 9:30\n"
        "💰 450 ₽/ч\n"
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
    html = build_vacancy_preview_html(inp, show_employer_contact=True)
    assert "DispIvan" in html
    assert "t.me/DispIvan" in html


def test_preview_hides_arrow_display_name_by_default():
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
    assert "Glavgruz" not in html
    assert "Откликнуться" in html
