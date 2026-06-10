import importlib.util
from pathlib import Path

# build_contact_link живёт в main.py — подгружаем без полного импорта бота
_main_path = Path(__file__).resolve().parents[1] / "main.py"
_spec = importlib.util.spec_from_file_location("hunter_main_contact", _main_path)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
build_contact_link = _mod.build_contact_link


def test_username_link():
    url = build_contact_link("@promostaffagency", "Привет")
    assert url == "https://t.me/promostaffagency?text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82"


def test_tg_user_id_no_button():
    assert build_contact_link("tg://user?id=123456789", "Hi") is None


def test_phone_no_inline_button():
    """Bot API не принимает tel: — только ручной отклик с черновиком в тексте."""
    assert build_contact_link("+7 916 123-45-67", "Hi") is None
    assert build_contact_link("89254807851", "Привет") is None


def test_wa_me_explicit_link_with_text():
    assert (
        build_contact_link("https://wa.me/79001234567", "Hi")
        == "https://wa.me/79001234567?text=Hi"
    )


def test_airtable_form_link_passthrough():
    url = "https://airtable.com/appglH7lKHqV99EIi/shrSX4Drh5gna7MkC"
    assert build_contact_link(url, "draft") == url


def test_maps_url_not_button():
    assert build_contact_link("https://yandex.ru/maps/-/CPvW7RjJ", "Hi") is None


def test_invalid_username():
    assert build_contact_link("@ab", "Hi") is None
