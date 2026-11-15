from __future__ import annotations

import json
from datetime import date, datetime, time

import pytest

from couple_calendar.gcal import CalendarError, compute_free_slots
from couple_calendar.tools import build_tools

from .conftest import TZ


@pytest.fixture
def tools(calendar):
    return {fn.__name__: fn for fn in build_tools(calendar)}


def test_calendar_info(tools):
    info = json.loads(tools["get_calendar_info"]())
    assert info["now"] == "2026-11-16T08:00"
    assert info["weekday"] == "月"
    assert info["owners"] == ["たろう", "はなこ", "ふたり"]


def test_create_timed_event_defaults_to_one_hour_and_sets_owner(tools, service):
    out = json.loads(tools["create_event"](title="歯医者", start="2026-11-20T10:00", owner="たろう"))
    created = out["created"]
    assert created["start"] == "2026-11-20T10:00"
    assert created["end"] == "11:00"
    assert created["weekday"] == "金"
    assert created["owner"] == "たろう"
    body = service.store[created["id"]]
    assert body["colorId"] == "9"
    assert body["extendedProperties"]["private"]["coupleOwner"] == "member1"
    assert body["start"]["timeZone"] == "Asia/Tokyo"


def test_create_all_day_uses_inclusive_end_and_is_free_by_default(tools, service):
    out = json.loads(tools["create_event"](title="旅行", start="2026-11-21", end="2026-11-23", all_day=True))
    created = out["created"]
    body = service.store[created["id"]]
    assert body["start"] == {"date": "2026-11-21"}
    assert body["end"] == {"date": "2026-11-24"}  # Google は終了日を含まない
    assert body["transparency"] == "transparent"
    assert created["end"] == "2026-11-23"
    assert created["owner"] == "ふたり"
    assert created["weekday"] == "土〜月"


def test_create_all_day_busy(tools, service):
    out = json.loads(tools["create_event"](title="出張", start="2026-11-25", busy=True, owner="はなこ"))
    assert "transparency" not in service.store[out["created"]["id"]]


def test_create_with_repeat_rule(tools, service):
    out = json.loads(tools["create_event"](title="ゴミ出し", start="2026-11-17T07:30", repeat_rule="FREQ=WEEKLY;BYDAY=TU"))
    assert service.store[out["created"]["id"]]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU"]


def test_unknown_owner_is_rejected(tools):
    with pytest.raises(CalendarError, match="たろう、はなこ、ふたり"):
        tools["create_event"](title="x", start="2026-11-20T10:00", owner="じろう")


def test_end_before_start_is_rejected(tools):
    with pytest.raises(CalendarError):
        tools["create_event"](title="x", start="2026-11-20T10:00", end="2026-11-20T09:00")


def test_list_detects_owner_from_color_and_creator(tools, service):
    service.store["a"] = {
        "id": "a", "summary": "飲み会", "colorId": "4",
        "start": {"dateTime": "2026-11-18T19:00:00+09:00"}, "end": {"dateTime": "2026-11-18T22:00:00+09:00"},
    }
    service.store["b"] = {
        "id": "b", "summary": "ジム", "creator": {"email": "Taro@example.com"},
        "start": {"dateTime": "2026-11-19T07:00:00+09:00"}, "end": {"dateTime": "2026-11-19T08:00:00+09:00"},
    }
    service.store["c"] = {
        "id": "c", "summary": "美容院",
        "start": {"dateTime": "2026-11-19T13:00:00+09:00"}, "end": {"dateTime": "2026-11-19T14:00:00+09:00"},
    }
    out = json.loads(tools["list_events"](start_date="2026-11-18", end_date="2026-11-19"))
    owners = {e["title"]: e["owner"] for e in out["events"]}
    assert owners == {"飲み会": "はなこ", "ジム": "たろう", "美容院": "未設定"}
    assert out["count"] == 3


def test_list_keyword_and_range(tools):
    tools["create_event"](title="映画", start="2026-11-20T18:00")
    tools["create_event"](title="映画", start="2026-12-20T18:00")
    out = json.loads(tools["list_events"](start_date="2026-11-01", end_date="2026-11-30", keyword="映画"))
    assert out["count"] == 1


