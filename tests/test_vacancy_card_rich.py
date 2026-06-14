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
    assert "boss123" in html.lower()


def test_full_rich_shows_visible_description():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body=_helper_body(),
        freshness="🟢 Свежая",
        published_at="09.06.2026 12:00 МСК",
    )
    html = build_vacancy_full_rich_html(inp)
    assert "<details>" not in html
    assert "Описание" in html
    assert "демонтаж" in html.lower()


def _zoo_helper_body() -> str:
    return (
        "Добрый день!\n"
        "Требуются сотрудники на мероприятие (монтаж) 🛠️\n\n"
        "📍 Локация: Московский зоопарк\n"
        "📅 Дата: 14.06.2026\n"
        "💰 Оплата за проект: 5.400 руб.\n"
        "Выплата через неделю после конца (15.06) проекта\n\n"
        "Вакансия: ХЕЛПЕР-ГРУЗЧИК\n"
        "👥 Количество: 2 человека\n"
        "⏰ Время работы: с 21:00 до 09:00\n"
        "Задачи:\n"
        "• Физическая помощь на демонтаже\n"
        "Как откликнуться:\n"
        "📲 @oneday_hr3 с пометкой «НОЧЬ»"
    )


def test_zoo_vacancy_rich_card_has_rate_date_and_body():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body=_zoo_helper_body(),
        freshness="🟢 Свежая: несколько часов назад",
        published_at="14.06.2026 12:43 МСК",
    )
    preview = build_vacancy_preview_rich_html(inp)
    full = build_vacancy_full_rich_html(inp)
    assert "5400" in preview
    assert "за проект" in preview
    assert "14.06.2026" in preview
    assert "21:00" in preview
    assert "09:00" in preview
    assert "Требуются сотрудники" in preview
    assert "Добрый день" not in preview.split("Описание")[0]
    assert "зоопарк" in full.lower()
    assert "демонтаж" in full.lower()
    assert "oneday_hr3" in full.lower()
