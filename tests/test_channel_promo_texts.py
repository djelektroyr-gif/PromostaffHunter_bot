import json
from pathlib import Path

import pytest

from services import channel_promo_texts as cpt


@pytest.fixture
def promo_file(tmp_path, monkeypatch):
    path = tmp_path / "channel_promo_texts.json"
    monkeypatch.setattr(cpt, "get_promo_texts_file_candidates", lambda: [path])
    return path


def test_default_when_no_file_no_db(monkeypatch, promo_file):
    monkeypatch.setattr(cpt, "get_channel_promo_texts_from_db", lambda: None)
    assert promo_file.exists() is False
    variants = cpt.get_promo_variants()
    assert variants == cpt.DEFAULT_PROMO_VARIANTS
    assert cpt.get_promo_texts_source() == "default"


def test_load_from_file(monkeypatch, promo_file):
    monkeypatch.setattr(cpt, "get_channel_promo_texts_from_db", lambda: None)
    promo_file.write_text(
        json.dumps({"variants": ["<b>A</b>", "<b>B</b>"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert cpt.get_promo_variants() == ["<b>A</b>", "<b>B</b>"]
    assert cpt.get_promo_texts_source() == "file"


def test_db_overrides_file(monkeypatch, promo_file):
    promo_file.write_text(json.dumps({"variants": ["file"]}), encoding="utf-8")
    monkeypatch.setattr(cpt, "get_channel_promo_texts_from_db", lambda: ["<b>DB</b>"])
    assert cpt.get_promo_variants() == ["<b>DB</b>"]
    assert cpt.get_promo_texts_source() == "db"


def test_pick_promo_text_cycles(monkeypatch):
    monkeypatch.setattr(cpt, "get_promo_variants", lambda: ["one", "two"])
    assert cpt.pick_promo_text(0) == "one"
    assert cpt.pick_promo_text(1) == "two"
    assert cpt.pick_promo_text(2) == "one"


def test_import_and_reset(monkeypatch, promo_file):
    saved = {}

    def fake_save(variants):
        saved["v"] = variants
        return variants

    monkeypatch.setattr(cpt, "set_channel_promo_texts_in_db", fake_save)
    monkeypatch.setattr(cpt, "clear_channel_promo_texts_override", lambda: saved.pop("v", None))
    monkeypatch.setattr(cpt, "get_channel_promo_texts_from_db", lambda: saved.get("v"))
    promo_file.write_text(json.dumps({"variants": ["x", "y", "z"]}), encoding="utf-8")

    variants, err = cpt.import_promo_from_file_to_db()
    assert err == ""
    assert variants == ["x", "y", "z"]

    monkeypatch.setattr(cpt, "load_promo_variants_from_file", lambda: ["x", "y", "z"])
    out, source = cpt.reset_promo_texts_to_file_or_defaults()
    assert source == "file"
    assert out == ["x", "y", "z"]


def test_bundle_file_in_repo():
    assert cpt.BUNDLE_PROMO_TEXTS_FILE.is_file()
    variants = cpt._load_promo_variants_from_path(cpt.BUNDLE_PROMO_TEXTS_FILE)
    assert variants
    assert len(variants) >= 3
