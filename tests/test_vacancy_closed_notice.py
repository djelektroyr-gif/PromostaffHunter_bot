from services.vacancy_closed_notice import format_closed_vacancy_notice_html


def test_closed_notice_has_category_and_preview():
    text = format_closed_vacancy_notice_html(
        category_emoji="📢",
        category_name="Промоутер",
        body="Нужен промоутер на 8 июня, 3500 руб, м. Сокол",
        address="м. Сокол",
    )
    assert "Промоутер" in text
    assert "Promo Jobs MSK" not in text
    assert "📢 Promo" not in text
    assert "промоутер" in text
    assert "f2c9e8d" not in text
    assert "ID вакансии" not in text


def test_closed_notice_without_body():
    text = format_closed_vacancy_notice_html(
        category_emoji="📦",
        category_name="Грузчик",
        body="",
    )
    assert "Грузчик" in text
    assert "Мои отклики" in text
