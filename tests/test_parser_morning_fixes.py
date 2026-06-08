"""Регрессии по утренней ленте 08.06.2026."""

from parser import (
    detect_category,
    detect_duplicate_type,
    evaluate_vacancy,
    is_digital_work_spam,
    is_massovka_or_film_extras,
    is_permanent_job_spam,
)


def test_massovka_clip_rejected_not_animator():
    text = (
        "‼️ МОСКВА. МАССОВКА на съемки клипа (9 ИЮНЯ) ‼️\n"
        "💰 Оплата: 500 ₽ за проект (сразу после съемок )\n"
        "ФИО\n"
        "@casting_manager"
    )
    assert is_massovka_or_film_extras(text) is True
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "massovka_film"
    assert detect_category(text) != "animator" or not ok


def test_driver_pickup_with_loader_is_loader():
    text = (
        "На сегодня 13:00\n"
        "1 грузчик( всего два)\n"
        "Метро румянцево ,водитель заберет\n"
        "Ездить по заправкам забирать металл\n"
        "450/4/1800\n"
        "@logist"
    )
    ok, cat, _, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "loader"


def test_digital_helper_spam_rejected():
    text = (
        "📱 **Удаленная подработка: Помощник / Ассистент (со смартфона)**\n"
        "Работа с готовым контентом. Публиковать материалы по шаблону.\n"
        "Оплата 5000 р/мес\n"
        "@remote_boss"
    )
    assert is_digital_work_spam(text) is True
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "digital_work_spam"


def test_permanent_loader_job_rejected():
    text = (
        "На постоянную основу нужен грузчик. В пару к водителю.\n"
        "Оплата 2 раза в месяц, в среднем 70 000 - 85 000 месяц.\n"
        "Метро Румянцево\n"
        "@hr_permanent"
    )
    assert is_permanent_job_spam(text) is True
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "permanent_job"


def test_event_helper_with_unload_prefers_helper():
    text = (
        "📍ул. Вильгельма Пика, 16\n"
        "с 8 до 19:00\n"
        "550 р/час\n"
        "Требуются хелперы ( разгрузить, расставить, приготовить площадку к мероприятию)\n"
        "☎️@Fd4Daria"
    )
    ok, cat, _, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "helper"


def test_campaign_duplicate_same_channel(monkeypatch):
    base = (
        "🇷🇺РАЗДАЧА ЛИСТОВОК С РЕЧЕВКОЙ🇷🇺 10-21 ИЮНЯ\n"
        "⚡️Длительный проект⚡️\n"
        "💸2500 руб смена💸\n"
        "ФИО, возраст\n"
        "@promo1"
    )
    variant = (
        "🇷🇺РАЗДАЧА ЛИСТОВОК С РЕЧЕВКОЙ🇷🇺 10-21 ИЮНЯ\n"
        "⚡️Длительный проект⚡️\n"
        "📍ул. Маршала Бирюзова, д. 17\n"
        "💸2500 руб смена💸\n"
        "@promo2"
    )

    def fake_exact(*_a, **_k):
        return False

    def fake_recent(*_a, **_k):
        return [{
            "id": "v1",
            "message_text": base,
            "author_contact": "@promo1",
            "dedupe_key": "k1",
            "source_chat_title": "Promo Channel",
            "category_code": "promoter",
        }]

    monkeypatch.setattr("parser.has_recent_duplicate_vacancy", fake_exact)
    monkeypatch.setattr("parser.get_recent_open_vacancies_for_dedupe", fake_recent)

    dup = detect_duplicate_type(
        variant, "@promo2", "k2", "promoter", "Promo Channel",
    )
    assert dup == "campaign"
