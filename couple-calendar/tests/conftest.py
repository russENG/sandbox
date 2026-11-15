from __future__ import annotations

import copy
import itertools
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from couple_calendar.config import Member, Settings
from couple_calendar.gcal import CoupleCalendar

TZ = ZoneInfo("Asia/Tokyo")


def make_settings(**overrides) -> Settings:
    values = dict(
        calendar_id="shared@group.calendar.google.com",
        timezone="Asia/Tokyo",
        members=(
            Member(key="member1", name="たろう", color_id="9", line_user_id="U_taro", email="taro@example.com",
                   personal_calendar_id="taro@example.com", work_calendar_id="taro@work.example.com"),
            Member(key="member2", name="はなこ", color_id="4", line_user_id="U_hana", email="hana@example.com"),
        ),
        both_label="ふたり",
        both_color_id="10",
        claude_model="claude-opus-5-5",
        claude_effort="medium",
        line_channel_secret="line-secret",
        line_channel_access_token="line-token",
        line_setup_mode=False,
        group_trigger_words=("コンシェルジュ",),
        mcp_user_name="たろう",
    )
    values.update(overrides)
    return Settings(**values)


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def _http_error(status: int, reason: str):
    from googleapiclient.errors import HttpError

    class Resp(dict):
        pass

    resp = Resp()
    resp.status = status
    resp.reason = reason
    return HttpError(resp, reason.encode())


class FakeEvents:
    """カレンダーIDごとに予定を持つ、Calendar API events() の偽物。"""

    def __init__(self, calendars: dict[str, dict[str, dict]], forbidden: set[str]):
        self.calendars = calendars
        self.forbidden = forbidden
        self.ids = itertools.count(1)
        self.calls: list[tuple[str, dict]] = []

    def _store(self, calendar_id: str) -> dict[str, dict]:
        if calendar_id in self.forbidden:
            raise _http_error(403, "Forbidden")
        return self.calendars.setdefault(calendar_id, {})

    def list(self, **params):
        self.calls.append(("list", params))

        def run():
            store = self._store(params["calendarId"])
            prop = params.get("privateExtendedProperty")
            out = []
            for ev in store.values():
                if prop:
                    key, value = prop.split("=", 1)
                    if ((ev.get("extendedProperties") or {}).get("private") or {}).get(key) == value:
                        out.append(copy.deepcopy(ev))
                    continue
                tmin = datetime.fromisoformat(params["timeMin"])
                tmax = datetime.fromisoformat(params["timeMax"])
                s = ev["start"].get("dateTime") or ev["start"]["date"]
                e = ev["end"].get("dateTime") or ev["end"]["date"]
                sdt = datetime.fromisoformat(s) if "T" in s else datetime.fromisoformat(s).replace(tzinfo=TZ)
                edt = datetime.fromisoformat(e) if "T" in e else datetime.fromisoformat(e).replace(tzinfo=TZ)
                if sdt < tmax and edt > tmin:
                    q = params.get("q")
                    if q and q not in ev.get("summary", "") and q not in ev.get("description", ""):
                        continue
                    out.append(copy.deepcopy(ev))
            out.sort(key=lambda ev: ev["start"].get("dateTime") or ev["start"]["date"])
            return {"items": out}

        return _Req(run)

    def insert(self, calendarId, body):
        self.calls.append(("insert", body))

        def run():
            ev = copy.deepcopy(body)
            ev["id"] = f"ev{next(self.ids)}"
            ev.setdefault("creator", {"email": "bot@project.iam.gserviceaccount.com"})
            self._store(calendarId)[ev["id"]] = ev
            return copy.deepcopy(ev)

        return _Req(run)

    def get(self, calendarId, eventId):
        return _Req(lambda: copy.deepcopy(self._find(calendarId, eventId)))

    def patch(self, calendarId, eventId, body):
        self.calls.append(("patch", body))

        def run():
            ev = self._find(calendarId, eventId)
            for k, v in body.items():
                if isinstance(v, dict) and isinstance(ev.get(k), dict):
                    merged = {**ev[k], **v}
                    ev[k] = {kk: vv for kk, vv in merged.items() if vv is not None}
                else:
                    ev[k] = v
            return copy.deepcopy(ev)

        return _Req(run)

    def update(self, calendarId, eventId, body):
        self.calls.append(("update", body))

        def run():
            old = self._find(calendarId, eventId)
            ev = {**copy.deepcopy(body), "id": eventId, "creator": old.get("creator")}
            self._store(calendarId)[eventId] = ev
            return copy.deepcopy(ev)

        return _Req(run)

    def delete(self, calendarId, eventId):
        self.calls.append(("delete", {"eventId": eventId}))

        def run():
            self._find(calendarId, eventId)
            del self._store(calendarId)[eventId]
            return ""

        return _Req(run)

    def _find(self, calendar_id, event_id):
        store = self._store(calendar_id)
        if event_id not in store:
            raise _http_error(404, "Not Found")
        return store[event_id]


class FakeFreeBusy:
    def __init__(self, busy: dict[str, list[dict]]):
        self.busy = busy

    def query(self, body):
        return _Req(lambda: {"calendars": {i["id"]: {"busy": self.busy.get(i["id"], [])} for i in body["items"]}})


SHARED_ID = "shared@group.calendar.google.com"
WORK_ID = "taro@work.example.com"


class FakeService:
    def __init__(self):
        self.store: dict[str, dict] = {}  # 共有カレンダー
        self.work_store: dict[str, dict] = {}  # たろうの仕事のカレンダー
        self.forbidden: set[str] = set()
        self._events = FakeEvents({SHARED_ID: self.store, WORK_ID: self.work_store}, self.forbidden)
        self.personal_busy: dict[str, list[dict]] = {}

    def events(self):
        return self._events

    def freebusy(self):
        return FakeFreeBusy(self.personal_busy)


class FixedClockCalendar(CoupleCalendar):
    fixed_now = datetime(2026, 11, 16, 8, 0, tzinfo=TZ)  # 月曜日

    def now(self) -> datetime:
        return self.fixed_now


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def calendar(settings, service) -> FixedClockCalendar:
    return FixedClockCalendar(settings, service=service)
