from main import format_publication_time


def test_format_publication_time_shows_msk():
    assert format_publication_time("2026-06-04 02:35:00") == "04.06.2026 05:35 МСК"
