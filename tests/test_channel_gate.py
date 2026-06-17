# -*- coding: utf-8 -*-

from services.channel_gate import is_channel_env_configured, is_vacancy_channel_autopost_enabled


def test_autopost_requires_env_and_db(monkeypatch):
    monkeypatch.setenv("CHANNEL_CROSSPOST_ENABLED", "1")
    monkeypatch.setenv("HUNTER_CHANNEL_ID", "-100123")
    import importlib
    import config

    importlib.reload(config)
    import services.channel_gate as gate

    importlib.reload(gate)
    monkeypatch.setattr("db.is_channel_crosspost_enabled", lambda: True)
    assert gate.is_channel_env_configured() is True
    assert gate.is_vacancy_channel_autopost_enabled() is True

    monkeypatch.setattr("db.is_channel_crosspost_enabled", lambda: False)
    assert gate.is_vacancy_channel_autopost_enabled() is False


def test_autopost_off_when_env_disabled(monkeypatch):
    monkeypatch.setenv("CHANNEL_CROSSPOST_ENABLED", "0")
    monkeypatch.setenv("HUNTER_CHANNEL_ID", "-100123")
    import importlib
    import config

    importlib.reload(config)
    import services.channel_gate as gate

    importlib.reload(gate)
    monkeypatch.setattr("db.is_channel_crosspost_enabled", lambda: True)
    assert gate.is_channel_env_configured() is False
    assert gate.is_vacancy_channel_autopost_enabled() is False
