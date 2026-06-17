"""Тесты единого формата карточки вакансии (превью / полная)."""

from services.vacancy_card import VacancyCardInput, build_vacancy_full_html, build_vacancy_preview_html


def _helper_body() -> str:
    return (
        "Нужны 16 хелперов на завтра\n"
        "Краснопресненская наб.\n"
        "Помощь на демонтаже, выгрузка\n"
        "Ставка: 500₽/ч\n"
        "Заявки: @boss123"
    )


def test_preview_structured_lines_no_contacts():
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
    text = build_vacancy_preview_html(inp)
    assert "Хелпер" in text
    assert "Свежая: только что" in text
    assert "Опубликовано" in text
    assert "09.06.2026" in text
    assert "16 хелпер" in text.lower()
    assert "Краснопресненская" in text
    assert "500" in text
    assert "на завтра" in text
    assert "10:00" in text
    assert "boss123" in text.lower()


def test_full_card_has_publication_and_body():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body=_helper_body(),
        freshness="🟢 Свежая: только что",
        published_at="09.06.2026 12:00 МСК",
    )
    text = build_vacancy_full_html(inp)
    assert "Опубликовано" in text
    assert "09.06.2026" in text
    assert "демонтаж" in text.lower()
    assert "boss123" in text.lower()


def test_channel_preview_no_publication_line():
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
    text = build_vacancy_preview_html(inp, show_published_at=False)
    assert "Свежая: только что" in text
    assert "Опубликовано" not in text
    assert "на завтра" in text


def test_preview_negotiated_rate_when_no_digits():
    inp = VacancyCardInput(
        category_code="driver",
        category_name="Водитель",
        category_emoji="🚐",
        body="Ищу водителя на своём авто, по договорённости. @boss",
        freshness="🟢 Свежая",
    )
    text = build_vacancy_preview_html(inp)
    assert "договорённости" in text.lower()


def test_preview_fallback_when_sparse_body():
    inp = VacancyCardInput(
        category_code="promoter",
        category_name="Промоутер",
        category_emoji="📢",
        body="☎️ @spam_only",
        freshness="🟢 Актуальна",
    )
    text = build_vacancy_preview_html(inp)
    assert "Промоутер" in text
    assert "spam_only" in text.lower()


def test_format_published_at_bad_timestamp_empty():
    from services.vacancy_card import format_vacancy_published_at

    assert format_vacancy_published_at("not-a-real-date") == ""
    assert format_vacancy_published_at(None) == ""


def test_push_row_min_hours_on_card():
    from services.vacancy_card import card_input_from_push_row
    from services.vacancy_rate import format_vacancy_rate_line

    row = [None] * 23
    row[0] = "Грузчики, разгрузка"
    row[5] = "loader"
    row[18] = 2000
    row[19] = 4
    inp = card_input_from_push_row(
        tuple(row),
        freshness="🟢 Свежая",
        category_name="Грузчик",
        category_emoji="📦",
        category_code="loader",
    )
    rate = format_vacancy_rate_line(body=row[0], rate_shift=inp.rate_shift, min_hours=inp.min_hours)
    assert rate is not None
    assert "от 4 ч" in rate

