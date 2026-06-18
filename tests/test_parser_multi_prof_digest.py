# -*- coding: utf-8 -*-
"""Digest с разными профессиями и контактами в одном посте."""

from parser import (
    evaluate_digest_blocks,
    evaluate_vacancy,
    extract_contact_from_text,
    should_split_digest,
    split_vacancy_blocks,
)


def _multi_prof_digest():
    return (
        "1. #Диджей с оборудованием (звук, микрофоны, музыка) на субботу 20.06 "
        "теплоход Москва прогулка 5 часов, начало с 16-17 часов 12.500\n"
        "📝 vk.com/antonlyavo\n\n"
        "2.20 июня  Нужен #аниматор на детский день рождения \n"
        "Активная анимация  Ресторан \n"
        "Оплата 10000 т.р. \n"
        "С 18:30 -21:30\n"
        "📝 https://vk.ru/id246738778\n\n"
        "3. ТРЕБУЕТСЯ #ДИДЖЕЙ НА ВЫПУСКНЫЕ 9 КЛАССОВ (25, 26, 27 ИЮНЯ) \n"
        "Оплата: 5 000 ₽ за один день работы. \n"
        "📝 vk.com/k.holodina\n\n"
        "4. СРОЧНО! ТРЕБУЕТСЯ #КАВЕРГРУППА (СВАДЕБНЫЙ ФОРМАТ) \n"
        "Оплата: 25 000 ₽ за выступление \n"
        "📝 vk.com/k.holodina"
    )


def test_multi_prof_digest_splits_four_blocks():
    text = _multi_prof_digest()
    blocks = split_vacancy_blocks(text)
    assert len(blocks) == 4
    assert should_split_digest(text) is True
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "digest_split_required"


def test_multi_prof_digest_each_block_own_category_and_contact():
    text = _multi_prof_digest()
    accepted = evaluate_digest_blocks(text)
    categories = {cat for cat, _ in accepted}
    assert "dj" in categories
    assert "animator" in categories
    assert len(accepted) >= 3

    contacts = set()
    for _cat, block in accepted:
        c = extract_contact_from_text(block)
        if c:
            contacts.add(c)
    assert "https://vk.com/antonlyavo" in contacts
    assert any("246738778" in c for c in contacts)
    assert any("k.holodina" in c for c in contacts)


def test_vk_contact_without_scheme():
    assert extract_contact_from_text("📝 vk.com/antonlyavo") == "https://vk.com/antonlyavo"
    assert extract_contact_from_text("https://vk.ru/id246738778") == "https://vk.ru/id246738778"
