"""Тесты Rich HTML карточек вакансий (Bot API 10.1)."""

from services.vacancy_card import VacancyCardInput
from services.vacancy_card_rich import (
    build_vacancy_full_rich_html,
    build_vacancy_preview_rich_html,
)


def _helper_body() -> str:
    return (
        "Нужны 16 хелперов на завтра\n"
        "Краснопресненская наб.\n"
        "Помощь на демонтаже, выгрузка\n"
        "Ставка: 500₽/ч\n"
        "Заявки: @boss123"
    )


def test_preview_rich_has_heading_and_table():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body=_helper_body(),
        freshness="🟢 Свежая: только что",
        published_at="09.06.2026 12:00 МСК",
        shift_date="tomorrow",
        address_normalized="Краснопресненская наб.",
        rate_hourly=500,
        shift_time_start="10:00",
    )
    html = build_vacancy_preview_rich_html(inp)
    assert "<h3>" in html
    assert "<table>" in html
    assert "Хелпер" in html
    assert "Краснопресненская" in html
    assert "500" in html
    assert "<footer>PromoStaff Hunter</footer>" in html
    assert "@" not in html


def test_full_rich_has_details_block():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body=_helper_body(),
        freshness="🟢 Свежая",
        published_at="09.06.2026 12:00 МСК",
    )
    html = build_vacancy_full_rich_html(inp)
    assert "<details>" in html
    assert "<summary>Полный текст вакансии</summary>" in html
    assert "демонтаж" in html.lower()