def test_update_start_keeps_duration(tools, service):
    created = json.loads(tools["create_event"](title="ディナー", start="2026-11-20T19:00", end="2026-11-20T21:00"))["created"]
    updated = json.loads(tools["update_event"](event_id=created["id"], start="2026-11-21T18:30"))["updated"]
    assert updated["start"] == "2026-11-21T18:30"
    assert updated["end"] == "20:30"
    assert updated["title"] == "ディナー"


def test_update_owner_changes_color(tools, service):
    created = json.loads(tools["create_event"](title="病院", start="2026-11-20T10:00", owner="たろう"))["created"]
    tools["update_event"](event_id=created["id"], owner="はなこ")
    assert service.store[created["id"]]["colorId"] == "4"
    assert service.store[created["id"]]["extendedProperties"]["private"]["coupleOwner"] == "member2"


def test_update_timed_to_all_day(tools, service):
    created = json.loads(tools["create_event"](title="記念日", start="2026-11-23T10:00"))["created"]
    updated = json.loads(tools["update_event"](event_id=created["id"], all_day=True))["updated"]
    assert updated["all_day"] is True
    assert updated["start"] == "2026-11-23" and updated["end"] == "2026-11-23"
    assert "dateTime" not in service.store[created["id"]]["start"]


def test_update_without_changes_is_rejected(tools):
    created = json.loads(tools["create_event"](title="x", start="2026-11-20T10:00"))["created"]
    with pytest.raises(CalendarError):
        tools["update_event"](event_id=created["id"])


def test_delete_and_missing_event(tools, service):
    created = json.loads(tools["create_event"](title="x", start="2026-11-20T10:00"))["created"]
    out = json.loads(tools["delete_event"](event_id=created["id"]))
    assert out["deleted"]["title"] == "x"
    assert created["id"] not in service.store
    with pytest.raises(CalendarError, match="見つかりません"):
        tools["delete_event"](event_id=created["id"])


def test_find_free_time_for_both_and_for_one(tools, service):
    tools["create_event"](title="たろう会議", start="2026-11-17T10:00", end="2026-11-17T12:00", owner="たろう")
    tools["create_event"](title="はなこ習い事", start="2026-11-17T14:00", end="2026-11-17T15:00", owner="はなこ")
    tools["create_event"](title="ゴミの日", start="2026-11-17", all_day=True)  # 終日・空き扱い
    service.personal_busy["taro@example.com"] = [
        {"start": "2026-11-17T09:00:00Z", "end": "2026-11-17T10:00:00Z"}  # 18:00-19:00 JST
    ]
    both = json.loads(tools["find_free_time"](start_date="2026-11-17", end_date="2026-11-17", min_minutes=60))
    assert [(s["start"], s["end"]) for s in both["free_slots"]] == [
        ("09:00", "10:00"), ("12:00", "14:00"), ("15:00", "18:00"), ("19:00", "22:00"),
    ]
    hanako = json.loads(tools["find_free_time"](start_date="2026-11-17", end_date="2026-11-17", who="はなこ"))
    assert [(s["start"], s["end"]) for s in hanako["free_slots"]] == [("09:00", "14:00"), ("15:00", "22:00")]


def test_find_free_time_skips_past_and_supports_midnight(tools):
    out = json.loads(tools["find_free_time"](start_date="2026-11-16", end_date="2026-11-16", day_start="06:00", day_end="24:00"))
    assert out["free_slots"] == [{"date": "2026-11-16", "weekday": "月", "start": "08:00", "end": "24:00", "minutes": 960}]


def test_compute_free_slots_merges_overlaps():
    busy = [
        (datetime(2026, 11, 20, 10, 0, tzinfo=TZ), datetime(2026, 11, 20, 12, 0, tzinfo=TZ)),
        (datetime(2026, 11, 20, 11, 0, tzinfo=TZ), datetime(2026, 11, 20, 13, 0, tzinfo=TZ)),
    ]
    slots = compute_free_slots(busy, date(2026, 11, 20), date(2026, 11, 20), time(9), time(18), 30, TZ)
    assert [(s.hour, e.hour) for s, e in slots] == [(9, 10), (13, 18)]
