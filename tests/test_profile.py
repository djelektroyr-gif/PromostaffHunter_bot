from db import (
    add_subscriber,
    init_db,
    rebuild_candidate_questionnaire,
    update_resume_extra,
    update_subscriber_profile,
)
from db_backend import execute


def _seed_user(user_id: int = 700001):
    init_db()
    add_subscriber(user_id, "tester", "Test", "User")
    update_subscriber_profile(
        user_id,
        full_name="Иван Петров",
        age=28,
        phone="+79991234567",
        birth_date="01.01.1998",
    )


def test_rebuild_candidate_questionnaire_includes_core_fields():
    _seed_user()
    text = rebuild_candidate_questionnaire(700001)
    assert "Иван Петров" in text
    assert "+79991234567" in text
    assert "28" in text
    assert "01.01.1998" in text


def test_rebuild_candidate_questionnaire_includes_resume_extra():
    _seed_user()
    update_resume_extra(700001, "Рост 180, опыт промо 2 года")
    text = rebuild_candidate_questionnaire(700001)
    assert "Рост 180" in text
    assert "Доп. информация" in text


def test_build_candidate_profile_text_resume_extra():
    import main as main_module

    profile = {
        "full_name": "Иван Петров",
        "age": 28,
        "phone": "+79991234567",
        "username": "tester",
        "resume_extra": "Опыт на выставках",
    }
    text = main_module.build_candidate_profile_text(profile)
    assert "Опыт на выставках" in text
    assert "Иван Петров" in text


def test_settings_markup_has_disable_feed():
    import main as main_module

    user_id = 700010
    _seed_user(user_id)
    execute("DELETE FROM user_categories WHERE user_id = ?", (user_id,))
    execute(
        "INSERT INTO user_categories (user_id, category_code) VALUES (?, ?)",
        (user_id, "helper"),
    )
    markup = main_module.build_categories_markup(["helper"], user_id)
    callbacks = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
    ]
    assert "disable_feed" in callbacks


def test_toggle_user_category_atomic():
    from db import get_user_categories, toggle_user_category

    user_id = 700011
    _seed_user(user_id)
    execute("DELETE FROM user_categories WHERE user_id = ?", (user_id,))

    codes, blocked = toggle_user_category(user_id, "helper", free_limit=3)
    assert not blocked
    assert codes == ["helper"]
    assert [c["code"] for c in get_user_categories(user_id)] == ["helper"]

    codes, blocked = toggle_user_category(user_id, "helper", free_limit=3)
    assert not blocked
    assert codes == []
    assert get_user_categories(user_id) == []

    toggle_user_category(user_id, "helper", free_limit=3)
    toggle_user_category(user_id, "promoter", free_limit=3)
    toggle_user_category(user_id, "wardrobe", free_limit=3)
    codes, blocked = toggle_user_category(user_id, "loader", free_limit=3)
    assert blocked
    assert len(codes) == 3
    assert "loader" not in codes


def test_main_keyboard_uses_new_button_labels():
    import main as main_module

    _seed_user(700003)
    keyboard, _ = main_module.get_main_keyboard(700003)
    labels = [btn.text for row in keyboard.keyboard for btn in row]
    assert main_module.BTN_SETTINGS in labels
    assert main_module.BTN_MY_DATA in labels
    assert main_module.BTN_UNSUB_LEGACY not in labels
