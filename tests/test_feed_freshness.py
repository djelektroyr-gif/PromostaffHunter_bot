from datetime import datetime, timedelta, timezone

from parser import vacancy_matches_category, is_unpaid_vacancy


def test_vacancy_matches_category_helper_loader():
    text = "Завтра нужны 3 грузчика на выгрузку, 500 р/ч, минималка 4ч, @boss1"
    assert vacancy_matches_category(text, "loader") is True
    assert vacancy_matches_category(text, "helper") is False


def test_vacancy_matches_category_rejects_unpaid():
    text = "**Оплата** 💵 Нет\n**Требуется** массовка\n@user"
    assert is_unpaid_vacancy(text) is True
    assert vacancy_matches_category(text, "animator") is False


def test_vacancy_matches_category_promo_not_helper():
    text = (
        "ЗАВТРА! АНКЕТИРОВАНИЕ\n550 р/час\n"
        "✨Нужны супер активные промо ✨\n@manager"
    )
    assert vacancy_matches_category(text, "promoter") is True
    assert vacancy_matches_category(text, "helper") is False


def test_get_freshness_label_old_post():
    import main as main_module

    old = datetime.now(timezone.utc) - timedelta(days=3)
    label = main_module.get_freshness_label(old.strftime("%Y-%m-%d %H:%M:%S"))
    assert "3 дн." in label


def test_vacancy_in_feed_mode_fresh_vs_archive():
    import main as main_module

    now = datetime.now(timezone.utc)
    fresh_vac = {"published_at": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}
    old_vac = {"published_at": (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")}
    assert main_module._vacancy_in_feed_mode(fresh_vac, "fresh") is True
    assert main_module._vacancy_in_feed_mode(fresh_vac, "archive") is False
    assert main_module._vacancy_in_feed_mode(old_vac, "fresh") is False
    assert main_module._vacancy_in_feed_mode(old_vac, "archive") is True
