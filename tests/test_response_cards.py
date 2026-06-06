from services.response_cards import draft_status_label, format_response_list_row


def test_draft_status_labels():
    assert "✅" in draft_status_label("delivered")
    assert "✋" in draft_status_label("manual")
    assert "⚠️" in draft_status_label("failed")


def test_format_response_list_row():
    line = format_response_list_row(
        {
            "source_chat_title": "Promo Jobs Moscow",
            "draft_status": "manual",
            "is_closed": False,
        },
        1,
    )
    assert line.startswith("1.")
    assert "Promo Jobs" in line
