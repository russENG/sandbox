from __future__ import annotations

import json

import pytest

from couple_calendar.gcal import CalendarError
from couple_calendar.tools import build_tools

from .conftest import WORK_ID, make_settings


@pytest.fixture
def tools(calendar):
    return {fn.__name__: fn for fn in build_tools(calendar)}


def _work(service, event_id, title, start, end, **extra):
    key = "date" if len(start) == 10 else "dateTime"
    service.work_store[event_id] = {"id": event_id, "summary": title, "start": {key: start}, "end": {key: end}, **extra}


def test_list_work_events_skips_declined(tools, service):
    _work(service, "w1", "大阪出張（A社）", "2026-11-24", "2026-11-26")
    _work(service, "w2", "定例会議", "2026-11-24T10:00:00+09:00", "2026-11-24T11:00:00+09:00",
          attendees=[{"email": "taro@work.example.com", "self": True, "responseStatus": "declined"}])
    _work(service, "w3", "のぞみ 東京→新大阪", "2026-11-24T07:00:00+09:00", "2026-11-24T09:30:00+09:00")
    out = json.loads(tools["list_work_events"](start_date="2026-11-23", end_date="2026-11-30"))
    titles = [e["title"] for e in out["たろう"]["events"]]
    assert titles == ["大阪出張（A社）", "のぞみ 東京→新大阪"]
    trip = out["たろう"]["events"][0]
    assert trip["all_day"] is True and trip["end"] == "2026-11-25"
    assert "owner" not in trip  # 仕事の予定には担当者の色分けは関係ない


def test_list_work_events_for_member_without_work_calendar(tools):
    with pytest.raises(CalendarError, match="はなこさんの仕事のカレンダーが設定されていません"):
        tools["list_work_events"](start_date="2026-11-23", end_date="2026-11-30", who="はなこ")


def test_no_work_calendars_configured(service):
    from couple_calendar.config import Member

    from .conftest import FixedClockCalendar

    settings = make_settings(members=(
        Member(key="member1", name="たろう", color_id="9"),
        Member(key="member2", name="はなこ", color_id="4"),
    ))
    tools = {fn.__name__: fn for fn in build_tools(FixedClockCalendar(settings, service=service))}
    with pytest.raises(CalendarError, match="仕事のカレンダーが設定されていません"):
        tools["list_work_events"](start_date="2026-11-23", end_date="2026-11-30")


def test_unreadable_work_calendar_explains_sharing(tools, service):
    service.forbidden.add(WORK_ID)
    with pytest.raises(CalendarError, match="すべての予定の詳細"):
        tools["list_work_events"](start_date="2026-11-23", end_date="2026-11-30", who="たろう")


def test_hidden_details_are_reported(tools, service):
    service.work_store["w1"] = {"id": "w1", "start": {"dateTime": "2026-11-24T10:00:00+09:00"},
                                "end": {"dateTime": "2026-11-24T11:00:00+09:00"}}
    out = json.loads(tools["list_work_events"](start_date="2026-11-24", end_date="2026-11-24"))
    assert "時間枠のみ" in out["たろう"]["note"]


def test_copying_the_same_trip_twice_updates_instead_of_duplicating(tools, service):
    first = json.loads(tools["create_event"](
        title="出張（大阪）", start="2026-11-24", end="2026-11-25", all_day=True, busy=True,
        owner="たろう", work_event_id="w1"))
    assert "created" in first
    second = json.loads(tools["create_event"](
        title="出張（大阪・京都）", start="2026-11-24", end="2026-11-26", all_day=True, busy=True,
        owner="たろう", work_event_id="w1"))
    assert second["updated_existing"]["title"] == "出張（大阪・京都）"
    assert second["updated_existing"]["end"] == "2026-11-26"
    assert len(service.store) == 1
    [event] = service.store.values()
    assert event["extendedProperties"]["private"] == {"coupleOwner": "member1", "workEventId": "w1"}
    assert "transparency" not in event  # 出張は空き時間検索で「ふさがっている」扱い

    listed = json.loads(tools["list_events"](start_date="2026-11-24", end_date="2026-11-24"))
    assert listed["events"][0]["from_work"] is True


def test_free_time_considers_work_calendar(tools, service):
    service.personal_busy[WORK_ID] = [{"start": "2026-11-17T01:00:00Z", "end": "2026-11-17T09:00:00Z"}]  # 10-18時 JST
    out = json.loads(tools["find_free_time"](start_date="2026-11-17", end_date="2026-11-17", who="たろう"))
    assert [(s["start"], s["end"]) for s in out["free_slots"]] == [("09:00", "10:00"), ("18:00", "22:00")]
