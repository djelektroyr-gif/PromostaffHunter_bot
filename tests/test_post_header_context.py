"""Тесты шапки поста и enrich digest."""

from parser import (
    detect_category,
    enrich_digest_block,
    evaluate_digest_blocks,
    evaluate_vacancy,
    has_payment_signal,
)
from services.channel_rate import extract_hourly_rate_rub, extract_shift_rate_rub
from services.post_header_context import extract_shared_header


def test_extract_shared_header_before_numbered_blocks():
    text = (
        "👷 Хелперы\n"
        "450 р/ч\n\n"
        "1. м. Тверская\n"
        "2. м. Сокол"
    )
    header = extract_shared_header(text)
    assert "Хелпер" in header
    assert "450" in header
    assert "1." not in header


def test_enrich_digest_block_adds_role_and_rate_from_header():
    full = (
        "👷 **2 парня хелпера**\n"
        "450 р/ч по окончанию\n"
        "@event_boss\n\n"
        "1. м. Тверская, сегодня 10:00\n"
        "2. м. Сокол, завтра 12:00"
    )
    block = "1. м. Тверская, сегодня 10:00"
    enriched = enrich_digest_block(block, full)
    assert "хелпер" in enriched.lower()
    assert "450" in enriched
    assert detect_category(enriched) == "helper"
    assert has_payment_signal(enriched)


def test_enrich_digest_block_keeps_block_own_rate():
    full = "Общая ставка 300 р/ч\n@boss\n\n1. грузчики, 500 р/ч\n@boss2"
    block = "1. грузчики, 500 р/ч\n@boss2"
    enriched = enrich_digest_block(block, full)
    assert extract_hourly_rate_rub(enriched) == 500


def test_header_boost_category_from_title_line():
    text = (
        "**2 парня хелпера**\n"
        "📅 Сегодня\n"
        "Помощь флористам на площадке\n"
        "450 р/ч\n"
        "@event_boss"
    )
    assert detect_category(text) == "helper"
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "helper"
    assert reason == "accepted"


def test_shift_rate_animator_colon_format():
    text = "Аниматор: 6500р + костюм\n@cast_boss"
    assert extract_shift_rate_rub(text) == 6500
    assert has_payment_signal(text)


def test_digest_blocks_accept_with_shared_header():
    full = (
        "Промоутеры, 500 р/ч\n"
        "@promo_boss\n\n"
        "1. м. Тверская, 10:00–18:00\n"
        "2. м. Сокол, 11:00–19:00"
    )
    poster = {"username": "promo_boss", "user_id": 1}
    blocks = evaluate_digest_blocks(full, poster)
    assert len(blocks) >= 1
    assert blocks[0][0] == "promoter"
