"""P0 + P1 tail tests."""

from parser import (
    detect_category,
    evaluate_vacancy,
    evaluate_digest_blocks,
    extract_contact_from_text,
    is_casting_call,
    is_job_post_for_staff,
    is_mixed_digest_post,
    is_service_request,
    passes_quality_gate,
    should_split_digest,
    vacancy_matches_category,
)


def _loader_vacancy():
    return (
        "Балашиха, нужны: 1 человек(ка) (ТОЛЬКО 18+)\n"
        "Что делать? - Помощь кладовщику при разгрузке и погрузке\n"
        "Оплата: 467 руб. час по самозанятости; минималка: 4 часа\n"
        "☎️@nataliapglav"
    )


def _promo_anketa():
    return (
        "3 июня АНКЕТИРОВАНИЕ (несложная анкета от продуктового бренда)\n"
        "550 р/час с 11 до 19.00\n"
        "✨Нужны супер активные промо ✨\n"
        "Для отклика @Fd4Daria"
    )


def _unpaid_massovka():
    return (
        "**Требуется** 🔎 Массовка\n"
        "**Оплата** 💵 Нет\n"
        "**Контакт** ✉️ @Vova_Budaev"
    )


def _event_hunter_digest():
    return (
        "**1. 06.06. 16:30-18:30 ****#Аниматор****\n"
        "CHALLENGE PARTY\n"
        "Аниматор: 6500р + костюм.\n\n"
        "2. 💢 Нужен парень #помощник мужчина\n"
        "Приехать на месте помочь девочке аниматору\n"
        "500 р/ч\n"
        "Контакт @boss1"
    )


def _quest_marketplace():
    return (
        "2. МО, Красногорск 7 июня 15.00\n"
        "Ищу зомби-#квест на 1,5 часа\n"
        "Бюджет 12000 рублей\n"
        "📝Присылайте программу vk.com/natalyborets/"
    )


def test_evaluate_accepts_loader_with_payment_and_contact():
    ok, cat, reason, _ = evaluate_vacancy(_loader_vacancy())
    assert ok is True
    assert cat == "loader"
    assert reason == "accepted"


def test_evaluate_accepts_promoter_not_helper():
    ok, cat, _, _ = evaluate_vacancy(_promo_anketa())
    assert ok is True
    assert cat == "promoter"
    assert vacancy_matches_category(_promo_anketa(), "promoter") is True
    assert vacancy_matches_category(_promo_anketa(), "helper") is False


def test_evaluate_rejects_unpaid():
    ok, cat, reason, _ = evaluate_vacancy(_unpaid_massovka())
    assert ok is False
    assert cat is None
    assert reason == "unpaid"


def test_digest_split_required_for_full_post():
    assert should_split_digest(_event_hunter_digest()) is True
    assert is_mixed_digest_post(_event_hunter_digest()) is True
    ok, _, reason, _ = evaluate_vacancy(_event_hunter_digest())
    assert ok is False
    assert reason == "digest_split_required"


def test_digest_split_accepts_valid_blocks():
    poster = {"username": "eventboss", "user_id": 1001}
    blocks = evaluate_digest_blocks(_event_hunter_digest(), poster)
    categories = {cat for cat, _ in blocks}
    assert "animator" in categories or "helper" in categories
    assert len(blocks) >= 1


def test_evaluate_rejects_service_request():
    assert is_service_request(_quest_marketplace()) is True
    ok, _, reason, _ = evaluate_vacancy(_quest_marketplace())
    assert ok is False
    assert reason in ("service_request", "no_hiring", "no_contact", "mixed_digest", "stop_phrase: присылайте программу")


def test_detect_category_returns_none_without_fallback():
    assert detect_category("просто текст без роли и оплаты") is None


def test_no_fallback_helper_for_ambiguous_labor_only():
    text = "Работа на производстве крупы на фасовочном конвейере. Рохля, паллет."
    assert detect_category(text) == "loader"
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason in ("no_hiring", "no_payment", "no_contact", "quality_gate:loader")


