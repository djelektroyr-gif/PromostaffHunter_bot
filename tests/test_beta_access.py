"""Мягкий запуск: UI без разблокировки лимитов."""

from unittest.mock import patch


def test_limits_stay_on_free_tier():
    with (
        patch("services.beta_access.is_user_premium", return_value=False),
        patch("services.beta_access.FREE_CATEGORY_LIMIT", 1),
    ):
        from services.beta_access import effective_free_category_limit, vacancy_push_enabled_for_user

        assert effective_free_category_limit() == 1
        assert vacancy_push_enabled_for_user(1) is False


def test_premium_keeps_push():
    with patch("services.beta_access.is_user_premium", return_value=True):
        from services.beta_access import vacancy_push_enabled_for_user

        assert vacancy_push_enabled_for_user(1) is True
