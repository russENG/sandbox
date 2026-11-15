"""Google カレンダー API の薄いラッパー。

予定の「担当者」（夫・妻・ふたり）は次の3つで表す:
  - extendedProperties.private.coupleOwner に member1 / member2 / both を保存（このツール経由で作った予定）
  - 予定の色（担当者ごとに色を決めておく。カレンダーアプリ上でもひと目で分かる）
  - 作成者のメールアドレス（カレンダーアプリから直接入れた予定の判定用）
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import BOTH, Member, Settings

SCOPES = ["https://www.googleapis.com/auth/calendar"]
OWNER_PROP = "coupleOwner"
WORK_PROP = "workEventId"  # 仕事のカレンダーから写した予定の、元の予定ID
WEEKDAYS = "月火水木金土日"
MAX_EVENTS = 500


class CalendarError(Exception):
    """利用者にそのまま伝えられるエラー。"""


def build_service() -> Any:
    """Application Default Credentials で Calendar API クライアントを作る。

    - ローカル: GOOGLE_APPLICATION_CREDENTIALS にサービスアカウントの鍵ファイルを指定
    - Cloud Run: サービスに紐づけたサービスアカウントがそのまま使われる（鍵ファイル不要）
    """
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=SCOPES)
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def weekday_ja(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def parse_clock(value: str) -> time | None:
    """'HH:MM' を time に。'24:00' は None（その日の終わり）を返す。"""
    v = value.strip()
    if v in ("24:00", "24:00:00"):
        return None
    try:
        return time.fromisoformat(v)
    except ValueError as e:
        raise CalendarError(f"時刻の形式が正しくありません: {value}（HH:MM で指定）") from e


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def compute_free_slots(
    busy: list[tuple[datetime, datetime]],
    first_day: date,
    last_day: date,
    window_start: time,
    window_end: time | None,
    min_minutes: int,
    tz: ZoneInfo,
    not_before: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """各日の [window_start, window_end) から busy を除いた、min_minutes 以上の空き枠を返す。"""
    merged = merge_intervals(busy)
    slots: list[tuple[datetime, datetime]] = []
    day = first_day
    while day <= last_day:
        ws = datetime.combine(day, window_start, tzinfo=tz)
        if window_end is None:
            we = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
        else:
            we = datetime.combine(day, window_end, tzinfo=tz)
        if not_before is not None and ws < not_before:
            # 現在時刻より前は除外し、15分単位に切り上げる
            nb = not_before.astimezone(tz).replace(second=0, microsecond=0)
            nb += timedelta(minutes=(-nb.minute) % 15)
            ws = max(ws, nb)
        cursor = ws
        for bs, be in merged:
            if cursor >= we or bs >= we:
                break
            if be <= cursor:
                continue
            if bs > cursor:
                slots.append((cursor, bs))
            cursor = max(cursor, be)
        if cursor < we:
            slots.append((cursor, we))
        day += timedelta(days=1)
    min_len = timedelta(minutes=min_minutes)
    return [(s, e) for s, e in slots if e - s >= min_len]


class CoupleCalendar:
    def __init__(self, settings: Settings, service: Any | None = None) -> None:
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self._service = service

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = build_service()
        return self._service

    # ------------------------------------------------------------------ 日時

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def parse_date(self, value: str) -> date:
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError as e:
            raise CalendarError(f"日付の形式が正しくありません: {value}（YYYY-MM-DD で指定）") from e

    def parse_datetime(self, value: str) -> datetime:
        v = value.strip().replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(v)
        except ValueError as e:
            raise CalendarError(f"日時の形式が正しくありません: {value}（YYYY-MM-DDTHH:MM で指定）") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    @staticmethod
    def is_date_only(value: str) -> bool:
        return len(value.strip()) == 10

    def day_start(self, d: date) -> datetime:
        return datetime.combine(d, time(0), tzinfo=self.tz)

    # ------------------------------------------------------------------ API 呼び出し

    def _execute(self, request: Any, forbidden: str | None = None) -> Any:
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
            if status in (404, 410):
                raise CalendarError("予定が見つかりませんでした（すでに削除されたか、IDが違います）。") from e
            if status == 403:
                raise CalendarError(
                    forbidden
                    or "カレンダーへのアクセス権がありません。共有カレンダーの共有設定で、"
                    "サービスアカウントに「予定の変更」権限を付けてください。"
                ) from e
            raise CalendarError(f"Googleカレンダーでエラーが発生しました（{status}）: {e}") from e

    def _events(self) -> Any:
        return self.service.events()

    # ------------------------------------------------------------------ 担当者

    def detect_owner(self, event: dict) -> str | None:
        s = self.settings
        private = (event.get("extendedProperties") or {}).get("private") or {}
        key = private.get(OWNER_PROP)
        if key == BOTH or (key and s.member_by_key(key)):
            return key
        color = event.get("colorId")
        if color:
            if color == s.both_color_id:
                return BOTH
            for m in s.members:
                if m.color_id == color:
                    return m.key
        creator = ((event.get("creator") or {}).get("email") or "").lower()
        for m in s.members:
            if m.email and creator == m.email.lower():
                return m.key
        return None

    def color_for(self, owner_key: str) -> str:
        if owner_key == BOTH:
            return self.settings.both_color_id
        member = self.settings.member_by_key(owner_key)
        if member is None:
            raise CalendarError(f"不明な担当者です: {owner_key}")
        return member.color_id

    # ------------------------------------------------------------------ 変換

    def event_bounds(self, event: dict) -> tuple[datetime, datetime]:
        start, end = event.get("start", {}), event.get("end", {})
        if "date" in start:
            s = self.day_start(date.fromisoformat(start["date"]))
            e = self.day_start(date.fromisoformat(end.get("date", start["date"])))
            return s, max(e, s + timedelta(days=1))
        return self.parse_datetime(start["dateTime"]), self.parse_datetime(end["dateTime"])

    def summarize(self, event: dict, with_owner: bool = True) -> dict:
        """モデルや人が読みやすい形に整える。終日予定の end は「最終日」（その日を含む）で返す。"""
        start = event.get("start", {})
        all_day = "date" in start
        s, e = self.event_bounds(event)
        out: dict[str, Any] = {"id": event.get("id"), "title": event.get("summary") or "(タイトルなし)"}
        if with_owner:
            out["owner"] = self.settings.owner_label(self.detect_owner(event))
        out["all_day"] = all_day
        if all_day:
            last = (e - timedelta(days=1)).date()
            out["start"] = s.date().isoformat()
            out["end"] = last.isoformat()
            out["weekday"] = weekday_ja(s.date()) if last == s.date() else f"{weekday_ja(s.date())}〜{weekday_ja(last)}"
        else:
            out["start"] = s.strftime("%Y-%m-%dT%H:%M")
            out["end"] = e.strftime("%Y-%m-%dT%H:%M") if e.date() != s.date() else e.strftime("%H:%M")
            out["weekday"] = weekday_ja(s.date())
        if event.get("location"):
            out["location"] = event["location"]
        if event.get("description"):
            out["memo"] = event["description"][:300]
        if event.get("recurringEventId") or event.get("recurrence"):
            out["recurring"] = True
        if event.get("transparency") == "transparent":
            out["busy"] = False
        if ((event.get("extendedProperties") or {}).get("private") or {}).get(WORK_PROP):
            out["from_work"] = True
        return out

    # ------------------------------------------------------------------ 操作

    def list_events(
        self,
        start: date,
        end: date,
        keyword: str | None = None,
        *,
        calendar_id: str | None = None,
        forbidden: str | None = None,
    ) -> list[dict]:
        if end < start:
            raise CalendarError("終了日が開始日より前になっています。")
        params: dict[str, Any] = {
            "calendarId": calendar_id or self.settings.calendar_id,
            "timeMin": self.day_start(start).isoformat(),
            "timeMax": self.day_start(end + timedelta(days=1)).isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if keyword:
            params["q"] = keyword
        items: list[dict] = []
        while True:
            resp = self._execute(self._events().list(**params), forbidden)
            items.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token or len(items) >= MAX_EVENTS:
                break
            params["pageToken"] = token
        return [i for i in items if i.get("status") != "cancelled"]

    def get_event(self, event_id: str) -> dict:
        return self._execute(self._events().get(calendarId=self.settings.calendar_id, eventId=event_id))

    def list_work_events(self, member: Member, start: date, end: date, keyword: str | None = None) -> list[dict]:
        """仕事のカレンダー（読み取りのみ）の予定。自分が欠席と答えた予定は除く。"""
        if not member.work_calendar_id:
            raise CalendarError(f"{member.name}さんの仕事のカレンダーが設定されていません（MEMBERx_WORK_CALENDAR_ID）。")
        events = self.list_events(
            start,
            end,
            keyword,
            calendar_id=member.work_calendar_id,
            forbidden=(
                f"{member.name}さんの仕事のカレンダーを読めません。仕事のカレンダーの共有設定で、"
                "サービスアカウントに「予定の表示（すべての予定の詳細）」を付けてください。"
            ),
        )
        declined = lambda ev: any(  # noqa: E731
            a.get("self") and a.get("responseStatus") == "declined" for a in ev.get("attendees", [])
        )
        return [ev for ev in events if not declined(ev)]

    def find_copied_work_event(self, work_event_id: str) -> dict | None:
        """仕事のカレンダーから写した予定を、元の予定IDで探す（二重登録を防ぐため）。"""
        resp = self._execute(
            self._events().list(
                calendarId=self.settings.calendar_id,
                privateExtendedProperty=f"{WORK_PROP}={work_event_id}",
                maxResults=5,
            )
        )
        items = [i for i in resp.get("items", []) if i.get("status") != "cancelled"]
        return items[0] if items else None

    def create_event(
        self,
        *,
        title: str,
        start: str,
        end: str | None = None,
        owner_key: str = BOTH,
        all_day: bool = False,
        location: str | None = None,
        memo: str | None = None,
        repeat_rule: str | None = None,
        busy: bool | None = None,
        work_event_id: str | None = None,
    ) -> tuple[dict, bool]:
        """予定を登録して (予定, 新しく作ったか) を返す。

        work_event_id（仕事のカレンダーの元の予定ID）を付けると、同じ予定を前に写していればそれを上書きする。
        """
        if not title.strip():
            raise CalendarError("予定のタイトルが空です。")
        private = {OWNER_PROP: owner_key}
        if work_event_id:
            private[WORK_PROP] = work_event_id
        body: dict[str, Any] = {
            "summary": title.strip(),
            "colorId": self.color_for(owner_key),
            "extendedProperties": {"private": private},
        }
        if all_day or self.is_date_only(start):
            first = self.parse_date(start)
            last = self.parse_date(end) if end else first
            if last < first:
                raise CalendarError("終了日が開始日より前になっています。")
            body["start"] = {"date": first.isoformat()}
            body["end"] = {"date": (last + timedelta(days=1)).isoformat()}
            # 終日予定は既定で「空き時間」扱い（ゴミの日・記念日など）。旅行など時間がふさがるものは busy=True
            if not busy:
                body["transparency"] = "transparent"
        else:
            s = self.parse_datetime(start)
            if end and self.is_date_only(end):
                raise CalendarError("終了は時刻まで指定してください（例: 2026-11-20T21:00）。")
            e = self.parse_datetime(end) if end else s + timedelta(hours=1)
            if e <= s:
                raise CalendarError("終了時刻が開始時刻より前になっています。")
            body["start"] = {"dateTime": s.isoformat(), "timeZone": self.settings.timezone}
            body["end"] = {"dateTime": e.isoformat(), "timeZone": self.settings.timezone}
            if busy is False:
                body["transparency"] = "transparent"
        if location:
            body["location"] = location
        if memo:
            body["description"] = memo
        if repeat_rule:
            rule = repeat_rule.strip()
            body["recurrence"] = [rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"]
        existing = self.find_copied_work_event(work_event_id) if work_event_id else None
        if existing:
            updated = self._execute(
                self._events().update(calendarId=self.settings.calendar_id, eventId=existing["id"], body=body)
            )
            return updated, False
        return self._execute(self._events().insert(calendarId=self.settings.calendar_id, body=body)), True

    def update_event(
        self,
        event_id: str,
        *,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        owner_key: str | None = None,
        all_day: bool | None = None,
        location: str | None = None,
        memo: str | None = None,
    ) -> dict:
        current = self.get_event(event_id)
        patch: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise CalendarError("予定のタイトルが空です。")
            patch["summary"] = title.strip()
        if location is not None:
            patch["location"] = location
        if memo is not None:
            patch["description"] = memo
        if owner_key is not None:
            patch["colorId"] = self.color_for(owner_key)
            patch["extendedProperties"] = {"private": {OWNER_PROP: owner_key}}

        if start is not None or end is not None or all_day is not None:
            cur_all_day = "date" in current.get("start", {})
            cur_s, cur_e = self.event_bounds(current)
            to_all_day = all_day if all_day is not None else (cur_all_day if start is None else self.is_date_only(start))
            if to_all_day:
                first = self.parse_date(start) if start else cur_s.date()
                if end:
                    last = self.parse_date(end)
                else:
                    days = (cur_e.date() - cur_s.date()).days if cur_all_day else 1
                    last = first + timedelta(days=max(days, 1) - 1)
                if last < first:
                    raise CalendarError("終了日が開始日より前になっています。")
                patch["start"] = {"date": first.isoformat(), "dateTime": None, "timeZone": None}
                patch["end"] = {"date": (last + timedelta(days=1)).isoformat(), "dateTime": None, "timeZone": None}
            else:
                if start is None and cur_all_day:
                    raise CalendarError("時刻のある予定に変えるには、開始日時を指定してください。")
                s = self.parse_datetime(start) if start else cur_s
                if end:
                    if self.is_date_only(end):
                        raise CalendarError("終了は時刻まで指定してください（例: 2026-11-20T21:00）。")
                    e = self.parse_datetime(end)
                else:
                    duration = (cur_e - cur_s) if not cur_all_day else timedelta(hours=1)
                    e = s + duration
                if e <= s:
                    raise CalendarError("終了時刻が開始時刻より前になっています。")
                tzname = self.settings.timezone
                patch["start"] = {"dateTime": s.isoformat(), "timeZone": tzname, "date": None}
                patch["end"] = {"dateTime": e.isoformat(), "timeZone": tzname, "date": None}

        if not patch:
            raise CalendarError("変更する項目が指定されていません。")
        return self._execute(
            self._events().patch(calendarId=self.settings.calendar_id, eventId=event_id, body=patch)
        )

    def delete_event(self, event_id: str) -> dict:
        event = self.get_event(event_id)
        self._execute(self._events().delete(calendarId=self.settings.calendar_id, eventId=event_id))
        return event

    def busy_intervals(
        self, start: date, end: date, owner_keys: set[str]
    ) -> tuple[list[tuple[datetime, datetime]], list[str]]:
        """owner_keys の人がふさがっている時間帯。共有カレンダー＋（あれば）個人カレンダーの free/busy から作る。"""
        busy: list[tuple[datetime, datetime]] = []
        notes: list[str] = []
        for event in self.list_events(start, end):
            if event.get("transparency") == "transparent":
                continue
            owner = self.detect_owner(event)
            # 担当者が不明な予定は、安全側に倒して「ふさがっている」とみなす
            if owner not in (None, BOTH) and owner not in owner_keys:
                continue
            busy.append(self.event_bounds(event))

        others = [  # (メンバー, カレンダーID, 呼び名)
            (m, cid, label)
            for m in self.settings.members
            if m.key in owner_keys
            for cid, label in ((m.personal_calendar_id, "個人"), (m.work_calendar_id, "仕事"))
            if cid
        ]
        if others:
            body = {
                "timeMin": self.day_start(start).isoformat(),
                "timeMax": self.day_start(end + timedelta(days=1)).isoformat(),
                "timeZone": self.settings.timezone,
                "items": [{"id": cid} for _, cid, _ in others],
            }
            resp = self._execute(self.service.freebusy().query(body=body))
            calendars = resp.get("calendars", {})
            for m, cid, label in others:
                info = calendars.get(cid, {})
                if info.get("errors"):
                    notes.append(f"{m.name}さんの{label}のカレンダーを参照できませんでした（共有設定を確認してください）。")
                for b in info.get("busy", []):
                    busy.append((self.parse_datetime(b["start"]), self.parse_datetime(b["end"])))
        return busy, notes
