"""Claude Code から共有カレンダーを操作するための MCP サーバー（stdio）。

登録例:
  claude mcp add couple-calendar --scope user -- uv run --directory /path/to/couple-calendar couple-calendar-mcp
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings, get_settings
from .gcal import CalendarError, CoupleCalendar
from .tools import build_tools


def _with_readable_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """カレンダーのエラー文をそのまま Claude Code に伝える（ToolError 以外の例外は MCP が中身を隠すため）。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except CalendarError as e:
            raise ToolError(str(e)) from e

    return wrapper


def build_instructions(settings: Settings) -> str:
    m1, m2 = settings.members
    me = settings.mcp_user_name
    lines = [
        f"{m1.name}さんと{m2.name}さん夫婦の共有Googleカレンダーを操作するツールです。",
        f"予定の担当者は「{m1.name}」「{m2.name}」「{settings.both_label}」（二人の予定）のいずれかです。",
        f"日時はタイムゾーン {settings.timezone} で扱います。相対的な日付は get_calendar_info の現在日時から計算してください。",
        "予定を変更・削除するときは、先に list_events で対象の id を調べ、候補が複数あれば利用者に確認してください。",
    ]
    if any(m.work_calendar_id for m in settings.members):
        lines.append(
            "仕事のカレンダーの出張・移動を共有カレンダーに写すように頼まれたら、list_work_events で読み、"
            "その説明のルールに従って create_event で登録してください。期間の指定がなければ今日から8週間先までです。"
        )
    if me:
        lines.append(f"このClaude Codeの利用者は{me}さんです。「私の予定」は{me}さんの予定として扱ってください。")
    return "\n".join(lines)


def create_server(settings: Settings | None = None, calendar: CoupleCalendar | None = None) -> MCPServer:
    settings = settings or get_settings()
    calendar = calendar or CoupleCalendar(settings)
    server = MCPServer("couple-calendar", instructions=build_instructions(settings))
    for fn in build_tools(calendar):
        server.add_tool(_with_readable_errors(fn))
    return server


def create_setup_server(reason: str) -> MCPServer:
    """設定が足りないときの代わりのサーバー。起動に失敗して「接続できない」となる代わりに、何を設定すればよいかを返す。"""
    message = f"夫婦カレンダーはまだ設定されていません: {reason}（couple-calendar/.env を作成してください。手順は README.md）"
    server = MCPServer("couple-calendar", instructions=message)

    def get_calendar_info() -> str:
        """共有カレンダーの基本情報を返す（未設定のため、設定方法を返す）。"""
        raise ToolError(message)

    server.add_tool(get_calendar_info)
    return server


def main() -> None:
    try:
        server = create_server()
    except RuntimeError as e:
        server = create_setup_server(str(e))
    server.run()  # stdio


if __name__ == "__main__":
    main()
