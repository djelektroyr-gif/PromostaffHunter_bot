from services.channel_custom_post import format_custom_post_preview
from services.channel_stats import build_channel_stats_html


def test_custom_post_preview():
    text = format_custom_post_preview("<b>Новость</b> индустрии", with_bot_button=True)
    assert "Новость" in text
    assert "Кнопка" in text


def test_channel_stats_html():
    html = build_channel_stats_html(
        member_count=1200,
        member_count_delta=15,
        posts_summary={"vacancy": 30, "promo": 6, "custom": 2, "total": 38},
        joins=3,
        leaves=1,
        activity_hours_line="14:00 — 8 пост.",
    )
    assert "1200" in html
    assert "+15" in html
    assert "вакансии: 30" in html
    assert "Bot API" in html
