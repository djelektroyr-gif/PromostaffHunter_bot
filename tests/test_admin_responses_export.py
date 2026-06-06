from admin_exports import build_responses_xlsx


def test_build_responses_xlsx_includes_draft_status_label():
    data = build_responses_xlsx([
        {
            "id": 1,
            "responded_at": "2026-06-06",
            "user_id": 100,
            "username": "tester",
            "full_name": "Иван",
            "phone": "+7999",
            "vacancy_id": "abc123",
            "category_code": "helper",
            "source_chat_title": "HelpersTeam",
            "employer_contact": "@boss",
            "draft_status": "delivered",
            "response_status": "pending",
            "vacancy_closed": False,
            "star_boost": False,
            "vacancy_link": "https://t.me/c/1/2",
            "vacancy_text": "Нужен хелпер",
        },
    ])
    assert len(data) > 1000
    assert data[:2] == b"PK"
