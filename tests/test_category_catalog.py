"""Тесты каталога категорий."""

from services.category_catalog import (
    category_group,
    category_name,
    sort_key_for_category,
)


def test_new_categories_have_groups():
    assert category_group("booth") == "event_prod"
    assert category_group("electrician") == "specialists"
    assert category_group("misc") == "other"
    assert category_group("promoter") == "event"


def test_sort_puts_event_before_specialists():
    assert sort_key_for_category("promoter") < sort_key_for_category("electrician")


def test_display_names():
    assert category_name("host_mc") == "Ведущий"
    assert category_name("electrician") == "Электромонтаж"
