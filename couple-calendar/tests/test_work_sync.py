from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from couple_calendar.concierge import Concierge
from couple_calendar.line_app import create_app
from couple_calendar.work_sync import WorkTripSync

from .conftest import make_settings
from .test_concierge import FakeAnthropic, _message


def _copy(service, copy_id, work_id, start, end, member="member1"):
    service.store[copy_id] = {
        "id": copy_id, "summary": f"出張（{copy_id}）", "colorId": "9",
        "start": {"date": start}, "end": {"date": end},
        "extendedProperties": {"private": {"coupleOwner": member, "workEventId": work_id, "workMember": member}},
    }


def _work(service, work_id, start, end, **extra):
    service.work_store[work_id] = {"id": work_id, "summary": "出張", "start": {"date": start}, "end": {"date": end}, **extra}


def _sync(settings, calendar, responses):
    fake = FakeAnthropic(responses)
    return WorkTripSync(settings, calendar, Concierge(settings, calendar, client=fake.client())), fake


def test_prune_removes_copies_whose_source_is_gone(settings, calendar, service):
    _work(service, "alive", "2026-11-24", "2026-11-26")
    _copy(service, "c_alive", "alive", "2026-11-24", "2026-11-26")
    _copy(service, "c_deleted", "deleted", "2026-11-27", "2026-11-28")  # 元の予定が消えた
    _work(service, "cancelled", "2026-12-01", "2026-12-02", status="cancelled")
    _copy(service, "c_cancelled", "cancelled", "2026-12-01", "2026-12-02")
    _work(service, "declined", "2026-12-03", "2026-12-04",
          attendees=[{"email": "taro@work.example.com", "self": True, "responseStatus": "declined"}])
    _copy(service, "c_declined", "declined", "2026-12-03", "2026-12-04")
    _work(service, "moved", "2027-03-01", "2027-03-02")  # 期間の外へ延期された
    _copy(service, "c_moved", "moved", "2026-12-08", "2026-12-09")
    service.store["manual"] = {"id": "manual", "summary": "歯医者", "start": {"date": "2026-11-25"}, "end": {"date": "2026-11-26"}}

    sync, _ = _sync(settings, calendar, [])
    start, end = sync.window()
    removed = sync.prune(start, end)

    assert sorted(service.store) == ["c_alive", "manual"]
    assert len(removed) == 4 and all("【たろう】" in r for r in removed)


def test_run_prunes_then_asks_claude_to_import(settings, calendar, service):
    _copy(service, "c_deleted", "deleted", "2026-11-27", "2026-11-28")
    sync, fake = _sync(settings, calendar, [_message("msg_1", [{"type": "text", "text": "変更なし"}], "end_turn")])
    result = sync.run()

    assert result.start == "2026-11-16" and result.end == "2027-01-11"  # 今日から8週間
    assert result.removed and result.report == "変更なし"
    prompt = fake.requests[0]["messages"][-1]["content"]
    assert "たろうさんの仕事のカレンダー" in prompt
    assert "2026-11-16〜2027-01-11" in prompt
    assert "質問はできない" in prompt


def test_run_without_work_calendars(service):
    from couple_calendar.config import Member

    from .conftest import FixedClockCalendar

    settings = make_settings(members=(Member(key="member1", name="たろう", color_id="9"),
                                      Member(key="member2", name="はなこ", color_id="4")))
    calendar = FixedClockCalendar(settings, service=service)
    sync = WorkTripSync(settings, calendar, Concierge(settings, calendar, client=FakeAnthropic([]).client()))
    with pytest.raises(RuntimeError, match="仕事のカレンダーが設定されていません"):
        sync.run()


class FakeSync:
    def __init__(self, settings):
        self.settings = settings
        self.runs = 0

    def run(self):
        self.runs += 1

        class R:
            def to_dict(self):
                return {"start": "2026-11-16", "end": "2027-01-11", "removed": [], "report": "変更なし"}

        return R()


def test_endpoint_requires_secret():
    sync = FakeSync(make_settings(cron_secret="s3cret"))
    client = TestClient(create_app(bot_factory=lambda: None, sync_factory=lambda: sync))
    assert client.post("/tasks/sync-work-trips").status_code == 403
    assert client.post("/tasks/sync-work-trips", headers={"x-cron-secret": "wrong"}).status_code == 403
    res = client.post("/tasks/sync-work-trips", headers={"x-cron-secret": "s3cret"})
    assert res.status_code == 200 and res.json()["report"] == "変更なし"
    assert sync.runs == 1


def test_endpoint_is_closed_when_no_secret_is_configured():
    sync = FakeSync(make_settings(cron_secret=None))
    client = TestClient(create_app(bot_factory=lambda: None, sync_factory=lambda: sync))
    assert client.post("/tasks/sync-work-trips", headers={"x-cron-secret": ""}).status_code == 403
    assert sync.runs == 0
