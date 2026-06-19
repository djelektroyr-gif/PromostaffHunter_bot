# -*- coding: utf-8 -*-
"""Digest с разными профессиями и контактами в одном посте."""

from parser import (
    evaluate_digest_blocks,
    evaluate_vacancy,
    extract_contact_from_text,
    should_split_digest,
    split_vacancy_blocks,
    vacancy_matches_category,
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


_DIGEST_BOLD_DIGEST_SAMPLE = (
    "**1. 27 июня. ****#Диджей**** с аппаратурой. на 6 часов город Видное. МО.\n"
    "бюджет 25 000\n"
    "#ВИДЕОМЕЙКER\n"
    "27.08.26 на свадьбу требуется видеомейкер - рилсмейкер.\n"
    "📝 vk.com/idermakovavi\n\n"
    "2.Лоукост На завтра 20.06 нужен #ведущий на годовщину свадьбы.\n"
    "📝 vk.com/alexmers\n\n"
    "3.Лоукост Ищу на завтра, 20 июня, #хелперов на квиз.\n"
    "📝 https://vk.ru/id246738778"
)


def test_bold_numbered_digest_splits():
    text = _DIGEST_BOLD_DIGEST_SAMPLE
    blocks = split_vacancy_blocks(text)
    assert len(blocks) >= 3
    assert should_split_digest(text) is True


def test_bold_digest_blocks_separate_roles():
    text = _DIGEST_BOLD_DIGEST_SAMPLE
    accepted = evaluate_digest_blocks(text)
    categories = {cat for cat, _ in accepted}
    assert "dj" in categories or "host_mc" in categories
    assert "helper" in categories


def test_animator_in_restaurant_not_waiter():
    text = (
        "**Аниматор в ресторан 20 и 21 июня 18+**\n"
        "📍 метро Ольховая\n"
        "Костюм и реквизит на месте\n"
        "❤️м.Ольховая 13-20ч СБ Оплата 2300р\n"
        "👉 @Elen_250182"
    )
    ok, cat, _, _ = evaluate_vacancy(text, {"username": "Elen_250182", "user_id": 1})
    assert ok is True
    assert cat == "animator"
    assert vacancy_matches_category(text, "animator") is True
    assert vacancy_matches_category(text, "waiter") is False

