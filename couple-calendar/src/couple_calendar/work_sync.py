"""仕事のカレンダーから出張・移動を自動で取り込む（Cloud Scheduler から3日に1回呼ぶ）。

1. 片付け（プログラムで確実に行う）
   写した予定のうち、元の予定が消えた・取り消された・欠席にした・取り込み期間の外へ移ったものを削除する。
2. 取り込み（Claude が判断する）
   期間内の仕事の予定を読み、出張・移動だけを写す。前に写したものは、変わっていれば上書きする。

LINE には何も送らない（結果はログに残る）。手元で1回だけ試すときは:
  uv run couple-calendar-sync
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .concierge import Concierge
from .config import Member, Settings, get_settings
from .gcal import CoupleCalendar

log = logging.getLogger(__name__)

SYNC_INSTRUCTION = """\
自動の取り込みです（定期的に、誰も見ていないところで動いています）。
{names}の仕事のカレンダーを list_work_events で {start}〜{end} の範囲で見て、出張・移動の予定を共有カレンダーに写してください。
- 写し方は list_work_events の説明に従ってください。
- 先に共有カレンダーの同じ期間を list_events で見てください。すでに写してある予定（work_event_id が同じもの）は、日時・場所・行き先が変わったときだけ create_event で上書きし、変わっていなければ何もしないでください。
- 手で入れた同じ出張（work_event_id がないもの）がすでにあれば、写さないでください。
- 質問はできないので、出張・移動かどうか迷うものは写さないでください。
- 最後に、写した予定と更新した予定を短く一覧にしてください（なければ「変更なし」）。
"""


@dataclass
class SyncResult:
    start: str
    end: str
    removed: list[str] = field(default_factory=list)
    report: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class WorkTripSync:
    def __init__(self, settings: Settings, calendar: CoupleCalendar, concierge: Concierge) -> None:
        self.settings = settings
        self.calendar = calendar
        self.concierge = concierge

    def members(self) -> list[Member]:
        return [m for m in self.settings.members if m.work_calendar_id]

    def window(self) -> tuple[date, date]:
        today = self.calendar.now().date()
        return today, today + timedelta(days=self.settings.work_sync_days)

    def prune(self, start: date, end: date) -> list[str]:
        """元の予定がなくなった（または期間外へ移った）写しを削除し、消した予定の説明を返す。"""
        window_start = self.calendar.day_start(start)
        window_end = self.calendar.day_start(end + timedelta(days=1))
        removed: list[str] = []
        for copy in self.calendar.work_copies(start, end):
            member, work_event_id = self.calendar.copy_source(copy)
            if member is None or not member.work_calendar_id:
                continue  # どこから写したか分からないものは触らない
            source = self.calendar.get_work_event(member, work_event_id)
            if source is not None:
                s, e = self.calendar.event_bounds(source)
                if s < window_end and e > window_start:
                    continue  # まだ期間内にある。変わっていれば取り込みで上書きされる
            summary = self.calendar.summarize(copy)
            self.calendar.delete_event(copy["id"])
            removed.append(f"{summary['start']} {summary['title']}【{summary['owner']}】")
        return removed

    def run(self) -> SyncResult:
        members = self.members()
        if not members:
            raise RuntimeError("仕事のカレンダーが設定されていません（MEMBERx_WORK_CALENDAR_ID）。")
        start, end = self.window()
        result = SyncResult(start=start.isoformat(), end=end.isoformat(), removed=self.prune(start, end))
        names = "、".join(f"{m.name}さん" for m in members)
        result.report = self.concierge.run_task(
            SYNC_INSTRUCTION.format(names=names, start=start.isoformat(), end=end.isoformat()),
            speaker="システム（仕事のカレンダーの自動取り込み）",
        )
        log.info("仕事のカレンダーの自動取り込み: %s", json.dumps(result.to_dict(), ensure_ascii=False))
        return result


def build_sync(settings: Settings | None = None) -> WorkTripSync:
    settings = settings or get_settings()
    calendar = CoupleCalendar(settings)
    return WorkTripSync(settings, calendar, Concierge(settings, calendar))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        result = build_sync().run()
    except RuntimeError as e:
        raise SystemExit(f"取り込めませんでした: {e}") from None
    print(f"期間: {result.start}〜{result.end}")
    if result.removed:
        print("削除した写し:")
        for line in result.removed:
            print(f"  ・{line}")
    print(result.report)


if __name__ == "__main__":
    main()
