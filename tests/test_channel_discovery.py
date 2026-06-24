"""Фильтры channel discovery."""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_channel_discovery.py"
_spec = importlib.util.spec_from_file_location("run_channel_discovery", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)


def test_load_queries_skips_comments():
    queries = _mod._load_queries(_SCRIPT.parent / "channel_discovery_queries.txt")
    assert queries
    assert not any(q.startswith("#") for q in queries)
    assert "хелпер мероприятие москва" in queries


def test_filter_discovery_rows_min_members():
    rows = [
        {"title": "Event Jobs", "members": 9, "link": "https://t.me/a"},
        {"title": "Big Event", "members": 500, "link": "https://t.me/b"},
        {"title": "Unknown", "members": None, "link": "https://t.me/c"},
    ]
    kept, stats = _mod._filter_discovery_rows(rows, min_members=50)
    assert len(kept) == 2
    assert stats["low_members"] == 1
    assert kept[0]["title"] == "Big Event"


def test_filter_discovery_rows_junk_title():
    rows = [{"title": "WB скидки каждый день", "members": 1000, "link": "https://t.me/x"}]
    kept, stats = _mod._filter_discovery_rows(rows, min_members=50)
    assert kept == []
    assert stats["junk_title"] == 1
