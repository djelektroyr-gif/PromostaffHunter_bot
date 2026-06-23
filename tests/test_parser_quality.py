from datetime import datetime, timedelta, timezone

from parser import (
    build_vacancy_dedupe_key,
    extract_contact_from_text,
    extract_address_from_text,
    is_message_recent,
    is_message_for_today,
    extract_metro_tokens,
    vacancy_matches_user_metro,
    _extract_phone_digits,
    detect_duplicate_type,
    detect_category,
    pick_employer_contact_for_response,
    chat_id_aliases,
    is_chat_monitored,
    is_vacancy_closed_text,
    is_strikethrough_closure,
)


class _FakeStrike:
    """Минимальная имитация MessageEntityStrike для тестов."""

    _test_strike = True

    def __init__(self, offset: int, length: int):
        self.offset = offset
        self.length = length


def test_extract_address_prefers_explicit_address():
    text = "Требуется хелпер. Адрес: Москва, ул. Ленина, 15. Оплата сразу."
    assert extract_address_from_text(text) == "Москва, ул. Ленина, 15. Оплата сразу"


def test_extract_address_detects_metro():
    text = "Срочно нужен промоутер, метро Таганская, ставка 3500"
    assert extract_address_from_text(text) == "метро Таганская"

def test_extract_address_detects_metro_with_emoji():
    text = "Метро: 🚇 Беляево"
    assert extract_address_from_text(text) == "м. Беляево"

def test_extract_contact_from_tg_resolve_link():
    text = "Контакт: tg://resolve?domain=GuseynzadeGF"
    assert extract_contact_from_text(text) == "@GuseynzadeGF"


def test_extract_contact_airtable_apply_link():
    text = (
        "Продавцы на ВДНХ\n"
        "[🙌ПОДАТЬ ЗАЯВКУ🙌](https://airtable.com/appglH7lKHqV99EIi/shrSX4Drh5gna7MkC)\n"
        "есть❓, пиши 👇\n"
        "📍 [Парк ОРИОН](https://yandex.ru/maps/-/CPvW7RjJ)\n"
    )
    assert extract_contact_from_text(text) == "https://airtable.com/appglH7lKHqV99EIi/shrSX4Drh5gna7MkC"


def test_pick_employer_contact_prefers_airtable_over_saved_channel():
    text = (
        "[🙌ПОДАТЬ ЗАЯВКУ🙌](https://airtable.com/appglH7lKHqV99EIi/shrSX4Drh5gna7MkC)\n"
    )
    assert pick_employer_contact_for_response("@HelpersTeam", text).startswith("https://airtable.com/")


def test_extract_contact_ignores_maps_links():
    text = "📍 [Парк ОРИОН](https://yandex.ru/maps/-/CPvW7RjJ)"
    assert extract_contact_from_text(text) is None


def test_is_message_recent_within_window():
    now = datetime.now(timezone.utc)
    assert is_message_recent(now) is True
    assert is_message_recent(now - timedelta(hours=30)) is True
    assert is_message_recent(now - timedelta(hours=40)) is False


def test_is_message_for_today_alias():
    now = datetime.now(timezone.utc)
    assert is_message_for_today(now) is True


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
    assert duplicate_type in ("fuzzy", "campaign")


def test_detect_category_loader_not_parking_for_upakovshchik():
    text = "Нужен упаковщик на склад, ставка 400 р/час"
    assert detect_category(text) == "loader"


def test_detect_category_parking_for_parkovshchik():
    text = "Нужен парковщик на мероприятие, парковка VIP"
    assert detect_category(text) == "parking"


def test_detect_category_handyman_not_security_for_raznorabochiy_with_propusk():
    text = (
        "завтра, 8:30, 1 человек. Паспорта при себе, пропускной режим.\n"
        "Адрес: Москва, Крылатская вл40\n"
        "Задача: разнорабочий на территории санатория, подай, принеси, сделай.\n"
        "Работа на 8ч.\n"
        "Оплата: 450/4/1800"
    )
    assert detect_category(text) == "handyman"


def test_detect_category_security_still_works_for_guard():
    text = "Нужен охранник на мероприятие, пропускной режим, 3500 за смену"
    assert detect_category(text) == "security"


def test_detect_category_loader_for_gruzchik():
    text = "Завтра к 7:00 нужны 3 грузчика в ТЦ Коламбус"
    assert detect_category(text) == "loader"


def test_detect_category_helper_for_helper_night():
    text = "Хелперы в ночь, помогать на площадке, ставка 600 руб/час"
    assert detect_category(text) == "helper"


