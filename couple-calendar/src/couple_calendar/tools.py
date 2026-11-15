"""カレンダー操作ツール。LINE コンシェルジュ（Claude API の tool runner）と
Claude Code 用 MCP サーバーの両方で、同じ関数をそのまま使う。

各関数は JSON 文字列を返す。失敗したときは例外を投げ、呼び出し側でエラーとして扱われる。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .config import BOTH
from .gcal import CalendarError, CoupleCalendar, compute_free_slots, parse_clock, weekday_ja

MAX_LIST_DAYS = 92
MAX_LISTED = 100


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def build_tools(cal: CoupleCalendar) -> list[Callable[..., str]]:
    settings = cal.settings

    def _owner(value: str) -> str:
        try:
            return settings.resolve_owner(value)
        except ValueError as e:
            raise CalendarError(str(e)) from e

    def get_calendar_info() -> str:
        """共有カレンダーの基本情報を返す: 現在日時、タイムゾーン、メンバー（予定の担当者）の名前。

        「明日」「来週」などの日付を計算する前や、担当者名が分からないときに使う。
        """
        now = cal.now()
        return _json(
            {
                "now": now.strftime("%Y-%m-%dT%H:%M"),
                "weekday": weekday_ja(now.date()),
                "timezone": settings.timezone,
                "owners": [m.name for m in settings.members] + [settings.both_label],
                "owner_note": f"{settings.both_label} は二人の予定",
            }
        )

    def list_events(start_date: str, end_date: str, keyword: str | None = None) -> str:
        """共有カレンダーの予定を一覧する。予定の変更・削除の前に、対象の id を調べるのにも使う。

        Args:
            start_date: 開始日 (YYYY-MM-DD)。この日を含む。
            end_date: 終了日 (YYYY-MM-DD)。この日を含む。1日だけ見るなら start_date と同じ日。
            keyword: タイトル・メモ・場所で絞り込むキーワード（任意）。
        """
        first, last = cal.parse_date(start_date), cal.parse_date(end_date)
        if (last - first).days > MAX_LIST_DAYS:
            raise CalendarError(f"一度に見られるのは {MAX_LIST_DAYS} 日分までです。期間を短くしてください。")
        events = [cal.summarize(e) for e in cal.list_events(first, last, keyword or None)]
        result: dict[str, Any] = {"count": len(events), "events": events[:MAX_LISTED]}
        if len(events) > MAX_LISTED:
            result["note"] = f"{len(events)} 件中 {MAX_LISTED} 件だけ表示しています。期間を短くしてください。"
        return _json(result)

    def create_event(
        title: str,
        start: str,
        end: str | None = None,
        owner: str = "ふたり",
        all_day: bool = False,
        location: str | None = None,
        memo: str | None = None,
        repeat_rule: str | None = None,
        busy: bool | None = None,
    ) -> str:
        """共有カレンダーに予定を登録する。

        Args:
            title: 予定のタイトル（例: 歯医者、結婚記念日ディナー）。担当者名はタイトルに入れなくてよい。
            start: 開始。時刻ありは YYYY-MM-DDTHH:MM、終日予定は YYYY-MM-DD。
            end: 終了（任意）。時刻ありは YYYY-MM-DDTHH:MM（省略時は1時間後）。終日予定は最終日 YYYY-MM-DD（その日を含む。省略時は1日だけ）。
            owner: 予定の担当者。メンバーの名前、または「ふたり」（二人の予定）。
            all_day: 終日予定なら true。
            location: 場所（任意）。
            memo: メモ（任意）。
            repeat_rule: 繰り返しの RRULE（任意）。例: 毎週火曜 = FREQ=WEEKLY;BYDAY=TU、毎月10日 = FREQ=MONTHLY;BYMONTHDAY=10。
            busy: 空き時間検索で「ふさがっている」とみなすか。省略時は、時刻ありの予定は true、終日予定は false。旅行など終日でふさがる予定は true にする。
        """
        event = cal.create_event(
            title=title,
            start=start,
            end=end,
            owner_key=_owner(owner),
            all_day=all_day,
            location=location,
            memo=memo,
            repeat_rule=repeat_rule,
            busy=busy,
        )
        return _json({"created": cal.summarize(event)})

    def update_event(
        event_id: str,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        owner: str | None = None,
        all_day: bool | None = None,
        location: str | None = None,
        memo: str | None = None,
    ) -> str:
        """既存の予定を変更する。指定した項目だけが変わる。

        開始だけを変えると、元の長さのまま後ろにずれる。繰り返し予定の id（list_events で得たもの）を指定した場合は、その回だけが変わる。

        Args:
            event_id: list_events で調べた予定の id。
            title: 新しいタイトル。
            start: 新しい開始（YYYY-MM-DDTHH:MM、終日なら YYYY-MM-DD）。
            end: 新しい終了（YYYY-MM-DDTHH:MM、終日なら最終日 YYYY-MM-DD）。
            owner: 新しい担当者。メンバーの名前、または「ふたり」。
            all_day: 終日予定に変えるなら true、時刻ありに変えるなら false。
            location: 新しい場所（空文字で削除）。
            memo: 新しいメモ（空文字で削除）。
        """
        event = cal.update_event(
            event_id,
            title=title,
            start=start,
            end=end,
            owner_key=_owner(owner) if owner else None,
            all_day=all_day,
            location=location,
            memo=memo,
        )
        return _json({"updated": cal.summarize(event)})

    def delete_event(event_id: str) -> str:
        """予定を削除する。対象がはっきりしているときだけ使う（候補が複数なら先に確認する）。

        繰り返し予定の id（list_events で得たもの）を指定した場合は、その回だけが消える。

        Args:
            event_id: list_events で調べた予定の id。
        """
        event = cal.delete_event(event_id)
        return _json({"deleted": cal.summarize(event)})

    def find_free_time(
        start_date: str,
        end_date: str,
        who: str = "ふたり",
        min_minutes: int = 60,
        day_start: str = "09:00",
        day_end: str = "22:00",
    ) -> str:
        """空いている時間帯を探す。「ふたり」なら二人とも空いている時間を返す。

        共有カレンダーの予定（担当者で判定）と、設定されていれば各自の個人カレンダーの予定を見る。

        Args:
            start_date: 探し始める日 (YYYY-MM-DD)。
            end_date: 探し終わる日 (YYYY-MM-DD)。この日を含む。
            who: 誰の空きを探すか。メンバーの名前、または「ふたり」。
            min_minutes: 必要な長さ（分）。
            day_start: 1日のうち探す時間帯の始まり (HH:MM)。
            day_end: 1日のうち探す時間帯の終わり (HH:MM、24:00 も可)。
        """
        first, last = cal.parse_date(start_date), cal.parse_date(end_date)
        if last < first:
            raise CalendarError("終了日が開始日より前になっています。")
        if (last - first).days > 31:
            raise CalendarError("空き時間を探せるのは31日分までです。期間を短くしてください。")
        window_start = parse_clock(day_start) or parse_clock("00:00")
        window_end = parse_clock(day_end)
        owner_key = _owner(who)
        keys = {m.key for m in settings.members} if owner_key == BOTH else {owner_key}
        busy, notes = cal.busy_intervals(first, last, keys)
        slots = compute_free_slots(
            busy,
            first,
            last,
            window_start,
            window_end,
            max(int(min_minutes), 1),
            cal.tz,
            not_before=cal.now(),
        )
        result: dict[str, Any] = {
            "who": settings.owner_label(owner_key),
            "free_slots": [
                {
                    "date": s.date().isoformat(),
                    "weekday": weekday_ja(s.date()),
                    "start": s.strftime("%H:%M"),
                    # 枠は日をまたがないので、日付が変わっていれば終わりは 24:00
                    "end": "24:00" if e.date() > s.date() else e.strftime("%H:%M"),
                    "minutes": int((e - s).total_seconds() // 60),
                }
                for s, e in slots
            ][:60],
        }
        if notes:
            result["notes"] = notes
        return _json(result)

    return [get_calendar_info, list_events, create_event, update_event, delete_event, find_free_time]
