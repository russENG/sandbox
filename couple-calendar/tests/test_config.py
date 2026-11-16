from __future__ import annotations

from couple_calendar.config import load_settings


def _clear(monkeypatch):
    for key in ("CONCIERGE_NAME", "GROUP_TRIGGER_WORDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "shared@group.calendar.google.com")


def test_default_name_is_used_as_group_call(monkeypatch):
    _clear(monkeypatch)
    settings = load_settings()
    assert settings.concierge_name == "りまいんたろう"
    assert settings.group_trigger_words == ("りまいんたろう",)


def test_name_and_calls_can_be_changed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CONCIERGE_NAME", "かれんだー係")
    assert load_settings().group_trigger_words == ("かれんだー係",)
    monkeypatch.setenv("GROUP_TRIGGER_WORDS", "りまいんたろう,りまいん")
    assert load_settings().group_trigger_words == ("りまいんたろう", "りまいん")
