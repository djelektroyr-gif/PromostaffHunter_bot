from services.response_cards import (
    draft_status_label,
    format_response_list_row,
    format_user_response_card,
)


def test_draft_status_labels():
    assert "готов" in draft_status_label("delivered")
    assert "отправлен" not in draft_status_label("delivered").lower()
    assert "✋" in draft_status_label("manual")
    assert "⚠️" in draft_status_label("failed")
    assert draft_status_label("delivered", admin=True) == "📝 черновик готов"
    assert "отправлен" not in draft_status_label("delivered", admin=True).lower()


def test_format_admin_response_list_row_shows_source_and_real_status():
    from services.response_cards import format_admin_response_list_row

    line = format_admin_response_list_row(
        {
            "source_chat_title": "HelpersTeam",
            "draft_status": "delivered",
            "is_closed": False,
        },
        1,
        "Команев Олег",
    )
    assert "HelpersTeam" in line
    assert "📝 черновик готов" in line
    assert "отправлен" not in line.lower()
    assert "Команев" in line


def test_format_response_list_row_uses_category_not_source():
    line = format_response_list_row(
        {
            "source_chat_title": "Promo Jobs Moscow",
            "category_code": "loader",
            "draft_status": "manual",
            "is_closed": False,
        },
        1,
    )
    assert line.startswith("1.")
    assert "Promo Jobs" not in line
    assert "Грузчик" in line


def test_user_response_card_hides_source():
    text = format_user_response_card(
        {
            "draft_status": "delivered",
            "responded_at": "2026-06-06",
            "source_chat_title": "Secret Parser Chat",
            "category_code": "helper",
            "employer_contact": "@boss",
            "vacancy_text": "Нужен хелпер",
            "is_closed": False,
        }
    )
    assert "Secret Parser" not in text
    assert "Хелпер" in text
    assert "готов" in text
