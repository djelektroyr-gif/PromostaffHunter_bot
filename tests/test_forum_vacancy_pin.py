"""Тесты pin вакансии в General."""

import pytest

from db import (
    clear_general_vacancy_pin,
    get_general_vacancy_pin,
    init_db,
    set_general_vacancy_pin,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()


def test_general_vacancy_pin_roundtrip():
    set_general_vacancy_pin(42, 100, "vac_abc", "<b>test</b>")
    pin = get_general_vacancy_pin(42)
    assert pin["message_id"] == 100
    assert pin["vacancy_id"] == "vac_abc"
    assert "test" in pin["card_text"]
    clear_general_vacancy_pin(42)
    assert get_general_vacancy_pin(42) is None
