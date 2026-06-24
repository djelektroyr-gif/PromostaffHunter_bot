from services.employer_contact import (
    format_phone_display,
    is_phone_only_employer_contact,
    phone_vacancy_notice_html,
)
from services.vacancy_card import VacancyCardInput, build_vacancy_preview_html


def test_format_phone_display():
    assert format_phone_display("89254807851") == "+7 (925) 480-78-51"


def test_is_phone_only():
    assert is_phone_only_employer_contact("89254807851")
    assert not is_phone_only_employer_contact("@boss")
    assert not is_phone_only_employer_contact("https://airtable.com/x")


def test_vacancy_preview_hides_phone_by_default():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body="Нужен хелпер, звоните 89254807851",
        freshness="Свежая",
        author_contact="89254807851",
    )
    html = build_vacancy_preview_html(inp)
    assert "+7 (925) 480-78-51" not in html
    assert "Откликнуться" in html


def test_vacancy_preview_shows_phone_when_enabled():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body="Нужен хелпер, звоните 89254807851",
        freshness="Свежая",
        author_contact="89254807851",
    )
    html = build_vacancy_preview_html(inp, show_employer_contact=True)
    assert "+7 (925) 480-78-51" in html
    assert "Откликнуться" in html


def test_vacancy_preview_no_notice_for_username():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body="Пишите @promostaffagency",
        freshness="Свежая",
        author_contact="@promostaffagency",
    )
    html = build_vacancy_preview_html(inp)
    assert "скопируйте текст отклика" not in html.lower()