def test_helper_position_promo_rejected_as_helper():
    text = (
        "На мероприятие требуются сотрудники: ПРОМО\n"
        "Позиция: ПРОМО\n"
        "Помощь на площадке\n"
        "13.06.2026 с 10:00 до 16:00\n"
        "Оплата 500 р/ч\n"
        "@event_manager"
    )
    assert detect_category(text) == "promoter"
    ok, cat, _, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "promoter"
    assert passes_quality_gate("helper", text) is False


def test_is_job_post_for_staff_requires_contact():
    text = "Нужны грузчики, 500 р/ч, оплата сразу"
    ok, reason, _ = is_job_post_for_staff(text)
    assert ok is False
    assert reason == "no_contact"


def test_extract_wa_me_contact():
    assert extract_contact_from_text("Пишите https://wa.me/79991234567") == "https://wa.me/79991234567"
    assert extract_contact_from_text("api.whatsapp.com/send?phone=79991234567") == "https://wa.me/79991234567"


def test_casting_rejected():
    text = "Кастинг моделей на рекламную съёмку, оплата 5000, @castboss1"
    assert is_casting_call(text) is True
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "casting"


def test_payment_slash_triple_format():
    text = (
        "Нужно 2 человека погрузка глины\n"
        "ОПЛАТА 400/4/1600\n"
        "Пишите в лс"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "nzboss"})
    assert ok is True
    assert cat == "loader"
    assert reason == "accepted"


def test_payment_thousands_per_day_promo():
    text = (
        "Требуются промоутеры на смотровую площадку!\n"
        "Заработок от 6 до 15 тысяч рублей в день!\n"
        "Заявки в лс"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "promoter"


def test_ls_phrase_without_username_is_contact():
    text = "Нужны 3 грузчика, оплата 3000, пишите в ЛС"
    ok, reason, _ = is_job_post_for_staff(text)
    assert ok is True
    assert reason == "staff_job"


def test_loader_uborka_two_people():
    text = (
        "На завтра к 9:00\n2 человека\n"
        "Уборка в цеху, на улице.\n"
        "Оплата 4500 на месте\n"
        "@Rabotniki24"
    )
    ok, cat, _, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "loader"


def test_format_chat_noise_report_in_progress():
    from parser import format_chat_noise_report

    text = format_chat_noise_report(
        {
            "by_chat": {},
            "messages_scanned": 120,
            "chats_ok": 5,
            "chats_total": 36,
            "finished_at": None,
        }
    )
    assert "Прогон ещё идёт" in text
    assert "120" in text


def test_format_chat_noise_report_with_by_chat():
    from parser import format_chat_noise_report

    text = format_chat_noise_report(
        {
            "by_chat": {
                "Test Chat": {
                    "scanned": 10,
                    "matched": 2,
                    "rejected": 6,
                    "role_mismatch": 0,
                    "reasons": {"not_staff": 4},
                }
            }
        }
    )
    assert "Test Chat" in text
    assert "шум" in text.lower()


def test_format_reject_samples_report():
    from parser import format_reject_samples_report

    text = format_reject_samples_report(
        {
            "run_kind": "audit",
            "reject_samples": [
                {
                    "chat": "Promo Chat",
                    "reason": "no_contact",
                    "preview": "Нужны промоутеры, 500 р/ч",
                }
            ],
        }
    )
    assert "no_contact" not in text or "контакта" in text
    assert "Promo Chat" in text
    assert "500 р/ч" in text


def test_format_channel_coverage_report():
    from parser import format_channel_coverage_report

    text = format_channel_coverage_report(
        {"by_chat": {"Active": {"matched": 2, "rejected": 5, "scanned": 10}}},
        {"Active": 15, "Silent": 0},
    )
    assert "Active" in text
    assert "15" in text
    assert "Silent" in text or "Молчат" in text


def test_build_admin_parser_help_html():
    from main import build_admin_parser_help_html

    text = build_admin_parser_help_html()
    assert "Аудит фильтра" in text
    assert "Покрытие каналов" in text
    assert "Примеры отсева" in text