def test_detect_category_promoter_not_helper():
    text = "Нужны промоутеры на раздачу листовок, метро Таганская"
    assert detect_category(text) == "promoter"


def test_detect_category_promo_position_caps():
    text = (
        "На мероприятие требуются сотрудники: ПРОМО(ДЕВУШКИ И МОЛОДЫЕ ЛЮДИ)\n"
        "Позиция: ПРОМО\n"
        "Помощь на площадке"
    )
    assert detect_category(text) == "promoter"


def test_detect_category_loader_factory_not_helper():
    text = (
        "Работа на производстве крупы на фасовочном конвейере. "
        "Выгрузка и перемещение фур, складирование и укладка готовой продукции. "
        "Работа с тележкой и рохлей."
    )
    assert detect_category(text) == "loader"


def test_detect_category_supervisor_not_wedding_coordinator():
    text = (
        "1. Ищем #организатора-#координатора Свадеб, с опытом работы заграницей\n"
        "2.06.06. 16:30-18:30 #аниматор CHALLENGE PARTY"
    )
    assert detect_category(text) == "animator"


def test_detect_category_supervisor_real():
    text = "ТРЕБУЕТСЯ СУПЕРВАЙЗЕР С АВТО. Контроль промо-персонала."
    assert detect_category(text) == "supervisor"


def test_detect_category_anketirovanie_promoter():
    text = (
        "3 июня АНКЕТИРОВАНИЕ (несложная анкета от продуктового бренда)\n"
        "550 р/час с 11 до 19.00\n"
        "Нужны супер активные промо"
    )
    assert detect_category(text) == "promoter"


def test_is_helper_message_rejects_unpaid_massovka():
    from parser import is_helper_message

    text = (
        "Требуется Массовка\n"
        "О вакансии: Ищем массовку для курсовой работы\n"
        "Оплата 💵 Нет"
    )
    ok, reason, _ = is_helper_message(text)
    assert ok is False
    assert reason in ("unpaid", "massovka_film")


def test_fuzzy_duplicate_anketirovanie_same_author(monkeypatch):
    base = (
        "3 июня АНКЕТИРОВАНИЕ (несложная анкета от продуктового бренда)\n"
        "550 р/час с 11 до 19.00\n"
        "Проспект победы 114\n"
        "Для отклика @Fd4Daria"
    )
    variant = (
        "ЗАВТРА! 3 июня АНКЕТИРОВАНИЕ (несложная анкета от продуктового бренда)\n"
        "550 р/час с 11 до 19.00\n"
        "Русаковская 31\n"
        "Для отклика @Fd4Daria"
    )

    def fake_exact_duplicate(*args, **kwargs):
        return False

    def fake_recent(*args, **kwargs):
        return [{"id": "old", "message_text": base, "author_contact": "@Fd4Daria", "dedupe_key": "x"}]

    monkeypatch.setattr("parser.has_recent_duplicate_vacancy", fake_exact_duplicate)
    monkeypatch.setattr("parser.get_recent_open_vacancies_for_dedupe", fake_recent)

    duplicate_type = detect_duplicate_type(variant, "@Fd4Daria", "new_key")
    assert duplicate_type in ("fuzzy", "campaign")


def test_order_number_duplicate_cross_chat(monkeypatch):
    base = (
        "Грузчики МОСКВА\n"
        "№279697\n"
        "Нужны 2 грузчика на разгрузку, оплата 500 р/ч\n"
        "Создано заказов: 12\n"
        "@boss_moscow"
    )
    repost = (
        "HelpersTeam\n"
        "Автопost №279697\n"
        "❌ Закрыто\n"
        "2 грузчика, 500 руб/час\n"
        "@boss_moscow"
    )

    def fake_exact_duplicate(*args, **kwargs):
        return False

    def fake_recent(*args, **kwargs):
        return [{
            "id": "old",
            "message_text": base,
            "author_contact": "@boss_moscow",
            "dedupe_key": "x",
            "source_chat_title": "Грузчики МОСКВА",
            "category_code": "loader",
        }]

    monkeypatch.setattr("parser.has_recent_duplicate_vacancy", fake_exact_duplicate)
    monkeypatch.setattr("parser.get_recent_open_vacancies_for_dedupe", fake_recent)

    duplicate_type = detect_duplicate_type(
        repost,
        "@boss_moscow",
        "new_key",
        "loader",
        "HelpersTeam",
    )
    assert duplicate_type == "order_number"


