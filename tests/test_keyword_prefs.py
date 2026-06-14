"""Tests for Premium keyword filters."""

from services.filter_prefs import default_prefs, normalize_prefs
from services.subscriber_match import vacancy_matches_subscriber
from services.text_keyword_prefs import parse_keyword_list, vacancy_matches_keyword_prefs


def test_parse_keyword_list_dedupes():
    assert parse_keyword_list("зоопарк, Зоопарк; выставка") == ["зоопарк", "выставка"]


def test_keyword_include_requires_phrase():
    prefs = normalize_prefs({
        "keywords": {"include": ["zoo"], "exclude": []},
    })
    assert vacancy_matches_keyword_prefs("job at the zoo park", prefs) is True
    assert vacancy_matches_keyword_prefs("regular promo shift", prefs) is False


def test_keyword_exclude_blocks():
    prefs = normalize_prefs({
        "keywords": {"include": [], "exclude": ["vip"]},
    })
    assert vacancy_matches_keyword_prefs("day shift promo", prefs) is True
    assert vacancy_matches_keyword_prefs("vip lounge hostess", prefs) is False


def test_subscriber_match_keywords_reason():
    vac = {"message_text": "promo at exhibition", "category_code": "promoter"}
    prefs = default_prefs()
    prefs["keywords"] = {"include": ["zoo"], "exclude": []}
    ok, reason = vacancy_matches_subscriber(vac, prefs)
    assert ok is False
    assert reason == "keywords"
