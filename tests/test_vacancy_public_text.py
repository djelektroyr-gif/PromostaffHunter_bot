"""Тесты публичного текста вакансии."""

from services.vacancy_public_text import sanitize_vacancy_public_body


def test_sanitize_strips_group_bot_header_and_contacts():
    raw = """⭐️VIP⭐️№279660 👉 Дмитрий
@Rabota_moskva1394
Создано заказов: 1345
Зарегистрирован: 974 дня назад

На завтра 🔥
к 16:00⏰
Нужен 1 промоутер ✅
У метро Смоленская ✅
Оплата 2000 💶
4 часа 💶
☎️Дмитрий
@Rabota_moskva1394"""
    out = sanitize_vacancy_public_body(raw)
    assert "@" not in out
    assert "279660" not in out
    assert "Создано заказов" not in out
    assert "промоутер" in out.lower()
    assert "Смоленская" in out
    assert "2000" in out


def test_sanitize_keeps_job_details():
    raw = (
        "⚡️ РАЗНОРАБОЧИЕ ЗАВТРА СРОЧНО ⚡️\n"
        "🚇 м. Спартак\n"
        "💵 3 500 руб сразу после смены!\n"
        "✍️записаться: @Alina_manager07"
    )
    out = sanitize_vacancy_public_body(raw)
    assert "Спартак" in out
    assert "3500" in out or "3 500" in out
    assert "@" not in out
    assert "записаться" not in out.lower()