def test_username_duplicate_cross_chat(monkeypatch):
    base = (
        "Нужны грузчики на завтра к 10:00, ставка 480 р/ч\n"
        "Контакт @same_boss"
    )
    repost = (
        "HelpersTeam repost\n"
        "Грузчики завтра 10-00, 480 руб час\n"
        "@same_boss"
    )

    def fake_exact_duplicate(*args, **kwargs):
        return False

    def fake_recent(*args, **kwargs):
        return [{
            "id": "old",
            "message_text": base,
            "author_contact": "@same_boss",
            "dedupe_key": "x",
            "source_chat_title": "Грузчики МОСКВА",
            "category_code": "loader",
        }]

    monkeypatch.setattr("parser.has_recent_duplicate_vacancy", fake_exact_duplicate)
    monkeypatch.setattr("parser.get_recent_open_vacancies_for_dedupe", fake_recent)

    duplicate_type = detect_duplicate_type(
        repost,
        "@same_boss",
        "new_key",
        "loader",
        "HelpersTeam",
    )
    assert duplicate_type in ("fuzzy", "campaign")


def test_metro_filter_matches_station():
    text = "Срочно нужен промоутер, метро Таганская, ставка 3500"
    assert extract_metro_tokens(text) == ["таганская"]
    assert vacancy_matches_user_metro(text, None, "Таганская, Сокол") is True
    assert vacancy_matches_user_metro(text, None, "Беляево") is False
    assert vacancy_matches_user_metro(text, None, "") is True


def test_format_parser_chats_report():
    from parser import format_parser_chats_report

    report = format_parser_chats_report([], "empty")
    assert "Чаты парсинга" in report
    assert "/addchat" in report

    long_title = "Персонал ресторана: Администраторы,директора,хостес_кальянщик"
    report2 = format_parser_chats_report(
        [{"status": "ok", "title": long_title, "chat_link": "@test", "chat_id": "-1001", "monitored": True}],
        "online",
    )
    assert long_title in report2
    assert "<b>" in report2


def test_numbered_annotation_list_not_split_as_digest():
    from parser import should_split_digest, split_vacancy_blocks

    stolyarny = (
        "Помощь на площадке;\n"
        "Адрес | Столярный пер.3 корп.17в\n"
        "Оплата за проект: 1500\n"
        "Смена | на сегодня, с 17:30\n"
        "1. 16+\n"
        "2. Проверка билетов.\n"
        "3. СРОЧНО!\n"
        "@boss"
    )
    assert should_split_digest(stolyarny) is False
    assert len(split_vacancy_blocks(stolyarny)) == 1 or should_split_digest(stolyarny) is False


def test_session_file_path_default():
    from parser import session_file_path
    assert session_file_path().endswith(".session")


def test_make_vacancy_id_same_for_parser_and_send():
    from parser import make_vacancy_id

    chat_id, message_id = "-100123456", "42"
    dedupe_key = "phone:79161234567|hash:abc"
    assert make_vacancy_id(chat_id, message_id, dedupe_key) == make_vacancy_id(chat_id, message_id, dedupe_key)
    assert make_vacancy_id(chat_id, message_id, dedupe_key) != make_vacancy_id(chat_id, message_id, None)
    assert len(make_vacancy_id(chat_id, message_id)) == 16


def test_is_vacancy_closed_text_detects_zakryto_block():
    text = (
        "Требуется 1 человек к 13:30\n"
        "Ставка 500р/час\n"
        "@egorwave\n\n"
        "ЗАКРЫТО❌❌❌❌❌"
    )
    assert is_vacancy_closed_text(text) is True


def test_is_vacancy_closed_text_open_vacancy_with_urgent_emoji():
    text = "СРОЧНО К 13:30❗️\nТребуется грузчик\nСтавка 500р/час\n@egorwave"
    assert is_vacancy_closed_text(text) is False


def test_is_strikethrough_closure_detects_struck_job_header():
    text = (
        "❗️РАБОТА НА МЕСЯЦ❗️\n"
        "❗️05.06.2025❗️\n"
        " С 9 утра до 18\n"
        "📞@aronnepalazzi"
    )
    header = "❗️РАБОТА НА МЕСЯЦ❗️\n❗️05.06.2025❗️\n С 9 утра до 18"
    entities = [_FakeStrike(0, len(header))]
    assert is_strikethrough_closure(text, entities) is True


def test_chat_id_aliases_and_monitored():
    import parser as parser_module

    aliases = chat_id_aliases("-1002130334767")
    assert "-1002130334767" in aliases
    assert "2130334767" in aliases

    parser_module._monitored_chat_ids = aliases
    assert is_chat_monitored("2130334767") is True
    assert is_chat_monitored(-1002130334767) is True
    parser_module._monitored_chat_ids = set()
