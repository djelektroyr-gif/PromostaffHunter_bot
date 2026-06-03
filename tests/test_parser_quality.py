from datetime import datetime, timedelta, timezone

from parser import (
    build_vacancy_dedupe_key,
    extract_contact_from_text,
    extract_address_from_text,
    is_message_for_today,
    _extract_phone_digits,
    detect_duplicate_type,
    detect_category,
)


def test_extract_address_prefers_explicit_address():
    text = "Требуется хелпер. Адрес: Москва, ул. Ленина, 15. Оплата сразу."
    assert extract_address_from_text(text) == "Москва, ул. Ленина, 15. Оплата сразу"


def test_extract_address_detects_metro():
    text = "Срочно нужен промоутер, метро Таганская, ставка 3500"
    assert extract_address_from_text(text) == "метро Таганская"

def test_extract_address_detects_metro_with_emoji():
    text = "Метро: 🚇 Беляево"
    assert extract_address_from_text(text) == "метро Беляево"

def test_extract_contact_from_tg_resolve_link():
    text = "Контакт: tg://resolve?domain=GuseynzadeGF"
    assert extract_contact_from_text(text) == "@GuseynzadeGF"


def test_is_message_for_today():
    now = datetime.now(timezone.utc)
    assert is_message_for_today(now) is True
    assert is_message_for_today(now - timedelta(days=1)) is False


def test_dedupe_key_same_for_cross_posted_messages():
    text1 = "Нужны хелперы завтра в ТЦ, писать @manager_one, ставка 4000"
    text2 = "Нужны хелперы завтра в ТЦ!!! писать @manager_two, ставка 4000"
    key1 = build_vacancy_dedupe_key(text1, "@manager")
    key2 = build_vacancy_dedupe_key(text2, "@manager")
    assert key1 == key2


def test_extract_phone_digits_normalizes_8_to_7():
    text = "Контакт: 8 (916) 123-45-67"
    assert _extract_phone_digits(text) == "79161234567"


def test_fuzzy_duplicate_detects_same_phone_and_similar_text(monkeypatch):
    def fake_exact_duplicate(*args, **kwargs):
        return False

    def fake_recent(*args, **kwargs):
        return [
            {
                "id": "old_1",
                "message_text": "Нужны хелперы в ТЦ с 10 до 20, ставка 4500, контакт 8 916 123 45 67",
                "author_contact": None,
                "dedupe_key": "abc",
            }
        ]

    monkeypatch.setattr("parser.has_recent_duplicate_vacancy", fake_exact_duplicate)
    monkeypatch.setattr("parser.get_recent_open_vacancies_for_dedupe", fake_recent)

    duplicate_type = detect_duplicate_type(
        "Нужны хелперы в ТЦ, смена 10-20, оплата 4500, контакт +7 (916) 123-45-67",
        None,
        "new_key",
    )
    assert duplicate_type == "fuzzy"


def test_detect_category_loader_not_parking_for_upakovshchik():
    text = "Нужен упаковщик на склад, ставка 400 р/час"
    assert detect_category(text) == "loader"


def test_detect_category_parking_for_parkovshchik():
    text = "Нужен парковщик на мероприятие, парковка VIP"
    assert detect_category(text) == "parking"


def test_detect_category_loader_for_gruzchik():
    text = "Завтра к 7:00 нужны 3 грузчика в ТЦ Коламбус"
    assert detect_category(text) == "loader"


def test_detect_category_helper_for_helper_night():
    text = "Хелперы в ночь, помогать на площадке, ставка 600 руб/час"
    assert detect_category(text) == "helper"


def test_format_parser_chats_report():
    from parser import format_parser_chats_report

    report = format_parser_chats_report([], "empty")
    assert "Чаты парсинга" in report
    assert "/addchat" in report


def test_make_vacancy_id_same_for_parser_and_send():
    from parser import make_vacancy_id

    chat_id, message_id = "-100123456", "42"
    dedupe_key = "phone:79161234567|hash:abc"
    assert make_vacancy_id(chat_id, message_id, dedupe_key) == make_vacancy_id(chat_id, message_id, dedupe_key)
    assert make_vacancy_id(chat_id, message_id, dedupe_key) != make_vacancy_id(chat_id, message_id, None)
    assert len(make_vacancy_id(chat_id, message_id)) == 16
